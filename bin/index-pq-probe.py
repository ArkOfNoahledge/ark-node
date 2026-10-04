#!/usr/bin/env python3
r"""
index-pq-probe.py - what quantization costs, measured on the index that exists.

    python bin/index-pq-probe.py --selftest        # synthetic, no corpus, seconds
    python bin/index-pq-probe.py                   # the measurement, ~20 min at 2.3M
    python bin/index-pq-probe.py --configs PQ64    # one config
    python bin/index-pq-probe.py --with-chroma     # also score Chroma's own HNSW

    THE ~20 MIN WAS MEASURED AT 2,315,810 VECTORS AND THE INDEX IS NOW 6,926,271.
    Every config is trained and added FROM SCRATCH - build() writes its index to
    --work but never reads one back - and the exhaustive reference is re-run per
    invocation over 14.2 GB. The default five configs at this scale is not a
    twenty minute job. The shipped question is `--configs PQ64` at one --depth at
    a time; the other four configs were the 2026-09-12 architecture choice and it
    is already made.

THE QUESTION, AND IT IS NOT THE ONE IT LOOKS LIKE.

Chroma's HNSW holds 1024-dim vectors as float32 in an M=16 graph and must be
fully RAM-resident. Measured on this machine's own store - `data_level0.bin` is
9,809,771,160 bytes over 2,315,810 vectors, and `length.bin` is exactly 4 bytes
per vector, which confirms the count - that is **4,236 bytes per vector**, or
4,287 with the id maps. At the ~22M chunks the two Wikipedias would produce it
is **94 GB resident against 64 GB of system RAM**, 16 of which Gemma is already
holding. Chroma is not slow at that size. It does not run at all.

`DECISIONS.md` 2026-09-01 recorded the escape hatch before the number existed: a
quantized faiss index over the same plain `.npy` files, available only because
the embeddings were stored separately. `faiss-cpu==1.15.0` is vendored and
pinned in `09-software/requirements-lock.txt`.

So the open question is not *does faiss use less memory* - arithmetic answers
that. It is **what quantization does to recall for BGE-M3 on THIS corpus**, and
that cannot be reasoned about. It has to be measured, and it can be measured
today, on the 2.3M vectors already on the drive, with no GPU and no re-embedding.

GROUND TRUTH IS EXACT SEARCH, NOT CHROMA.

Scoring a quantized index against Chroma would measure agreement between two
approximations and report it as accuracy - a check true about its own question
and misleading about the one being asked, which this build has now recorded four
times in four days. The reference here is an exhaustive inner product over every
vector in `10-index/vectors/*.npy`. That is 2.3M x 1024 floats, about 4.7 GB
read in blocks, and it takes under a minute.

It also produces a number nobody has ever had: **how good is Chroma's HNSW?**
`--with-chroma` scores it against the same exact reference. If it is not 1.000,
then part of what looks like quantization loss below was already being paid.

WHAT TRANSFERS TO 22M AND WHAT DOES NOT. Stated because the whole point of
measuring at 2.3M is to decide something about 22M.

  * PQ quantization error is a property of the vectors and the codebook, not of
    how many vectors there are. A flat `PQ64` recall measured here TRANSFERS.
  * IVF coarse-quantizer loss does NOT transfer: at fixed nlist, ten times the
    vectors means ten times as many per list, and nprobe has to move with it.
    The IVF rows below are a tuning knob measured at this scale, not a
    prediction.

That is why the flat configurations are run at all. They are slower and nobody
would ship them; they isolate the one number that survives the extrapolation.

RERANK IS WHAT MAKES PQ SAFE, AND IT IS FREE HERE.

Take the top `--depth` from the quantized index, then rescore those candidates
against the REAL float16 vectors and keep the top k. `vectors/*.npy` divides out
to exactly 2,048 bytes per chunk - 1024 x float16, no padding - so it is a
fixed-width array numpy memory-maps at zero cost, and row i is chunk i of that
artifact. A rerank at depth 200 is 200 reads of 2 KB.

The quantized index becomes a candidate generator and the archive of record
becomes the scorer. Both halves of that sentence exist because of the 2026-09-01
decision to keep embeddings as plain files.

HOW A CHUNK ID BECOMES A ROW, which is the one fiddly part.

`index-build.py` writes chunk k of an artifact to line k of its `.jsonl` and row
k of its `.f16.npy`, and asserts the two counts match. Ids look like
`src:dnum:i` and carry no row number, so the mapping is built by streaming the
jsonl once and is cached in the work directory. Artifacts are walked in
`sorted(..., key=int)` order - the same order `index-build.py` loads Chroma in -
and taken from `sources.json` rather than from a glob, so a stray file in
`chunks/` cannot silently shift every row after it.

A chunk id that cannot be resolved to a row RAISES. It is not skipped. A silent
skip here would quietly shrink the reference set and report the resulting
agreement as recall - the failure `index-mirror-set.py` has a fixture for.

WHAT IT WILL NOT WRITE. Nothing inside the archive. The row map, the query
embeddings and the temporary faiss indexes go to `--work`, which defaults to
`ark-pq-probe` beside the archive folder, not in it. Runtime state inside a
checksummed shelf makes a 1.6 TB manifest drift for no reason, and a session
that cannot delete must not leave anything there at all.

THE QUERY ENCODER RUNS ON THE CPU, DELIBERATELY. BGE-M3 wants 2.3 GB of VRAM and
the primary model is holding 14.5 of 16. Twenty-four short queries on the CPU
take seconds and cannot evict the model an operator is talking to. The
embeddings are cached, so a second run costs nothing.
"""

import argparse
import importlib.util
import json
import os
import re
import sys
import time

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.environ.get("ARK_ROOT") or os.path.dirname(HERE)
INDEX = os.path.join(ROOT, "10-index")
DEFAULT_WORK = os.path.join(os.path.dirname(os.path.abspath(ROOT)), "ark-pq-probe")

