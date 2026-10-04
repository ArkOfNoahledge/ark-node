#!/usr/bin/env python3
"""
index-pq-build.py - build the flat PQ64 index the node queries. Spec 8.4.

    python bin/index-pq-build.py --selftest     synthetic, needs no index
    python bin/index-pq-build.py --dry-run      what it would build, writes nothing
    python bin/index-pq-build.py                build it
    python bin/index-pq-build.py --verify       build, then check it can find itself
    python bin/index-pq-build.py --check        check an index already built

WHAT THIS IS, AND WHY THERE IS NOTHING TO MIGRATE

Chroma is not a corpus. It is a DERIVED index over `10-index/vectors/*.npy`, and
those files are the archive of record - float16, 2,048 bytes per chunk, row i is
chunk i, written by index-build.py for every artifact it has ever built. So this
tool does not convert anything and has no legacy path. It reads all 48 artifacts,
pass 1 and pass 2 alike, and builds ONE index over all of them.

That is the 2026-09-01 decision paying out: embeddings kept as plain files rather
than only inside Chroma is what left the index TYPE re-decidable, and it is why
replacing the vector store costs an hour of CPU instead of days of GPU.

    Chroma HNSW, measured   4,287 bytes/vector   29.7 GB at 6.93M
    flat PQ64                      64            443 MB at 6.93M

    r@5 0.983 (Chroma) against 0.975 (PQ64 + exact rerank) on 24 real queries.
    One slot in 120. bin/index-pq-probe.py, DECISIONS.md 2026-09-12.

THE RERANK IS NOT IN THIS FILE, DELIBERATELY. This builds the thing that
NOMINATES candidates; store.py scores them against the real float16 vectors. PQ64
alone is a poor index (r@5 0.425) and is only ever meant to hand 200 candidates to
something exact. A reader who finds this file and uses its output directly will
get the 0.425 index and no warning, so: the warning is here.

WHAT IT WRITES, into a CHECKSUMMED shelf

    10-index/pq/ark-pq64.faiss    the index
    10-index/pq/rows.npy          global row -> (src, dnum, i), int32, N x 3
    10-index/pq/manifest.json     the stamp, so staleness is detectable

`rows.npy` rather than a list of id strings: 6,926,271 ids as JSON is ~100 MB of
text and ~350 MB once Python holds the strings, on a node whose whole argument is
a 443 MB index. Three int32 columns is 83 MB, memory-mappable, and only the ~200
rows a query touches are ever paged in. The id string is reassembled on demand.

ON THE STAMP, AND WHY IT IS NOT THE ONE index-pq-probe.py USES

The probe stamps its row-map cache with [slug, n, size, MTIME]. That is right for
a cache which costs seconds to rebuild. It is wrong here: a cold-copy restore
rewrites every mtime without changing a byte, and this index costs tens of
minutes. So the stamp is [slug, n, size] plus the total - strong enough that an
artifact rebuilt to a different content would have to land on the same row count
AND the same byte size to slip through, and stable across a restore.

This matters because of what happened on 2026-09-14: `mirror-mdwiki.json` was
stamped against provenance.sqlite3's BYTE SIZE, so rebuilding provenance for an
unrelated reason invalidated it. A stamp should fire on the thing it describes
changing and on nothing else.
"""

import os, sys, json, time, argparse, importlib.util

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.environ.get("ARK_ROOT") or os.path.dirname(HERE)
INDEX = os.path.join(ROOT, "10-index")
OUT = os.path.join(INDEX, "pq")

SPEC = "PQ64"               # 64 bytes/vector. PQ32 is a cliff: r@5 0.633 and its
                            # recall INVERTS with depth. At scale the direction
                            # to explore is up, PQ96 or PQ128. DECISIONS 09-12.
TRAIN = 200_000             # vectors sampled evenly across the whole corpus