BLOCK = 50_000          # rows per exact-search block: 50k x 1024 x 4 = 205 MB

# Default matrix. Flat first, because those are the transferable numbers.
CONFIGS = ["PQ64", "OPQ64_1024,PQ64", "PQ32", "SQ8", "IVF4096,PQ64"]


def _sibling(name):
    """Import a bin/ tool by path; their filenames carry hyphens. Same helper and
    the same reason as measure-filters.py, which is where this was taken from."""
    spec = importlib.util.spec_from_file_location(
        name.replace("-", "_").replace(".py", ""), os.path.join(HERE, name))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def model_path():
    """Where the index's own embedder lives, IMPORTED from index-build.py.

    THE FIRST VERSION HARDCODED IT AND GOT IT WRONG, in a comment that claimed
    to be quoting index-build.py: it said `01-models/embedding/bge-m3` where the
    real constant is `01-models/tier4-embedding/bge-m3`. It failed on the first
    real run, which is the cheap version of that mistake - a path that existed
    but held a DIFFERENT model would have produced query vectors in another
    space and a full table of plausible, meaningless numbers.

    Same rule store.py already follows with the BM25 stoplist and
    measure-filters.py with the query list: import the one definition rather
    than keep a second copy that can drift. index-build.py's module level is
    regexes, constants and the three HF_*_OFFLINE environment variables - which
    this tool wants set anyway - and `main()` is behind `__name__`, so importing
    it loads nothing."""
    m = getattr(_sibling("index-build.py"), "MODEL", None)
    if not m:
        sys.exit("index-build.py no longer defines MODEL. This tool reads it "
                 "from there deliberately rather than keeping a second copy.")
    if not os.path.isdir(m):
        sys.exit("the embedding model is not on this drive:\n  %s\n"
                 "Query vectors must come from the SAME model the index was "
                 "built with, or every number in the table is meaningless." % m)
    return m


def need_faiss():
    try:
        import faiss                                           # noqa: F401
        return faiss
    except ImportError:
        sys.exit(
            "faiss is not importable by this interpreter.\n"
            "It IS on the drive - 09-software/python-wheels/\n"
            "  faiss_cpu-1.15.0-cp312-cp312-win_amd64.whl\n"
            "Install it offline into the keeper venv:\n"
            '  <venv>\\Scripts\\pip.exe install --no-index '
            '--find-links "%s" faiss-cpu==1.15.0'
            % os.path.join(ROOT, "09-software", "python-wheels"))


# ---------------------------------------------------------------------------
# the artifacts, in the order index-build.py loaded them

class Artifact(object):
    __slots__ = ("sid", "file", "slug", "chunks", "vectors", "n", "base")

    def __repr__(self):
        return "<%s n=%d base=%d>" % (self.slug, self.n, self.base)


def artifacts():
    """Every artifact with BOTH a chunk file and a vector file, in build order.

    Taken from sources.json rather than from a glob of chunks/. A glob would
    include a file index-build.py never loaded and shift every global row after
    it, which is a defect that produces plausible wrong answers rather than an
    error."""
    import numpy as np
    reg = json.load(open(os.path.join(INDEX, "sources.json"), encoding="utf-8"))
    out, base = [], 0
    for sid, a in sorted(reg["artifacts"].items(), key=lambda kv: int(kv[0])):
        slug = re.sub(r"[^A-Za-z0-9._-]", "_", a["file"])
        cf = os.path.join(INDEX, "chunks", slug + ".jsonl")
        vf = os.path.join(INDEX, "vectors", slug + ".f16.npy")
        if not (os.path.exists(cf) and os.path.exists(vf)):
            continue
        art = Artifact()
        art.sid, art.file, art.slug = int(sid), a["file"], slug
        art.chunks, art.vectors = cf, vf
        v = np.load(vf, mmap_mode="r")
        if v.ndim != 2:
            raise SystemExit("%s is not a 2-D array: shape %s" % (vf, v.shape))
        art.n, art.base = int(v.shape[0]), base
        base += art.n
        out.append(art)
    if not out:
        raise SystemExit("no artifacts with both chunks and vectors under %s" % INDEX)
    return out


def row_map(arts, work, quiet=False):
    """Global row -> chunk id, built by streaming each .jsonl once and cached.

    The cache is keyed on the size and mtime of every vector file, the same
    stamp discipline mirror-mdwiki.json uses, so a rebuilt artifact invalidates
    it instead of silently mapping rows to the ids of a previous build."""
    stamp = [[a.slug, a.n, os.path.getsize(a.vectors), int(os.path.getmtime(a.vectors))]
             for a in arts]
    cache = os.path.join(work, "rowmap.json")
    if os.path.exists(cache):
        try:
            got = json.load(open(cache, encoding="utf-8"))
            if got.get("stamp") == stamp:
                if not quiet:
                    print("  row map      %s ids (cached)" % "{:,}".format(len(got["ids"])))
                return got["ids"]
            if not quiet:
                print("  row map      cache is stale, rebuilding")
        except Exception:                                      # noqa: BLE001
            if not quiet:
                print("  row map      cache unreadable, rebuilding")

    ids = []
    t0 = time.time()
    for a in arts:
        n = 0
        with open(a.chunks, encoding="utf-8") as fh:
            for line in fh:
                ids.append(json.loads(line)["id"])
                n += 1
        # index-build.py asserts this on the way in. Asserting it again on the
        # way out is what makes row k of the .npy trustworthy as chunk k.
        if n != a.n:
            raise SystemExit("%s: %d jsonl lines but %d vectors - the row map "
                             "would be wrong for every artifact after this one"
                             % (a.file, n, a.n))
    if not quiet:
        print("  row map      %s ids in %.1fs" % ("{:,}".format(len(ids)), time.time() - t0))
    os.makedirs(work, exist_ok=True)
    json.dump({"stamp": stamp, "ids": ids}, open(cache, "w", encoding="utf-8"))
    return ids


# ---------------------------------------------------------------------------
# vectors

def gather(arts, rows, _cache=None):
    """The real float16 vectors for a list of GLOBAL row numbers, as float32.

    Used by the rerank and by nothing else. A row is looked up in the artifact
    whose [base, base+n) window contains it, by bisect on the bases.

    THE FIRST VERSION WALKED TWO POINTERS OVER A SORTED COPY and ran off the end
    of the list on a row past the last artifact - found by the fixture below,
    which exists because a row that cannot be placed must RAISE. Returning a
    zero vector instead would score as orthogonal, drop the candidate to last
    place, and read as a recall loss caused by quantization. Bisect has no
    cursor to get wrong."""
    import bisect
    import numpy as np
    if _cache is None:
        _cache = {}
    bases = [a.base for a in arts]
    out = np.empty((len(rows), dim(arts)), dtype=np.float32)
    for pos, g in enumerate(rows):
        ai = bisect.bisect_right(bases, g) - 1
        if ai < 0 or not (arts[ai].base <= g < arts[ai].base + arts[ai].n):
            raise SystemExit("global row %d is outside every artifact "
                             "(%d vectors in %d artifacts)"
                             % (g, bases[-1] + arts[-1].n, len(arts)))
        v = _cache.get(ai)
        if v is None:
            v = _cache[ai] = np.load(arts[ai].vectors, mmap_mode="r")
        out[pos] = np.asarray(v[g - arts[ai].base], dtype=np.float32)
    return out


def dim(arts):
    """The embedding width, read from the vectors rather than assumed to be 1024.

    Hardcoding it would make this tool quietly wrong the day the embedder
    changes, and `bge-m3` is exactly the kind of pin a later phase revisits.
    Deliberately NOT cached in a module-level default: a cache keyed on nothing
    is how a selftest's synthetic width would leak into a real run."""
    import numpy as np
    return int(np.load(arts[0].vectors, mmap_mode="r").shape[1])


def stream(arts, block=BLOCK):
    """Yield (base_row, float32 block) over every vector, in global row order."""
    import numpy as np
    for a in arts:
        v = np.load(a.vectors, mmap_mode="r")
        for s in range(0, a.n, block):
            e = min(s + block, a.n)
            yield a.base + s, np.asarray(v[s:e], dtype=np.float32)


def normalize(x):
    import numpy as np
    n = np.linalg.norm(x, axis=1, keepdims=True)
    n[n == 0] = 1.0
    return x / n


def exact_topk(arts, Q, depth):
    """Exhaustive inner product over every vector. This is the reference.

    The vectors were L2-normalized at embed time (index-build.py passes
    normalize_embeddings=True), so inner product IS cosine and this ranks
    identically to the collection's own `hnsw:space: cosine`. float16 storage
    perturbs the norms in the fourth decimal; both sides see the same values."""
    import numpy as np
    nq = Q.shape[0]
    best_s = np.full((nq, depth), -np.inf, dtype=np.float32)
    best_i = np.full((nq, depth), -1, dtype=np.int64)
    rows = np.arange(nq)[:, None]
    t0 = time.time()
    for base, B in stream(arts):
        sc = (B @ Q.T).T                                   # (nq, b)
        idx = np.arange(base, base + B.shape[0], dtype=np.int64)
        cat_s = np.concatenate([best_s, sc], axis=1)
        cat_i = np.concatenate([best_i, np.broadcast_to(idx, (nq, idx.size))], axis=1)
        part = np.argpartition(-cat_s, depth - 1, axis=1)[:, :depth]
        best_s, best_i = cat_s[rows, part], cat_i[rows, part]
    order = np.argsort(-best_s, axis=1)
    return best_i[rows, order], best_s[rows, order], time.time() - t0


# ---------------------------------------------------------------------------
# the queries

def load_queries(path=None):
    """(queries, groups, provenance) - what to ask, and how to score it apart.

    TWO SETS, AND THE DIFFERENCE BETWEEN THEM MUST BE PRINTED. Without a path
    this returns the 43 hand-written questions, which are the only coverage the
    medical, water, repair and food artifacts have. With a path it reads a set
    built by `bin/index-queryset.py` from real question titles in the corpus -
    288 of them at 12 per artifact - which fixes the sample size and the
    pass-1/pass-2 topic confound, and covers ONLY the 24 Stack Exchange
    artifacts, because they are the only ones whose titles are questions.

    So the two sets answer different questions and their numbers are NOT
    comparable. `provenance` is returned rather than left implicit, and the
    caller prints it, for the reason this build has recorded ten times: a figure
    that does not carry what it was measured on gets compared with one that was
    measured on something else, and nothing in the output says so.

    SELF-RETRIEVAL DOES NOT NEED EXCLUDING HERE, and this is the one tool where
    that is true. The reference is exhaustive inner product over the same
    vectors, so a query that is its own document's title matches itself on both
    sides and cancels exactly. `exclude_cid_prefix` rides along in the file for
    `measure-retrieval.py`, where it is load-bearing."""
    if not path:
        mr = _sibling("measure-retrieval.py")
        qs = list(mr.QUERIES_ALL)
        n1 = len(mr.QUERIES)
        return (qs,
                [("pass 1 queries", list(range(n1))),
                 ("pass 2 queries", list(range(n1, len(qs))))],
                "bin/measure-retrieval.py, %d hand-written questions - medical, "
                "water, repair and food are covered here and NOWHERE else" % len(qs))

    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    rows = doc.get("queries") or []
    if not rows:
        raise SystemExit("%s holds no queries" % path)
    qs = [r["q"] for r in rows]

    # GROUPED BY THE PASS THE ARTIFACT WAS INDEXED IN, not by position in the
    # list. The hand-written set is grouped by position because it was built in
    # two halves; doing that to a sampled set would silently mislabel it the
    # first time the sampler's output order changed.
    by = {}
    for i, r in enumerate(rows):
        by.setdefault(r.get("pass_", 0), []).append(i)
    groups = [(("pass %d queries" % p) if p else "queries of no recorded pass",
               ix) for p, ix in sorted(by.items())]

    prov = "%s, %d questions, seed %s, generated %s" % (
        os.path.basename(path), len(qs), doc.get("seed"), doc.get("generated"))
    cav = (doc.get("caveat") or "").strip()
    if cav:
        prov += "\n               " + cav
    return qs, groups, prov