def bytes_per_vector(spec):
    """Code size READ FROM THE FACTORY STRING, not from a constant.

    The first version of this file printed "64 B/vector" for every spec,
    including the PQ8 its own fixture builds - a confident number derived from
    something other than the question asked. Caught by the fixture printing
    64 B/vector beside a 49.1 B/vector index on the very next line.

    A PQ<m> index stores m bytes per vector, and a prefix like OPQ64_1024 is a
    rotation that does not change the code size. Returns None when the string is
    not a plain PQ, in which case the estimate is not printed at all rather than
    guessed."""
    import re as _re
    m = _re.search(r"PQ(\d+)\s*$", spec.strip())
    return int(m.group(1)) if m else None


def probe():
    """index-pq-probe.py, imported rather than copied.

    artifacts(), stream(), normalize(), dim(), build() and need_faiss() are all
    proved there, three of them by fixtures that caught real defects - gather()
    ran off the end of the artifact list, and a rerank fixture passed without
    testing anything. A second copy here would drift from the one that was
    measured, and the measurement is the only reason to trust any of it.

    Same rule measure-spatial-cues.py follows with spatial.py and index-measure.py
    with index-build.py: import the one definition."""
    spec = importlib.util.spec_from_file_location(
        "index_pq_probe", os.path.join(HERE, "index-pq-probe.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    # The probe computes its own ROOT at import. Under --selftest ours is a
    # temporary tree, and a module pointing at the real 10-index would read the
    # real vectors and take an hour. Point it at ours, out loud.
    m.ROOT, m.INDEX = ROOT, INDEX
    return m


def stamp_of(arts):
    """[slug, rows, bytes] per artifact, plus the total. No mtime - see header."""
    return {"artifacts": [[a.slug, a.n, os.path.getsize(a.vectors)] for a in arts],
            "total": sum(a.n for a in arts)}


def read_manifest(out=None):
    """The manifest, or None for absent, or the string "unreadable".

    ABSENT AND CORRUPT ARE NOT THE SAME FACT and this returned None for both
    until 2026-09-15, so `--check` on a manifest that existed and would not
    parse said "index is absent - build it". store.py's _pq_plan() already drew
    the distinction; the tool that writes the file did not, which is the wrong
    way round. Found by a fixture that deliberately corrupted one."""
    f = os.path.join(out or OUT, "manifest.json")
    if not os.path.exists(f):
        return None
    try:
        return json.load(open(f, encoding="utf-8"))
    except Exception:                                          # noqa: BLE001
        return "unreadable"


def state(arts, out=None):
    """absent | stale | current, and never a bare False.

    THREE CAUSES THAT ARE NOT THE SAME FACT. store.py reports them separately
    for the same reason the 2026-09-12 mirror rule does: 'no dense results' meant
    four different things once, and one of them was a bug hiding behind the
    other three."""
    man = read_manifest(out)
    if man == "unreadable":
        return "error", None
    if man is None:
        return "absent", None
    if not os.path.exists(os.path.join(out or OUT, man.get("index", ""))):
        return "absent", man
    return ("current" if man.get("stamp") == stamp_of(arts) else "stale"), man


# ---------------------------------------------------------------------------

def write_rows(p, arts, pq, quiet=False):
    """global row -> (src, dnum, i) as int32, streamed from the chunk files.

    The chunk id index-build.py writes is "src:dnum:i" - three integers. Stored
    as three columns rather than as text, and asserted against the vector count
    per artifact, because a row map that is off by one for ONE artifact is wrong
    for every artifact after it and produces plausible citations for the wrong
    passages. index-build.py asserts this on the way in; asserting it again here
    is what makes row k of the .npy trustworthy as chunk k."""
    import numpy as np
    total = sum(a.n for a in arts)
    rows = np.empty((total, 3), dtype=np.int32)
    at, t0 = 0, time.time()
    for a in arts:
        n = 0
        with open(a.chunks, encoding="utf-8") as fh:
            for line in fh:
                cid = json.loads(line)["id"]
                parts = cid.split(":")
                if len(parts) != 3:
                    raise SystemExit("%s: chunk id %r is not src:dnum:i - this "
                                     "tool stores ids as three integers and "
                                     "cannot represent that" % (a.file, cid))
                rows[at] = [int(parts[0]), int(parts[1]), int(parts[2])]
                at += 1
                n += 1
        if n != a.n:
            raise SystemExit("%s: %d jsonl lines but %d vectors - the row map "
                             "would be wrong for every artifact after this one"
                             % (a.file, n, a.n))
    if at != total:
        raise SystemExit("row map is %d rows, expected %d" % (at, total))
    np.save(p, rows)
    if not quiet:
        print("  rows         %s x 3 int32, %s in %.1fs"
              % ("{:,}".format(total), human(rows.nbytes), time.time() - t0))
    return rows


def human(n):
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return "%.1f %s" % (n, u)
        n /= 1024.0


def verify(pq, faiss, ix, arts, rows, n=64, depth=10, quiet=False):
    """Can the index find the vectors it was built from?

    NOT A RECALL MEASUREMENT, and saying so matters. This searches for vectors
    that are IN the index and asks whether each comes back near the top of its
    own result list. It catches a wrong row map, a truncated add, a metric
    mix-up and a corrupt file. It cannot tell you what recall this index has on
    real questions - that is index-pq-probe.py, against exhaustive search, and
    the answer there was r@5 0.975 WITH the rerank this file does not do.

    A self-identity check that passes proves the plumbing. It proves nothing
    about retrieval quality, and a build tool that reported it as though it did
    would be the defect this build has recorded six times in a week."""
    import numpy as np
    total = sum(a.n for a in arts)
    step = max(1, total // n)
    want = np.arange(0, total, step)[:n]
    Q = pq.normalize(pq.gather(arts, list(want)))
    _d, got = ix.search(Q, depth)
    hit1 = int(sum(1 for k, g in zip(want, got) if g[0] == k))
    hitk = int(sum(1 for k, g in zip(want, got) if k in g))
    if not quiet:
        print("  self-check   %d/%d at rank 1, %d/%d within top %d"
              % (hit1, len(want), hitk, len(want), depth))
        print("               (plumbing only - real recall is index-pq-probe.py)")
    return hit1, hitk, len(want)


def do_check(a):
    """The self-check on an index that already exists, with WHERE the misses are.

    ADDED 2026-09-15 BECAUSE --verify REPORTED A COUNT AND NOTHING ELSE. The
    first real build scored 58/64 at rank 1, and a bare count cannot separate
    the two explanations that matter:

      quantization noise   misses scattered through the corpus, which is what
                           PQ64 alone does at 6.9M and is harmless, because the
                           node reranks the top 200 and never uses rank 1 alone
      a structural fault   misses clustered - all in one artifact, or all past
                           some row - which would mean a truncated add or a row
                           map that slipped, and every citation past that point
                           naming the wrong passage

    Those need opposite responses and the count reads the same for both. This
    reports where each miss sits in the corpus, which artifact it came from, and
    how many would survive a rerank at --depth.

    IT IS STILL NOT A RECALL MEASUREMENT. Searching for vectors that are IN the
    index is an easier question than answering a real one. index-pq-probe.py
    against exhaustive search is the honest number."""
    pq = probe()
    faiss = pq.need_faiss()
    import bisect
    arts = pq.artifacts()
    st, man = state(arts, a.out)
    if st != "current":
        sys.exit("index is %s - %s" % (st, {
            "absent": "build it:  python bin/index-pq-build.py",
            "error": "its manifest.json will not parse. Rebuild: "
                     "python bin/index-pq-build.py --force",
        }.get(st, "rebuild:   python bin/index-pq-build.py --force")))
    ix = faiss.read_index(os.path.join(a.out, man["index"]))
    total = sum(x.n for x in arts)
    step = max(1, total // a.sample)
    want = list(range(0, total, step))[:a.sample]

    Q = pq.normalize(pq.gather(arts, want))
    t0 = time.time()
    _d, got = ix.search(Q, a.depth)
    el = time.time() - t0

    bases = [x.base for x in arts]
    pos, misses, sampled = [], [], {}
    for k, row in zip(want, got):
        ai = bisect.bisect_right(bases, k) - 1
        sampled[arts[ai].file] = sampled.get(arts[ai].file, 0) + 1
        r = list(row)
        p = r.index(k) if k in r else -1
        pos.append(p)
        if p != 0:
            misses.append((k, p, arts[ai].file, k / float(total), int(r[0])))

    at1 = sum(1 for p in pos if p == 0)
    inlist = sum(1 for p in pos if p >= 0)
    print("self-check over %s sampled rows, depth %d" % ("{:,}".format(len(want)), a.depth))
    print("  index        %s, %s vectors" % (man["index"], "{:,}".format(ix.ntotal)))
    print("  rank 1       %d/%d  (%.1f%%)   PQ64 ALONE, which the node never uses"
          % (at1, len(want), 100.0 * at1 / len(want)))
    print("  within %-5d %d/%d  (%.1f%%)   <- what the rerank actually receives"
          % (a.depth, inlist, len(want), 100.0 * inlist / len(want)))
    print("  latency      %.1f ms/query" % (1000.0 * el / len(want)))

    if not misses:
        print("")
        print("  every sampled row came back first.")
        return 0

    # WHAT OUTRANKED IT, SCORED EXACTLY. This is the question a count cannot
    # answer and the whole reason --check exists.
    #
    # If the row that took first place is a NEAR-DUPLICATE of the query - the
    # same passage in another artifact, a translation, a mirrored article - it
    # scores ~1.0 against it and the "miss" is semantically nothing: two rows
    # said the same thing and quantization could not separate them. If it scores
    # low, the index or the row map is wrong and the citation would name a
    # passage that does not answer the question.
    #
    # Those two need opposite responses and until 2026-09-15 this tool reported
    # them identically, then guessed between them from how the misses were
    # spread. It guessed WRONG on the first real index: it called 48 scattered
    # misses "clustered" because ONE row of 512 fell outside the window.
    usurp = pq.normalize(pq.gather(arts, [m[4] for m in misses]))
    qrows = {k: i for i, k in enumerate(want)}
    sims = [float(usurp[i] @ Q[qrows[m[0]]]) for i, m in enumerate(misses)]
    sims_sorted = sorted(sims)
    med = sims_sorted[len(sims_sorted) // 2]
    near = sum(1 for x in sims if x >= 0.95)

    print("")
    print("  %d rows did not come back first." % len(misses))
    print("  WHAT TOOK FIRST PLACE, scored exactly against the query:")
    print("    median %.3f    min %.3f    max %.3f" % (med, sims_sorted[0], sims_sorted[-1]))
    print("    %d of %d score >= 0.95, i.e. are near-duplicates of the query"
          % (near, len(sims)))

    tenths = [0] * 10
    for m in misses:
        tenths[min(9, int(m[3] * 10))] += 1
    print("")
    print("  by position in the corpus, tenth by tenth:  %s"
          % " ".join("%d" % t for t in tenths))
    # RATE, NOT COUNT. An evenly spaced sample gives each artifact rows in
    # proportion to its size, so a raw count says more about how big an artifact
    # is than about how it behaves. The first version of this printed counts and
    # made physics.stackexchange (79 samples, 4 misses, 5%) look comparable to
    # mdwiki (22 samples, 17 misses, 77%).
    byart = {}
    for m in misses:
        byart[m[2]] = byart.get(m[2], 0) + 1
    print("  %-46s %7s %8s %7s" % ("artifact", "misses", "sampled", "rate"))
    for f, c in sorted(byart.items(), key=lambda kv: -kv[1] / float(max(1, sampled.get(kv[0], 1))))[:8]:
        n = sampled.get(f, 0)
        print("  %-46s %7d %8d %6.0f%%" % (f[:46], c, n, 100.0 * c / max(1, n)))
    outside = [m for m in misses if m[1] < 0]
    if outside:
        print("  %d of %d were not in the top %d at all (%.1f%%)"
              % (len(outside), len(want), a.depth, 100.0 * len(outside) / len(want)))

    print("")
    # THE VERDICT, FROM THE SIMILARITIES RATHER THAN FROM THE SPREAD.
    if near >= 0.8 * len(sims):
        print("  NOT A FAULT. %d of %d misses were beaten by a near-duplicate - the"
              % (near, len(sims)))
        print("  same passage in another artifact, a translation, or a mirrored")
        print("  article. Quantization cannot separate two vectors that are the same")
        print("  vector, and the rerank does not need it to: both rows say the same")
        print("  thing. Expect this to concentrate in the artifacts the archive")
        print("  already excludes duplicates from.")
    elif med < 0.7:
        print("  FAULT. The rows taking first place are NOT similar to the query")
        print("  (median %.3f). That is a row map that slipped or a truncated add," % med)
        print("  and every citation past the first disagreement names the wrong")
        print("  passage. Do NOT wire this index into store.py.")
    else:
        print("  UNCLEAR at median %.3f. Not obviously duplicates and not obviously" % med)
        print("  wrong. Widen --sample before deciding, and compare against")
        print("  index-pq-probe.py on real questions.")
    if 100.0 * len(outside) / len(want) > 2.0:
        print("")
        print("  SEPARATELY: %.1f%% fell outside the depth-%d window, which is the"
              % (100.0 * len(outside) / len(want), a.depth))
        print("  number that costs recall no rerank can recover. Raise --depth.")
    return 0


def do_build(a):
    pq = probe()
    faiss = pq.need_faiss()
    arts = pq.artifacts()
    total = sum(x.n for x in arts)
    d = pq.dim(arts)
    st, man = state(arts, a.out)

    print("Ark PQ index build")
    print("  index        %s" % INDEX)
    print("  out          %s" % a.out)
    print("  artifacts    %d, %s vectors, %d dims" % (len(arts), "{:,}".format(total), d))
    bpv = bytes_per_vector(a.spec)
    print("  spec         %s%s"
          % (a.spec, ("   ->  about %s at %d B/vector, before overhead"
                      % (human(total * bpv), bpv)) if bpv else
                     "   (code size not derivable from this factory string)"))
    print("  to read      %s of float16 vectors"
          % human(sum(os.path.getsize(x.vectors) for x in arts)))
    print("  existing     %s%s" % (st, "" if st == "absent" else
                                   " (built %s over %s vectors)"
                                   % (man.get("built", "?"),
                                      "{:,}".format((man.get("stamp") or {}).get("total", 0)))))

    if a.dry_run:
        print("\nDRY RUN - nothing is written")
        if st == "stale":
            print("  the existing index does NOT match the vectors on disk and would be rebuilt")
        elif st == "current":
            print("  the existing index matches; a real run would refuse without --force")
        return 0

    if st == "current" and not a.force:
        print("\n  up to date. Nothing to do. --force rebuilds anyway.")
        return 0

    os.makedirs(a.out, exist_ok=True)
    t_all = time.time()

    rows = write_rows(os.path.join(a.out, "rows.npy"), arts, pq)
    ix, metric, add_s = pq.build(faiss, arts, a.spec, total, a.train)
    print("  index        %s vectors added in %.1fs, metric %s"
          % ("{:,}".format(ix.ntotal), add_s, metric))
    if ix.ntotal != total:
        raise SystemExit("faiss holds %d vectors but the corpus has %d - "
                         "refusing to write an index whose rows do not line up "
                         "with rows.npy" % (ix.ntotal, total))

    ipath = os.path.join(a.out, "ark-%s.faiss" % a.spec.lower().replace(",", "-"))
    faiss.write_index(ix, ipath)
    size = os.path.getsize(ipath)
    print("  wrote        %s  (%s, %.1f B/vector)"
          % (os.path.basename(ipath), human(size), size / float(total)))

    vres = None
    if a.verify:
        vres = verify(pq, faiss, ix, arts, rows)

    json.dump({
        "built": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "spec": a.spec, "metric": metric, "dim": d,
        "index": os.path.basename(ipath), "rows": "rows.npy",
        "bytes_per_vector": round(size / float(total), 1),
        "train_vectors": min(a.train, total),
        "stamp": stamp_of(arts),
        # THE RERANK IS NOT OPTIONAL AND THE MANIFEST SAYS SO. A reader who
        # wires this index up without one gets r@5 0.425 and no error.
        "requires_rerank": True,
        "rerank_against": "10-index/vectors/*.npy (float16, row i = chunk i)",
        "measured": {"note": "index-pq-probe.py, 24 real queries, 2.3M vectors, 2026-09-12",
                     "pq64_rerank_r_at_5": 0.975, "chroma_r_at_5": 0.983},
        "self_check": ({"rank1": vres[0], "within_top10": vres[1], "of": vres[2]}
                       if vres else None),
    }, open(os.path.join(a.out, "manifest.json"), "w", encoding="utf-8"), indent=1)

    print("\n  done in %.1fs" % (time.time() - t_all))
    # THIS ADVICE WAS WRONG IN TWO WAYS UNTIL 2026-09-25, and both are the shape
    # this build keeps recording: a sentence true when written that outlived the
    # thing it named. Corrected rather than deleted, because the correction is
    # the more useful half.
    #
    # IT SAID "Chroma is NOT retired yet: keep it until store.py has answered
    # real questions from this index. `index-build.py --load-only` rebuilds it."
    # Chroma was retired 2026-09-16 - 37,140,488,678 bytes returned - and
    # 10-index/chroma does not exist. So the last line this tool printed told the
    # operator to keep a directory that is gone AND named the one command
    # bin/index-scope-pass3.txt spends a paragraph warning about: load_chroma()
    # does os.makedirs on that directory and opens a PersistentClient, and at
    # Chroma's 4,287 measured bytes per vector this index's 39.1M rows are about
    # 168 GB on a 64 GB machine. It is the kind of line someone follows at 2am
    # because the tool said so.
    #
    # The USEFUL half of that sentence was the caution underneath it - do not
    # trust an index that has not served - and it survives precisely BECAUSE the
    # fallback does not. That is what is printed now.
    #
    # AND IT SAID "NOW RUN: bash bin/rehash.sh 10-index" with no ordering. True
    # about the shelf and wrong about the sequence: this tool runs inside a
    # closeout where bm25.sqlite3 and provenance.sqlite3 are rebuilt AFTERWARDS,
    # by separate tools, into the same shelf. On 2026-09-25 this index covered
    # 39,073,563 chunks while both of those still held 6,926,271. A manifest
    # taken between them is stale before it is read. rehash.sh is the LAST step
    # of a closeout, never the first.
    if os.environ.get("ARK_SETUP"):
        return 0
    print("  This wrote into a CHECKSUMMED shelf, so 10-index has to be rehashed -")
    print("  but LAST, once nothing else will write there. bm25.sqlite3 and")
    print("  provenance.sqlite3 are rebuilt by separate tools and lag this one.")
    print("")
    print("      python bin/ark.py rehash 10-index")
    if os.path.isfile(os.path.join(ROOT, "MANIFEST.csv")):
        print("      then update the 10-index row in MANIFEST.csv from what it reports")
    print("")
    print("  AND THERE IS NO LONGER A FALLBACK. Chroma was the second vector store")
    print("  if this index disappointed; it was retired 2026-09-16 and is gone. So")
    print("  verify before trusting, not after - the self-check above is plumbing,")
    print("  not recall:")
    print("")
    print("      python bin/index-pq-probe.py --configs PQ64 --depth 400")
    return 0


# ---------------------------------------------------------------------------

def selftest():
    """Synthetic vectors, a synthetic registry, no faiss training shortcuts.

    Builds a real index over a real (tiny) corpus on disk, which is the only way
    to test the row map, the stamp and the vector/jsonl agreement at once."""
    import tempfile, shutil
    import numpy as np
    global ROOT, INDEX, OUT
    keep = (ROOT, INDEX, OUT)
    ok = fail = 0

    def check(label, cond):
        nonlocal ok, fail
        if cond:
            ok += 1; print("  ok    %s" % label)
        else:
            fail += 1; print("  FAIL  %s" % label)

    tmp = tempfile.mkdtemp()
    try:
        ROOT = tmp
        INDEX = os.path.join(tmp, "10-index")
        OUT = os.path.join(INDEX, "pq")
        os.environ["ARK_ROOT"] = tmp
        for sub in ("chunks", "vectors"):
            os.makedirs(os.path.join(INDEX, sub))

        rng = np.random.default_rng(7)
        D, reg = 32, {"artifacts": {}}
        plan = [("a.zim", 400), ("b.zim", 250), ("c.zim", 150)]
        for sid, (name, n) in enumerate(plan, start=1):
            v = rng.normal(size=(n, D)).astype(np.float16)
            np.save(os.path.join(INDEX, "vectors", name + ".f16.npy"), v)
            with open(os.path.join(INDEX, "chunks", name + ".jsonl"),
                      "w", encoding="utf-8", newline="\n") as fh:
                for i in range(n):
                    fh.write(json.dumps({"id": "%d:%d:%d" % (sid, i // 3, i % 3),
                                         "src": sid, "text": "x"}) + "\n")
            reg["artifacts"][str(sid)] = {"file": name, "chunks": n}
        json.dump(reg, open(os.path.join(INDEX, "sources.json"), "w", encoding="utf-8"))

        pq = probe()
        arts = pq.artifacts()
        check("artifacts in build order with contiguous bases",
              [x.base for x in arts] == [0, 400, 650] and sum(x.n for x in arts) == 800)

        # 1. STATE IS THREE THINGS, NOT A BOOLEAN.
        check("no index -> 'absent'", state(arts, OUT)[0] == "absent")

        # 2. The row map must agree with the vectors, per artifact.
        rows = write_rows(os.path.join(tmp, "rows.npy"), arts, pq, quiet=True)
        check("row map is one row per vector", rows.shape == (800, 3))
        check("row 0 is artifact 1 chunk 0", list(rows[0]) == [1, 0, 0])
        check("row 400 is the first row of artifact 2", list(rows[400]) == [2, 0, 0])
        check("row 650 is the first row of artifact 3", list(rows[650]) == [3, 0, 0])

        # 3. A JSONL THAT DISAGREES WITH ITS VECTORS MUST RAISE, NOT TRUNCATE.
        #    Off by one in one artifact is wrong for every artifact after it.
        bad = os.path.join(INDEX, "chunks", "b.zim.jsonl")
        with open(bad, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps({"id": "2:99:9"}) + "\n")
        try:
            write_rows(os.path.join(tmp, "bad.npy"), arts, pq, quiet=True)
            check("jsonl/vector mismatch raises", False)
        except SystemExit:
            check("jsonl/vector mismatch raises", True)
        with open(bad, encoding="utf-8") as fh:
            lines = fh.readlines()
        open(bad, "w", encoding="utf-8", newline="\n").writelines(lines[:-1])

        # 4. Build for real.
        args = argparse.Namespace(out=OUT, spec="PQ8", train=800, force=False,
                                  verify=True, dry_run=False, check=False,
                                  sample=64, depth=10)
        rc = do_build(args)
        check("build returns 0", rc == 0)
        man = read_manifest(OUT)
        check("manifest records the spec", man and man["spec"] == "PQ8")
        check("manifest says the rerank is required", man and man["requires_rerank"] is True)
        check("rows.npy written beside the index",
              os.path.exists(os.path.join(OUT, "rows.npy")))

        # 5. STAMP: current now, stale when a vector file changes, and NOT stale
        #    when only an mtime moves - which is what a cold-copy restore does.
        check("built index -> 'current'", state(arts, OUT)[0] == "current")

        # A refusal that protects a good index from a pointless rebuild. CHECKED
        # HERE, WHILE THE CORPUS IS STILL CONSISTENT: the first version of this
        # fixture ran it last, after the staleness case had deliberately left
        # the vectors and the jsonl disagreeing, so do_build correctly raised on
        # the corruption and the check never ran. The tool was right and the
        # test was wrong, which is the harder of the two to notice.
        check("a current index is not rebuilt without --force",
              do_build(argparse.Namespace(out=OUT, spec="PQ8", train=800,
                                          force=False, verify=False,
                                          dry_run=False)) == 0)
        check("--dry-run on a current index writes nothing and says so",
              do_build(argparse.Namespace(out=OUT, spec="PQ8", train=800,
                                          force=False, verify=False,
                                          dry_run=True)) == 0)
        vf = os.path.join(INDEX, "vectors", "c.zim.f16.npy")
        os.utime(vf, (time.time() + 10_000, time.time() + 10_000))
        check("an mtime change alone is NOT stale (survives a cold-copy restore)",
              state(pq.artifacts(), OUT)[0] == "current")
        v = np.load(vf)
        np.save(vf, np.vstack([v, rng.normal(size=(5, D)).astype(np.float16)]))
        check("more vectors -> 'stale'", state(pq.artifacts(), OUT)[0] == "stale")

        # ABSENT AND CORRUPT ARE NOT THE SAME FACT, and this returned "absent"
        # for both until a fixture corrupted one and read the advice.
        man_p = os.path.join(OUT, "manifest.json")
        good = open(man_p, encoding="utf-8").read()
        open(man_p, "w", encoding="utf-8").write("{not json")
        check("an unreadable manifest is 'error', not 'absent'",
              state(pq.artifacts(), OUT)[0] == "error")
        open(man_p, "w", encoding="utf-8").write(good)

        # 6. And the estimate comes from the spec rather than from a constant.
        check("PQ64 -> 64 B/vector", bytes_per_vector("PQ64") == 64)
        check("PQ32 -> 32 B/vector", bytes_per_vector("PQ32") == 32)
        check("an OPQ rotation does not change the code size",
              bytes_per_vector("OPQ64_1024,PQ64") == 64)
        check("a non-PQ factory string returns None rather than 64",
              bytes_per_vector("SQ8") is None and bytes_per_vector("IVF4096,Flat") is None)
    finally:
        ROOT, INDEX, OUT = keep
        os.environ.pop("ARK_ROOT", None)
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n  %d/%d" % (ok, ok + fail))
    return 0 if not fail else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=OUT)
    ap.add_argument("--spec", default=SPEC,
                    help="faiss factory string. Default PQ64 - see DECISIONS 09-12 "
                         "for why not PQ32, not OPQ and not IVF")
    ap.add_argument("--train", type=int, default=TRAIN)
    ap.add_argument("--force", action="store_true",
                    help="rebuild even when the stamp says the index is current")
    ap.add_argument("--verify", action="store_true",
                    help="after building, check the index can find its own vectors. "
                         "PLUMBING ONLY - real recall is index-pq-probe.py")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would be built and write nothing")
    ap.add_argument("--check", action="store_true",
                    help="self-check an index already built, reporting WHERE the "
                         "misses are rather than only how many")
    ap.add_argument("--sample", type=int, default=512, help="--check only: rows to probe")
    ap.add_argument("--depth", type=int, default=200,
                    help="--check only: the rerank window store.py will use")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if a.check:
        return do_check(a)
    return do_build(a)


if __name__ == "__main__":
    sys.exit(main())