def query_vectors(queries, work, quiet=False):
    """Embed on the CPU and cache. See the module docstring for why CPU."""
    import numpy as np
    cache = os.path.join(work, "queries.npz")
    if os.path.exists(cache):
        try:
            z = np.load(cache, allow_pickle=True)
            if list(z["q"]) == list(queries):
                if not quiet:
                    print("  queries      %d (cached)" % len(queries))
                return z["v"].astype(np.float32)
        except Exception:                                      # noqa: BLE001
            pass
    from sentence_transformers import SentenceTransformer
    t0 = time.time()
    m = SentenceTransformer(model_path(), local_files_only=True, device="cpu")
    v = np.asarray(m.encode(list(queries), normalize_embeddings=True,
                            show_progress_bar=False), dtype=np.float32)
    if not quiet:
        print("  queries      %d embedded on CPU in %.1fs" % (len(queries), time.time() - t0))
    os.makedirs(work, exist_ok=True)
    np.savez(cache, q=np.array(list(queries), dtype=object), v=v)
    return v


# ---------------------------------------------------------------------------
# the measurement

def chroma_bytes_per_vector(total):
    """What Chroma's HNSW actually costs per vector, MEASURED off this drive.

    The baseline row exists to be compared against, so it has to be read rather
    than remembered. The resident part of a Chroma segment is the level-0 graph,
    the upper link lists, the length array and the id maps; `chroma.sqlite3` is
    on disk and is not counted here, though it is roughly three times larger
    again and is the reason dropping Chroma frees more disk than RAM.

    `length.bin` is 4 bytes per vector, so it doubles as a check: if it does not
    divide out to the number of vectors on the drive, the collection and the
    .npy files describe different builds and the whole table is meaningless."""
    seg = os.path.join(INDEX, "chroma")
    if not os.path.isdir(seg):
        return 4287.0, "recorded 2026-09-12, chroma/ not on this drive"
    files, n_from_len = 0, None
    for dirpath, _dirs, names in os.walk(seg):
        for nm in names:
            if nm in ("data_level0.bin", "link_lists.bin", "length.bin",
                      "index_metadata.pickle"):
                p = os.path.join(dirpath, nm)
                files += os.path.getsize(p)
                if nm == "length.bin":
                    n_from_len = os.path.getsize(p) // 4
    if not files:
        return 4287.0, "recorded 2026-09-12, no hnsw segment found"
    if n_from_len and n_from_len != total:
        return (files / float(n_from_len),
                "MEASURED, but length.bin says %s vectors and the .npy files "
                "hold %s - the collection is from a different build"
                % ("{:,}".format(n_from_len), "{:,}".format(total)))
    return files / float(total), "measured off chroma/ on this drive"


def recall(got, ref, k, sel=None):
    """Fraction of the exact top-k that a config's top-k also returned.

    Set overlap, not rank correlation. The operator sees a page, not an
    ordering, and two results that swap places are not a defect.

    IT COMPARES ROW IDS, AND ON THIS CORPUS THAT UNDERSTATES, 2026-09-15.
    `index-pq-build.py --check` measured 41 of 48 displaced items as
    near-duplicates at similarity 1.000. Two byte-identical chunks are the SAME
    PASSAGE to the operator, and this function scores returning the other copy
    as a miss. That is the same mistake the 90.6% rank-1 figure was, in a second
    tool, and it is why `ties()` below exists: the id number stays as the
    headline so it remains comparable to 2026-09-12, and the tie count is
    printed beside it rather than folded into it.

    `sel` scores a SUBSET of the queries, which is how the pass-1 and pass-2
    groups are reported separately. Without it a set that reaches two thirds of
    the index and a set that reaches a third average into one number."""
    idx = range(len(ref)) if sel is None else sel
    hits = 0
    for i in idx:
        hits += len(set(got[i][:k]) & set(ref[i][:k]))
    n = len(ref) if sel is None else len(sel)
    return hits / float(k * n) if n else float("nan")


def ties(got, got_s, ref, ref_s, k, eps=1e-4):
    """Of the top-k rows this config MISSED, how many were ties?

    A miss is a reference row absent from `got[:k]`. It is a TIE when the config
    returned some row whose exact score equals the missed row's, within `eps` -
    a different copy of the same passage, or a genuinely equidistant neighbour.
    Nothing here is an approximation: both scores are exact inner products over
    the float16 vectors.

    Returns (misses, tied). WHY IT IS NOT FOLDED INTO recall(): a tie is only
    harmless if the duplicate is really a duplicate, and this function proves
    the SCORES match, not the TEXT. Reporting them apart lets a reader disagree.

    float16 storage perturbs norms in the fourth decimal, which is where eps
    comes from; it is not a similarity threshold and must not become one."""
    misses = tied = 0
    for i in range(len(ref)):
        gk = set(got[i][:k])
        have = [got_s[i][j] for j in range(min(k, len(got_s[i])))]
        for j in range(min(k, len(ref[i]))):
            if ref[i][j] in gk:
                continue
            misses += 1
            if any(abs(float(h) - float(ref_s[i][j])) <= eps for h in have):
                tied += 1
    return misses, tied


def build(faiss, arts, spec, total, train_n, quiet=False):
    import numpy as np
    d = dim(arts)
    try:
        ix = faiss.index_factory(d, spec, faiss.METRIC_INNER_PRODUCT)
        metric = "IP"
    except Exception:                                          # noqa: BLE001
        # Not every factory string supports inner product. For UNIT vectors the
        # L2 ordering is identical - ||a-b||^2 = 2 - 2(a.b) - so falling back
        # changes the arithmetic and not the ranking. Said out loud because a
        # silent metric change would be exactly the class of defect this file
        # exists to avoid.
        ix = faiss.index_factory(d, spec, faiss.METRIC_L2)
        metric = "L2 (inner product unsupported for this factory string)"

    if not ix.is_trained:
        want = min(train_n, total)
        take, got = [], 0
        step = max(1, total // want)
        for base, B in stream(arts):
            sel = np.arange(0, B.shape[0], step)
            if sel.size:
                take.append(normalize(B[sel]))
                got += sel.size
            if got >= want:
                break
        T = np.concatenate(take)[:want]
        t0 = time.time()
        ix.train(T)
        if not quiet:
            print("      trained on %s vectors in %.1fs" % ("{:,}".format(len(T)), time.time() - t0))

    t0 = time.time()
    for _base, B in stream(arts):
        ix.add(normalize(B))
    add_s = time.time() - t0
    return ix, metric, add_s


def measure(a):
    import numpy as np
    faiss = need_faiss()
    work = a.work
    os.makedirs(work, exist_ok=True)
    if os.path.abspath(work).startswith(os.path.abspath(ROOT) + os.sep):
        sys.exit("--work is inside the archive (%s). Runtime state in a "
                 "checksummed shelf makes the manifest drift; pick a path "
                 "outside %s." % (work, ROOT))

    # QUERIES_ALL, not QUERIES, from 2026-09-15. The original twenty-four are
    # medicine, water, repair and agriculture, and pass 2 added 4.6M chunks of
    # physics, chemistry, unix, electronics, astronomy and retrocomputing that
    # NOT ONE of them reaches. Recall measured only where the old shelves are
    # is a number about a third of this index, printed as a number about all of
    # it. measure-retrieval.py keeps both lists and says why.
    #
    # AND THEY ARE SCORED SEPARATELY. Adding the pass-2 questions and then
    # averaging all 43 into one row would have replaced a number that was
    # silently about a third of the index with a number that is silently a
    # blend - 24 easy and 19 hard reads identically to 43 medium, and the
    # pass-2 half is the half nobody has measured.
    #
    # AND SINCE 2026-09-17 THE SET CAN COME FROM THE CORPUS INSTEAD. The two
    # complaints about 43 hand-written questions are that 43 is small - the
    # depth sweep that day turned on ONE query of 43, and was only caught
    # because it moved in an impossible direction - and that its pass-1/pass-2
    # split is confounded with topic, so "pass 2 retrieves worse" and
    # "engineering retrieves worse" are the same measurement. See load_queries().
    queries, groups, prov = load_queries(getattr(a, "queries", None))
    arts = artifacts()
    total = sum(x.n for x in arts)

    print("index-pq-probe  archive=%s" % ROOT)
    print("  artifacts    %d, %s vectors, %.2f GB of float16"
          % (len(arts), "{:,}".format(total), total * 2048 / 1e9))
    # WHAT WAS ASKED, ON EVERY RUN. A recall figure with no query set beside it
    # is half a measurement, and two of them get averaged by whoever reads the
    # log next.
    print("  query set    %s" % prov)
    ids = row_map(arts, work, a.quiet)
    if len(ids) != total:
        raise SystemExit("row map has %d ids for %d vectors" % (len(ids), total))
    Q = normalize(query_vectors(queries, work, a.quiet))
    # THE ONE CHECK THAT CATCHES THE WRONG MODEL. Importing the path from
    # index-build.py stops it drifting; this catches the case the import cannot
    # - a path that exists and holds a different embedder. Vectors of unequal
    # width raise here rather than broadcasting into a table of nonsense.
    d = dim(arts)
    if Q.shape[1] != d:
        raise SystemExit(
            "the query vectors are %d-dimensional and the corpus vectors are "
            "%d. They are not from the same model, so nothing below would mean "
            "anything. Delete %s and re-run."
            % (Q.shape[1], d, os.path.join(work, "queries.npz")))

    print("  configs      %d: %s" % (len(a.configs), ", ".join(a.configs)))
    print("      TRAINING IS THE SLOW PART and it is per config. A PQ64 k-means"
          "\n      over %s vectors is minutes, not seconds. Start with"
          "\n      `--configs PQ64 SQ8` if you want the shape of the answer first."
          % "{:,}".format(a.train))

    print("\n  exact reference over every vector, depth %d" % a.depth)
    ref_rows, ref_s, secs = exact_topk(arts, Q, a.depth)
    print("      %.1fs for %d queries (%.2fs each)" % (secs, len(queries), secs / len(queries)))

    hnsw_per_vec, hnsw_src = chroma_bytes_per_vector(total)
    print("\n  %-22s %7s %7s %7s  %8s %9s %8s"
          % ("config", "r@1", "r@5", "r@10", "B/vec", "RAM@22M", "q ms"))
    print("  " + "-" * 74)
    print("  %-22s %7.3f %7.3f %7.3f  %8.0f %8.1fG %8s"
          % ("exact (reference)", 1.0, 1.0, 1.0, 4096, 4096 * 22e6 / 1e9, "-"))
    print("  %-22s %7s %7s %7s  %8.0f %8.1fG %8s"
          % ("chroma hnsw, as built", "?", "?", "?", hnsw_per_vec,
             hnsw_per_vec * 22e6 / 1e9, "-"))
    print("      (%s; --with-chroma scores it)" % hnsw_src)

    results = []
    for spec in a.configs:
        try:
            ix, metric, add_s = build(faiss, arts, spec, total, a.train, a.quiet)
        except Exception as e:                                 # noqa: BLE001
            print("  %-22s FAILED: %s" % (spec, e))
            continue
        if "IVF" in spec:
            ix.nprobe = a.nprobe

        path = os.path.join(work, re.sub(r"[^A-Za-z0-9]", "_", spec) + ".faiss")
        faiss.write_index(ix, path)
        per_vec = os.path.getsize(path) / float(total)

        t0 = time.time()
        _d, got = ix.search(Q, a.depth)
        q_ms = 1000.0 * (time.time() - t0) / len(queries)

        row = dict(spec=spec, metric=metric, per_vec=per_vec, q_ms=q_ms, add_s=add_s)
        for k in (1, 5, 10):
            row["r%d" % k] = recall(got, ref_rows, k)
        print("  %-22s %7.3f %7.3f %7.3f  %8.0f %8.1fG %8.1f"
              % (spec, row["r1"], row["r5"], row["r10"], per_vec,
                 per_vec * 22e6 / 1e9, q_ms))

        # RERANK. Top `depth` from the index, rescored against the real vectors.
        t0 = time.time()
        rr, rr_s = [], []
        for qi in range(len(queries)):
            cand = [int(c) for c in got[qi] if c >= 0]
            if not cand:
                rr.append([])
                rr_s.append([])
                continue
            V = gather(arts, cand)
            s = V @ Q[qi]
            order = np.argsort(-s)
            rr.append([cand[j] for j in order])
            rr_s.append([float(s[j]) for j in order])
        rr_ms = 1000.0 * (time.time() - t0) / len(queries)
        for k in (1, 5, 10):
            row["rr%d" % k] = recall(rr, ref_rows, k)
        print("  %-22s %7.3f %7.3f %7.3f  %8s %9s %8.1f"
              % ("  + exact rerank", row["rr1"], row["rr5"], row["rr10"], "", "", q_ms + rr_ms))

        # WHICH HALF OF THE INDEX. See the groups comment above.
        for label, sel in groups:
            if not sel:
                continue
            print("      %-18s %7.3f %7.3f %7.3f   (%d queries, reranked)"
                  % (label, recall(rr, ref_rows, 1, sel), recall(rr, ref_rows, 5, sel),
                     recall(rr, ref_rows, 10, sel), len(sel)))

        # HOW MANY OF THE MISSES WERE TIES. Added 2026-09-15 after r@1 fell from
        # 0.977 at depth 200 to 0.953 at depth 400 - IMPOSSIBLE for a correct
        # rerank, because the depth-400 candidate set CONTAINS the depth-200 one
        # and exact rescoring cannot demote a row it already ranked first. One
        # query of 43 flipped, which is what a tie broken the other way looks
        # like and is not what a broken reranker looks like. This measures it
        # instead of assuming it.
        for k in (1, 5):
            m, t = ties(rr, rr_s, ref_rows, ref_s, k)
            if m:
                print("      r@%-2d misses %3d, of which %d scored IDENTICALLY to what "
                      "was missed" % (k, m, t))
        results.append(row)

    if a.with_chroma:
        print("\n  chroma's own HNSW, against the same exact reference")
        print("  (run this with the node DOWN - two processes opening the same")
        print("   persistent store is a lock this probe should not be taking)")
        try:
            import chromadb
            cl = chromadb.PersistentClient(path=os.path.join(INDEX, "chroma"))
            col = cl.get_collection("ark_pass1")
            pos = {cid: i for i, cid in enumerate(ids)}
            t0 = time.time()
            res = col.query(query_embeddings=[q.tolist() for q in Q], n_results=a.depth)
            c_ms = 1000.0 * (time.time() - t0) / len(queries)
            got = []
            for lst in res["ids"]:
                rows = []
                for cid in lst:
                    if cid not in pos:
                        raise SystemExit(
                            "chroma returned id %r which is in no .jsonl. The "
                            "collection and the vectors describe different "
                            "builds; nothing below would mean anything." % cid)
                    rows.append(pos[cid])
                got.append(rows)
            print("  %-22s %7.3f %7.3f %7.3f  %8.0f %8.1fG %8.1f"
                  % ("chroma hnsw (measured)", recall(got, ref_rows, 1),
                     recall(got, ref_rows, 5), recall(got, ref_rows, 10),
                     hnsw_per_vec, hnsw_per_vec * 22e6 / 1e9, c_ms))
        except SystemExit:
            raise
        except Exception as e:                                 # noqa: BLE001
            print("  chroma not measured: %s: %s" % (type(e).__name__, e))

    print("""
  READ IT THIS WAY.

  r@5 is the fraction of the exact top five a config also returned. The rerank
  row is the same index used only to nominate candidates, with the real float16
  vectors doing the scoring - which is the shape that would actually ship.

  The FLAT rows (PQ64, PQ32, SQ8, OPQ) carry the number that transfers to 22M:
  quantization error does not depend on how many vectors there are. The IVF row
  does NOT transfer - nprobe=%d over nlist=4096 is a different selectivity at
  22M than at %s - and has to be re-measured or re-tuned at scale.

  RAM@22M is this config's own measured bytes per vector times 22 million. It is
  arithmetic, not a projection, and it is the column the decision turns on:
  94 GB does not fit in 64, and under 2 GB leaves the Phase 2 node room to exist.
""" % (a.nprobe, "{:,}".format(total)))
    return 0


# ---------------------------------------------------------------------------

def selftest():
    """Synthetic vectors, known answers, no corpus and no GPU.

    Every assertion here is about the MEASUREMENT rather than about faiss: that
    recall is computed the way the table claims, that a rerank of a deliberately
    bad candidate list recovers the exact answer, that the row map refuses a
    mismatched artifact instead of shifting every row after it, and that an
    unresolvable id raises. A probe that silently drops what it cannot map would
    report a smaller reference set as a higher score."""
    import numpy as np
    faiss = need_faiss()
    bad = 0
    TOTAL = 24

    def check(name, ok, detail=""):
        nonlocal bad
        print("  %-5s %-44s %s" % ("ok" if ok else "FAIL", name, detail))
        if not ok:
            bad += 1

    # 1. recall(), against hand-counted overlaps.
    ref = [[1, 2, 3, 4, 5]]
    check("identical lists are recall 1.0", recall([[1, 2, 3, 4, 5]], ref, 5) == 1.0)
    check("reordering is not a miss", recall([[5, 4, 3, 2, 1]], ref, 5) == 1.0,
          "set overlap, not rank correlation")
    check("three of five is 0.6", abs(recall([[1, 2, 3, 9, 8]], ref, 5) - 0.6) < 1e-9)
    check("r@1 reads only the first", recall([[1, 9, 9, 9, 9]], ref, 1) == 1.0)
    check("a wrong first is r@1 0.0", recall([[9, 1, 2, 3, 4]], ref, 1) == 0.0)

    # 2. exact_topk over a synthetic artifact, where the answer is constructed.
    rng = np.random.default_rng(0)
    # 12,000 x 256: wide enough that 256 PQ centroids have the training points
    # faiss wants, small enough to run in seconds. The real corpus width is read
    # from the vectors, so nothing here depends on 1024.
    N, d = 12_000, 256
    V = normalize(rng.standard_normal((N, d)).astype(np.float32))
    work = os.path.join(DEFAULT_WORK, "selftest")
    os.makedirs(work, exist_ok=True)
    vf = os.path.join(work, "syn.f16.npy")
    np.save(vf, V.astype(np.float16))
    art = Artifact()
    art.sid, art.file, art.slug = 1, "syn", "syn"
    art.chunks, art.vectors, art.n, art.base = "", vf, N, 0
    arts = [art]
    # A query that IS one of the vectors must return that vector first.
    pick = 1234
    Q = normalize(np.asarray(V[pick:pick + 1], dtype=np.float32))
    rows, scores, _ = exact_topk(arts, Q, 10)
    check("exact search finds the planted vector", int(rows[0][0]) == pick,
          "row %d, score %.4f" % (rows[0][0], scores[0][0]))
    check("exact scores descend", all(scores[0][i] >= scores[0][i + 1] for i in range(9)))

    # 3. gather() round-trips, and refuses a row it cannot place.
    g = gather(arts, [pick])
    check("gather returns the stored vector",
          float(g[0] @ Q[0]) > 0.999, "cos %.5f" % float(g[0] @ Q[0]))
    try:
        gather(arts, [N + 10])
        check("a row outside every artifact raises", False)
    except SystemExit:
        check("a row outside every artifact raises", True, "not a zero vector")

    # 4. RERANK, AND THE FIRST VERSION OF THIS TEST PASSED WITHOUT TESTING
    # ANYTHING. It built a PQ16, found the planted vector already at rank 0, and
    # then "confirmed" that reranking kept it at rank 0. A check true about its
    # own question. Replaced with three that cannot pass vacuously.
    #
    # 4a. Deterministic and never vacuous: hand the rerank a SHUFFLED copy of
    # the exact top ten and require it to reproduce the exact order. This tests
    # the scoring path itself rather than hoping an approximation errs.
    exact10 = [int(r) for r in rows[0][:10]]
    shuffled = list(exact10)
    rng.shuffle(shuffled)
    back = [shuffled[j] for j in np.argsort(-(gather(arts, shuffled) @ Q[0]))]
    check("rerank reproduces the exact order", back == exact10,
          "from a shuffled candidate list")
    check("the shuffle was a real one", shuffled != exact10,
          "otherwise the line above proves nothing")

    # 4b. THE PIPELINE, NOT THE OUTCOME. A second version of this test asserted
    # that a crude PQ keeps the true top hit in its candidates. It failed, and it
    # deserved to: whether quantization keeps the hit IS the empirical question
    # this tool exists to answer, so requiring it in a fixture is asserting the
    # conclusion. What is testable here is that the probe REPORTS what faiss
    # actually returned.
    #
    # It is worth knowing why the synthetic case is so poor. Uniform random
    # directions in high dimensions have no cluster structure for a product
    # quantizer to exploit, so this is the adversarial case rather than a
    # representative one. Real embeddings are strongly clustered, which is
    # precisely why the number has to be measured on the corpus and cannot be
    # guessed from a fixture.
    Qn = normalize((V[pick] + 0.35 * rng.standard_normal(d)).astype(np.float32)[None, :])
    true_rows, _ts, _ = exact_topk(arts, Qn, 10)
    ix = faiss.index_factory(d, "PQ8", faiss.METRIC_INNER_PRODUCT)
    ix.train(V)
    ix.add(V)
    _s, got = ix.search(Qn, 50)
    cand = [int(c) for c in got[0] if c >= 0]
    by_hand = len(set(cand[:5]) & set(int(r) for r in true_rows[0][:5])) / 5.0
    check("recall() reports what faiss actually returned",
          abs(recall([cand], [list(true_rows[0])], 5) - by_hand) < 1e-9,
          "r@5 = %.3f, counted by hand" % by_hand)

    # 4c. AN INVARIANT, TRUE BY CONSTRUCTION AND WORTH ASSERTING ANYWAY.
    # Reranking sorts the candidate set by the EXACT score. The exact top-k are
    # by definition the k highest exact scores, so any of them present in the
    # candidate set must sort above anything that is not. Rerank can therefore
    # never LOSE recall at fixed k; it can only recover it. If this ever fails,
    # gather() is handing back the wrong rows.
    rr = [cand[j] for j in np.argsort(-(gather(arts, cand) @ Qn[0]))]
    before = recall([cand], [list(true_rows[0])], 5)
    after = recall([rr], [list(true_rows[0])], 5)
    check("rerank never loses recall at fixed k", after >= before - 1e-9,
          "%.3f -> %.3f" % (before, after))

    # 4d. AND THE LIMIT, STATED AS A TEST. Rerank cannot invent a candidate the
    # index never returned, which is why `--depth` is the knob that matters: the
    # risk lives in the recall of the CANDIDATE list, not in the scorer.
    top = int(true_rows[0][0])
    if top in cand:
        without = [c for c in cand if c != top]
        rr2 = [without[j] for j in np.argsort(-(gather(arts, without) @ Qn[0]))]
        check("rerank cannot recover a hit never nominated",
              bool(rr2) and rr2[0] != top, "depth is the knob, not the scorer")
    else:
        # The crude index already missed it, which proves the same point without
        # the removal. Reported rather than skipped silently.
        check("rerank cannot recover a hit never nominated",
              all(r != top for r in rr), "the index missed it unaided")

    # 5. The row map refuses a jsonl whose length disagrees with its .npy.
    cf = os.path.join(work, "syn.jsonl")
    with open(cf, "w", encoding="utf-8") as fh:
        for i in range(N - 1):                       # one line SHORT, on purpose
            fh.write(json.dumps({"id": "1:%d:0" % i}) + "\n")
    art.chunks = cf
    try:
        row_map(arts, os.path.join(work, "nocache"), quiet=True)
        check("a short .jsonl is refused, not truncated", False)
    except SystemExit as e:
        check("a short .jsonl is refused, not truncated", "jsonl lines" in str(e))

    # 6. recall(sel=...) scores a subset and nothing else, and ties() tells a
    #    duplicate from a fault. Added 2026-09-15 with the functions themselves.
    g = [[1, 2], [9, 9], [5, 6]]
    r = [[1, 2], [3, 4], [5, 6]]
    check("recall over all queries averages them", abs(recall(g, r, 2) - 2 / 3.0) < 1e-9,
          "2 of 3 queries perfect")
    check("recall(sel) scores only the selected", recall(g, r, 2, [0, 2]) == 1.0,
          "and the failing query is excluded, not weighted to zero")
    check("recall(sel) sees the failure when selected", recall(g, r, 2, [1]) == 0.0)

    # A miss whose score MATCHES the missed row is a tie; one that does not is a
    # fault. Same shape, different verdict, which is the whole point.
    gs = [[1.0, 0.5]]
    rs = [[1.0, 0.5]]
    m, t = ties([[7, 2]], gs, [[1, 2]], rs, 2)
    check("a miss at an identical score counts as a tie", (m, t) == (1, 1),
          "row 7 scored 1.0 and so did the row 1 it displaced")
    rs2 = [[0.9, 0.5]]
    m, t = ties([[7, 2]], gs, [[1, 2]], rs2, 2)
    check("a miss at a different score is NOT a tie", (m, t) == (1, 0),
          "1.0 returned where 0.9 was wanted is a fault, not a duplicate")
    m, t = ties([[1, 2]], gs, [[1, 2]], rs, 2)
    check("no misses means no ties", (m, t) == (0, 0))

    # 5. THE QUERY SET LOADER, added 2026-09-17 with --queries. SYNTHETIC, like
    # everything else in this function: three questions across two passes in a
    # temporary file, so the grouping is checked against a known answer rather
    # than against whatever happens to be in 00-docs on the day. A test that
    # reads the real set would pass for the wrong reason the moment the set is
    # regenerated with a different --n.
    import tempfile
    fixture = {"seed": "fixture-seed", "generated": "2026-09-17",
               "caveat": "a caveat that must reach the printed line",
               "queries": [{"q": "one?", "pass_": 1, "exclude_cid_prefix": "19:1:"},
                           {"q": "two?", "pass_": 2, "exclude_cid_prefix": "40:2:"},
                           {"q": "three?", "pass_": 1, "exclude_cid_prefix": "19:3:"}]}
    fd, fp = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(fixture, fh)
    try:
        qs, gr, prov = load_queries(fp)
        check("a query set loads in file order",
              qs == ["one?", "two?", "three?"], "%s" % qs)
        # BY RECORDED PASS, NOT BY POSITION. The hand-written set is grouped by
        # position because it was literally built in two halves. Carrying that
        # over would mislabel a sampled set the first time its order changed,
        # and the label is the whole reason the groups exist.
        check("groups are by recorded pass, not position",
              gr == [("pass 1 queries", [0, 2]), ("pass 2 queries", [1])],
              "%s" % gr)
        check("the groups partition the set exactly",
              sorted(i for _n, ix in gr for i in ix) == [0, 1, 2])
        # THE CAVEAT IS PART OF THE MEASUREMENT. This set covers 24 of 48
        # artifacts and none of the medical ones; a run whose header does not
        # say so produces a number that will be compared with one measured on
        # something else.
        check("the caveat reaches the line that gets printed",
              "must reach the printed line" in prov and "fixture-seed" in prov)
    finally:
        os.unlink(fp)

    print("\n%d/%d" % (TOTAL - bad, TOTAL))
    return 1 if bad else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[1])
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--configs", nargs="*", default=CONFIGS,
                    help="faiss index_factory strings")
    ap.add_argument("--depth", type=int, default=200,
                    help="candidates retrieved, and the rerank depth")
    ap.add_argument("--nprobe", type=int, default=16, help="IVF lists probed")
    ap.add_argument("--train", type=int, default=200_000,
                    help="training sample size")
    ap.add_argument("--work", default=DEFAULT_WORK,
                    help="scratch, and it must be OUTSIDE the archive")
    ap.add_argument("--with-chroma", action="store_true",
                    help="also score Chroma's HNSW against exact. Node DOWN.")
    ap.add_argument("--queries", default=None,
                    help="a query set from bin/index-queryset.py; default is "
                         "the hand-written questions in measure-retrieval.py")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()
    return selftest() if a.selftest else measure(a)


if __name__ == "__main__":
    sys.exit(main())
