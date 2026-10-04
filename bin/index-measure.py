#!/usr/bin/env python3
"""
index-measure.py - how big is the index actually going to be?

Reads bin/index-scope-pass1.txt, opens every ZIM with libzim and every PDF with
pymupdf, and reports REAL text volume, chunk counts, and the RAM a Chroma HNSW
index would need to hold them.

    python bin/index-measure.py                 # sample - minutes
    python bin/index-measure.py --full          # read every article - slow, exact
    python bin/index-measure.py --sample 5000   # bigger sample

WHY THIS EXISTS, AND WHY IT RUNS BEFORE ANY EMBEDDING.

Research on 2026-09-01 found that Chroma's HNSW index must be FULLY RESIDENT IN
RAM. Chroma's own sizing for 1024-dimension vectors - which is what BGE-M3 dense
produces - is roughly 1.7 million chunks per 8GB. So the chunk count is not a
curiosity, it is the number that decides whether the architecture is viable on a
solar-powered node at all. Guessing it from ZIM file sizes is worthless: a ZIM is
compressed and mostly images, and the ratio of payload to text differs by an order
of magnitude between iFixit (photographs) and a Stack Exchange dump (nearly all
prose).

So: measure, then choose. The alternative is discovering the number after three
days of embedding.

WHAT IT REFUSED TO TELL YOU UNTIL 2026-09-13, AND WHY THAT CHANGED. This file
used to end its report with "NOT MEASURED HERE: embedding throughput ... a figure
borrowed from someone else's hardware would be precisely the kind of confident
wrong number this build keeps catching." That was correct, and it was correct
because no record of this machine's own throughput existed.

One exists now. `10-index/sources.json` carries `seconds`, `seconds_embed` and
`seconds_read_chunk` for every artifact ever built here - 37 of them as of
2026-09-13. So the refusal is lifted exactly as far as that record reaches, and
no further: `timing_model()` DERIVES the rates from it and returns None when the
registry is absent, which makes the report fall back to the old refusal rather
than to a guess.

THE REFUSAL WAS BEING ROUTED AROUND ANYWAY, WHICH IS THE REAL REASON THIS EXISTS.
The 2026-09-13 CORE measurement was written into DECISIONS, BUILD-LOG, ROADMAP
and the scope files as "192.6 h GPU", and pass 2 as "8.0 h". Neither number came
from this tool. Both were `chunks / 162` computed by hand, and 162 is an
EMBEDDING rate. Extraction is 42% of wall clock across those 37 artifacts, so
every hour in the record was low by about half. Pass 2 was quoted at 8.0 h and is
landing near 12.2. Pass 1's real wall-clock rate - 107 chunks/s over 29 artifacts
and six hours - had been sitting in sources.json since 2026-09-03, unread.

A tool that declines to answer a question does not stop the question being asked.
It just moves the answer somewhere with no calibration on it.
"""

import os, sys, random, json, re

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCOPE = os.path.join(HERE, "index-scope-pass1.txt")
if "--scope" in sys.argv:
    # A SECOND SCOPE FILE rather than editing this constant per run, which is
    # how a measurement ends up attributed to the wrong corpus.
    SCOPE = sys.argv[sys.argv.index("--scope") + 1]
    if not os.path.isabs(SCOPE):
        SCOPE = os.path.join(HERE, SCOPE)

# Chunking assumption, stated so it can be argued with rather than discovered.
# 512 tokens is the common default for BGE-M3 retrieval; ~4 characters per token
# is the usual English rule of thumb and Spanish runs slightly longer. 15% overlap
# is the LlamaIndex SentenceSplitter default territory.
CHARS_PER_CHUNK = 2048
OVERLAP = 0.15
DIM = 1024                 # BGE-M3 dense
CHROMA_MCHUNKS_PER_GB = 0.245   # Chroma's own figure for 1024-dim, see DECISIONS
# MEASURED rather than published: flat faiss PQ64 writes 64 bytes per vector, and
# with an exact rerank against vectors/*.npy it reached r@5 0.975 against Chroma's
# own measured 0.983 on 24 real queries. bin/index-pq-probe.py, DECISIONS.md
# 2026-09-12. This is the figure a scope decision turns on now, not the Chroma one.
PQ_BYTES_PER_VECTOR = 64


def human(n):
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return "%.1f %s" % (n, u)
        n /= 1024.0


def load_scope():
    out = []
    for line in open(SCOPE, encoding="utf-8"):
        line = line.split("#", 1)[0].strip()
        if line:
            out.append(line)
    return out


def _entry_at(a, i):
    """libzim 3.12 renamed this to _get_entry_by_id. Probed and confirmed on the
    build machine 2026-09-01. Do NOT wrap the lookup in a bare except: an earlier
    version of this script swallowed the AttributeError and reported "0 html" for
    all 26 ZIMs, which read as a corpus finding rather than as a bug."""
    g = getattr(a, "_get_entry_by_id", None) or getattr(a, "get_entry_by_id", None)
    if g is None:
        raise RuntimeError("libzim Archive exposes no entry-by-id accessor")
    return g(i)


_SCRIPT = re.compile(r"(?is)<(script|style|noscript)\b.*?</\1>")
_TAG = re.compile(r"(?s)<[^>]+>")
_WS = re.compile(r"\s+")


def html_to_text(b):
    """Actual text length, measured rather than assumed.

    The previous version multiplied raw HTML bytes by a guessed 0.55 "text
    fraction". That guess was carrying 76% of the project's index-size estimate,
    and it produced figures that expand a compressed ZIM by 11x - arithmetically
    impossible for a file that also holds images. Measuring costs a regex pass
    and removes the guess. This is deliberately crude (no bs4, no lxml): it
    overshoots slightly on entity-heavy pages, which is the safe direction."""
    try:
        t = b.decode("utf-8", "ignore")
    except Exception:
        return 0
    t = _SCRIPT.sub(" ", t)
    t = _TAG.sub(" ", t)
    return len(_WS.sub(" ", t).strip())


def _pct(v, q):
    if not v:
        return 0
    v = sorted(v)
    return v[min(len(v) - 1, int(len(v) * q))]


_BUILDER = []


def builder():
    """index-build.py itself, imported once.

    THE ESTIMATOR NOW RUNS THE BUILDER'S OWN PIPELINE ON A SAMPLE rather than
    approximating it, and the 2026-09-12 calibration is why. Scored against the
    index that actually got built, the approximation was wrong three separate
    ways and two of them cancelled in the total:

      * its `html_to_text` was a three-line tag strip against the builder's
        `ark-html-strip/3` - nesting-aware boilerplate, htdig_noindex blocks,
        reference lists, navboxes, entity decoding, rendered tables. It counted
        1.67x to 2.36x too much text on wiki ZIMs and 0.38x to 0.55x too little
        on Stack Exchange.
      * it divided characters by a constant 2048 per chunk, i.e. 4.00 characters
        per BGE-M3 token, for every corpus. The measured figure is 2.07 for
        iFixit, 2.60 for homebrew, 3.26 for mdwiki. Never 4.00.
      * it had no per-document floor, and predicted 213,396 chunks for iFixit
        across 263,337 documents - fewer chunks than documents, impossible by
        construction.

    So chunks are no longer derived. Each sampled document is passed through the
    builder's extractor and its real SentenceSplitter, and the chunks are
    counted. The cost is that this tool now needs the keeper venv rather than
    libzim alone, which is what every other tool here already needs."""
    if not _BUILDER:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "index_build", os.path.join(HERE, "index-build.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        for need in ("html_to_text", "make_splitter", "MIN_DOC_CHARS", "pdf_document"):
            if not hasattr(mod, need):
                raise RuntimeError("index-build.py no longer defines %s" % need)
        _BUILDER.append(mod)
    return _BUILDER[0]


def measure_zim(path, full, sample):
    """Text, documents and chunks in one archive, by running the real pipeline.

    REIMPLEMENTED 2026-09-12. The sampling loop this is built around was DELETED
    by the 2026-09-02 density fix - the edit DECISIONS.md records as correcting
    the largest estimation error in the project. It replaced a region and took
    the sampler with it, leaving eleven lines referencing seven names nothing
    defined. The tell was an unused `import random`. The tool raised NameError
    on its first ZIM for ten days while three STATE documents named it as the
    next action. `bin/` entered version control three days after that edit, so
    neither history nor either cold copy can recover the original.

    HOW ENTRIES ARE CHOSEN, AND THE FIRST VERSION OF THIS GOT IT WRONG. When an
    archive was small enough to walk whole it used `range(ec)` and marked itself
    exhaustive - but the loop still breaks once it has `sample` documents, and a
    range is ORDERED. Seventeen artifacts were therefore scored on their first
    N entries in ZIM storage order, which is grouped by namespace and
    systematically unrepresentative. Outdoors read 8.2% HTML density where the
    truth is about 34%. Measured on 2026-09-12: artifacts that stopped early
    spanned 0.29x to 1.94x against the built index, and the eight that ran to
    the end of their id list spanned 0.54x to 1.14x.

    So the ids are SHUFFLED whenever stopping early is possible, and
    `exhaustive` now means the walk actually finished rather than that it was
    permitted to.

    Documents are still counted by HTML DENSITY and never by `article_count`;
    that is the 2026-09-02 finding and it survived calibration intact.
    Redirects count toward `consumed` and never toward `lens`, because
    `entry_count` includes them and excluding them from the denominator would
    inflate every estimate in the archive."""
    from libzim.reader import Archive
    import fitz
    ib = builder()
    sp = ib.make_splitter()
    floor = ib.MIN_DOC_CHARS

    a = Archive(path)
    n = a.article_count
    ec = a.entry_count
    get = getattr(a, "_get_entry_by_id", None) or getattr(a, "get_entry_by_id", None)
    if get is None:
        raise RuntimeError("libzim Archive exposes no entry-by-id accessor")

    # The cap bounds the walk on an archive that is mostly media - iFixit yields
    # text worth 0.04x its file size - without which "sample 2000 documents" can
    # mean reading a million entries.
    cap = min(ec, max(int(sample) * 50, 20000))
    if full:
        ids = range(ec)
    else:
        ids = random.sample(range(ec), cap)      # SHUFFLED, always. See above.

    lens, chunks = [], 0
    consumed = 0
    pdf_n = pdf_chars = pdf_bytes = pdf_no_text = pdf_chunks = 0
    completed = True
    for i in ids:
        if not full and len(lens) >= sample:
            completed = False
            break
        consumed += 1
        try:
            e = get(i)
            if e.is_redirect:
                continue
            item = e.get_item()
            mime = str(item.mimetype).split(";")[0]
        except Exception:                                # noqa: BLE001
            continue
        try:
            if mime.startswith("text/html"):
                text, _title = ib.html_to_text(bytes(item.content))
                if len(text) < floor:
                    continue
                lens.append(len(text))
                chunks += len(sp.split_text(text))
            elif mime == "application/pdf":
                pdf_n += 1
                data = bytes(item.content)
                pdf_bytes += len(data)
                doc = fitz.open(stream=data, filetype="pdf")
                full_text, _pmap = ib.pdf_document(doc)
                doc.close()
                if len(full_text) < floor:
                    pdf_no_text += 1
                    continue
                pdf_chars += len(full_text)
                pdf_chunks += len(sp.split_text(full_text))
        except Exception:                                # noqa: BLE001
            continue

    exhaustive = completed and consumed >= ec
    mean = (sum(lens) / float(len(lens))) if lens else 0
    density = len(lens) / float(max(1, consumed))
    est_docs = int(density * ec)

    # SCALED BY THE FRACTION OF ENTRIES EXAMINED, which is the only quantity the
    # walk actually observes. Chunks are counted, never derived from characters.
    seen = consumed / float(max(1, ec))
    grow = (1.0 / seen) if (not exhaustive and seen) else 1.0
    est_chunks = int((chunks + pdf_chunks) * grow)
    est_pdf_chars = int(pdf_chars * grow)

    info = {"entry_count": ec, "exhaustive": exhaustive,
            "mean_text": int(mean), "median_text": _pct(lens, 0.5),
            "p90_text": _pct(lens, 0.9), "max_text": max(lens) if lens else 0,
            "pdf_entries_seen": pdf_n, "pdf_bytes_seen": pdf_bytes,
            "pdf_chars": est_pdf_chars, "pdf_no_text": pdf_no_text,
            "chars": int(mean * est_docs) + est_pdf_chars,
            "articles": est_docs,
            "article_count_reported": n,
            "html_density": round(density, 4),
            "chunks_direct": est_chunks,
            "chunks_per_doc": round((chunks / float(len(lens))) if lens else 0, 2)}
    info["method"] = ("full" if exhaustive else
                      "sample(%d html of %d entries, %.1f%% html -> ~%s docs%s)"
                      % (len(lens), consumed, density * 100,
                         "{:,}".format(est_docs),
                         ", %d PDFs" % pdf_n if pdf_n else ""))
    return info


def measure_pdfs(d, full, sample):
    """On-disk PDF collections. Same correction as the ZIM path, same reason.

    It summed per-page text and divided by a characters-per-chunk constant. The
    builder does neither: a PDF is ONE document with a page map, not N page
    documents (2026-09-01, after a water-purification procedure was split across
    a page break with no overlap), and it chunks with the real splitter. So this
    now calls `pdf_document` and counts.

    The BYTE-scaled file sample is unchanged and is still the weak part: `fao`
    scored 0.24x against the built index on 2026-09-12 and six of its
    seventy-six files carry that estimate. Recorded, not fixed here."""
    import fitz  # pymupdf
    ib = builder()
    sp = ib.make_splitter()
    files = []
    for root, _, fs in os.walk(d):
        for f in fs:
            if f.lower().endswith(".pdf"):
                files.append(os.path.join(root, f))
    if not files:
        return {"articles": 0, "chars": 0, "note": "no PDFs"}
    # PAGES, NOT BYTES, AND A RANDOM SAMPLE. Two defects, both the same shape as
    # the ones the 2026-09-12 calibration found in the ZIM path.
    #
    # `files[:6]` after `sorted()` is the FIRST six alphabetically, which is not
    # a sample - it is the same ordered-walk mistake that had seventeen ZIMs
    # reading their opening entries and calling it density.
    #
    # And scaling by BYTES assumes characters per byte is constant across a
    # collection. It is not: a scanned page and a text page differ by orders of
    # magnitude, and `fao` scored 0.28x against the built index while
    # libretexts-workforce scored 0.97x on the same code. Characters per PAGE is
    # far steadier, and page counts are cheap - fitz reads the xref, not the
    # content - so every file can be counted and only a sample has to be read.
    pages_all = 0
    for f in files:
        try:
            d0 = fitz.open(f)
            pages_all += d0.page_count
            d0.close()
        except Exception:                                   # noqa: BLE001
            pass
    k = len(files) if full else min(len(files), max(8, len(files) // 4))
    pick = files if full else random.sample(files, k)
    total = 0; pages = 0; chunks = 0
    for p in pick:
        try:
            doc = fitz.open(p)
            text, pmap = ib.pdf_document(doc)
            pages += len(pmap) or doc.page_count
            doc.close()
            if len(text) >= ib.MIN_DOC_CHARS:
                total += len(text)
                chunks += len(sp.split_text(text))
        except Exception as e:
            print("      unreadable: %s (%s)" % (os.path.basename(p), e))
    if not pages:
        return {"articles": len(files), "chars": 0, "note": "no extractable text"}
    # SCALED BY PAGES. The byte-scaled version is kept in the comment above
    # because the reason it failed is the useful part: `fao` was predicted at
    # 1,582 chunks and produced 10,990 - and after the 09-12 rewrite it still
    # read 0.28x, because scaling the wrong quantity from a non-random sample
    # cannot be fixed by making the rest of the pipeline exact.
    scale = (pages_all / float(pages)) if pages else 1.0
    pick_bytes = sum(os.path.getsize(f) for f in pick) or 1
    all_bytes = sum(os.path.getsize(f) for f in files) or 1
    return {"articles": len(files), "chars": int(total * scale),
            "chunks_direct": int(chunks * scale),
            "method": "full" if full else
                      "sample(%d of %d files, %s pages of %s, %s of %s)" % (
                          len(pick), len(files), "{:,}".format(pages),
                          "{:,}".format(pages_all), human(pick_bytes),
                          human(all_bytes)),
            "pages_read": pages, "pages_total": pages_all}


# Extraction is not overhead noise. Measured over the 37 artifacts on record:
#
#   pure-HTML ZIMs        read is 21-30% of wall clock   0.0020-0.0026 s / chunk
#   in-ZIM PDF containers read is 78-87%                 ~0.032 s / page
#   PDF DIRECTORIES       read is 82-91%                 0.036-0.100 s / page
#
# Three terms and not one blended rate, because a blended rate is exactly the
# mistake this project has now made twice: article_count on 2026-09-02 and
# docs-per-GB on 2026-09-13. A number that is stable inside a corpus family and
# varies 30x between families cannot be averaged into a constant.
#
# The PDF-directory term is 2.5x the in-ZIM one because the directory path runs
# pdf-tables.py and the in-ZIM path does not. That is a real difference in work
# done, not a measurement artifact, and it is why they are separate terms.
def timing_model():
    """Embed and extract rates, derived from this build's own record.

    Returns None when 10-index/sources.json does not exist, which is the state
    this file was originally written for and the state in which it should still
    refuse to quote hours."""
    reg_p = os.path.join(ROOT, "10-index", "sources.json")
    if not os.path.exists(reg_p):
        return None
    try:
        arts = json.load(open(reg_p, encoding="utf-8"))["artifacts"].values()
    except Exception:
        return None

    emb_c = emb_s = 0
    html_c = html_s = 0
    pdir_p = pdir_s = 0
    rates = []
    trained = set()
    for a in arts:
        c = a.get("chunks") or 0
        e = a.get("seconds_embed") or 0
        tot = a.get("seconds") or 0
        rd = a.get("seconds_read_chunk", tot - e)
        st = a.get("stats") or {}
        pages = st.get("pdf_pages") or 0
        hdocs = st.get("html_docs") or 0
        if c and e > 1:
            emb_c += c; emb_s += e
            rates.append(c / e)
            trained.add(a.get("file"))
        # PURE artifacts only for the read terms. A mixed artifact cannot
        # attribute its seconds between the two paths, and fitting both terms
        # from blended rows is how a model learns to cancel its own errors.
        if c and rd > 0 and not pages:
            html_c += c; html_s += rd
        elif pages and not hdocs and rd > 0:
            pdir_p += pages; pdir_s += rd
    if not emb_c or not html_c:
        return None
    rates.sort()
    return {
        "embed_per_sec": emb_c / emb_s,
        "embed_lo": rates[0], "embed_med": rates[len(rates) // 2],
        "embed_hi": rates[-1], "n_embed": len(rates),
        "read_per_chunk_html": html_s / html_c,
        "read_per_pdf_page": (pdir_s / pdir_p) if pdir_p else 0.0805,
        "n_html": html_c, "n_pdf_pages": pdir_p,
        "built_chunks": emb_c,
        # WHICH ARTIFACTS THIS MODEL LEARNED FROM. Without it, calibrate()
        # cheerfully scores the model against its own training set and reports
        # a fit as though it were a prediction - which is the defect this whole
        # file spent 2026-09-13 correcting, reappearing inside the correction.
        "trained_on": trained,
    }


def hours(r, tm):
    """(embed, extract, wall) hours for one measured artifact.

    A PDF directory pays per PAGE and a ZIM pays per CHUNK, which is the same
    split measure_pdfs already makes for text volume. An in-ZIM PDF container is
    charged at the HTML rate and is therefore UNDERSTATED - measure_zim counts
    PDF entries but not PDF pages, so the term cannot be applied. Flagged in the
    report rather than fudged."""
    if not tm:
        return None
    emb = r["chunks"] / tm["embed_per_sec"]
    pages = r.get("pages_total")
    if pages:
        rd = pages * tm["read_per_pdf_page"]
    else:
        rd = r["chunks"] * tm["read_per_chunk_html"]
    return emb / 3600.0, rd / 3600.0, (emb + rd) / 3600.0


def calibrate(rows, tm=None):
    """Score this estimator against the index that actually got built.

    2026-09-13: THIS SCORED CHUNKS AND NEVER TIME, AND THAT IS THE HOLE THE
    8.0-HOUR FIGURE WENT THROUGH. The 09-12 calibration landed at 0.89x-1.13x
    per artifact and was written up as a clean bill of health - correctly, for
    chunks. libretexts and openstax then came in at 65,746 against a predicted
    66,199, which is 0.993. Meanwhile the HOURS quoted alongside those chunks
    were wrong by half, because nothing scored them and nothing could: this
    function only ever compared one column. A calibration that covers part of
    the output reads exactly like one that covers all of it.

    THE POINT OF THIS FUNCTION. `measure_zim` was reimplemented on 2026-09-12
    from its callers rather than recovered, so its numbers carry no authority of
    their own. `10-index/sources.json` carries the REAL document and chunk
    counts for every artifact of pass 1, which makes the error measurable
    instead of arguable - and a measured error is worth more than a resemblance
    to a tool nobody can read any more.

    It is also the check the 2026-09-02 entry wishes had existed: the previous
    estimator was 78% high and that was discovered by building the index, which
    is the expensive way to find out."""
    reg_p = os.path.join(ROOT, "10-index", "sources.json")
    if not os.path.exists(reg_p):
        print("\n  no sources.json - nothing to calibrate against"); return
    reg = json.load(open(reg_p, encoding="utf-8"))["artifacts"]
    real = {a["file"]: a for a in reg.values()}

    print("\n" + "=" * 74)
    print("  CALIBRATION against the built index")
    print("  %-44s %9s %9s %6s" % ("artifact", "predicted", "actual", "ratio"))
    pairs = []
    for r in rows:
        a = real.get(r["name"])
        if not a or not a.get("chunks"):
            continue
        ratio = r["chunks"] / float(a["chunks"])
        pairs.append((ratio, r["name"], r["chunks"], a["chunks"]))
        print("  %-44s %9s %9s %5.2fx"
              % (r["name"][:44], "{:,}".format(r["chunks"]),
                 "{:,}".format(a["chunks"]), ratio))
    if not pairs:
        print("  nothing in this scope has been built yet"); return
    tp = sum(p[2] for p in pairs); ta = sum(p[3] for p in pairs)
    rs = sorted(p[0] for p in pairs)
    print("  " + "-" * 70)
    print("  %-44s %9s %9s %5.2fx" % ("TOTAL over %d built artifacts" % len(pairs),
                                      "{:,}".format(tp), "{:,}".format(ta),
                                      tp / float(ta)))
    print("  per-artifact ratio: median %.2fx, worst low %.2fx, worst high %.2fx"
          % (rs[len(rs) // 2], rs[0], rs[-1]))
    print()
    print("  READ THE SPREAD, NOT THE TOTAL. A total near 1.00x can be two large")
    print("  errors cancelling - which is exactly what made the 2026-09-02")
    print("  estimator look merely imprecise when it was broken. The per-artifact")
    print("  worst case is what a scope estimate should be quoted with.")

    # AND NOW THE COLUMN THAT WAS MISSING.
    if not tm:
        print("\n  TIME NOT SCORED: no timing model (10-index/sources.json absent).")
        return
    tpairs = []
    for r in rows:
        a = real.get(r["name"])
        if not a or not a.get("seconds"):
            continue
        hh = hours(r, tm)
        if not hh:
            continue
        tpairs.append((hh[2] / (a["seconds"] / 3600.0), r["name"],
                       hh[2], a["seconds"] / 3600.0))
    if not tpairs:
        print("\n  TIME NOT SCORED: nothing in this scope has been built yet.")
        return
    print("\n  CALIBRATION of WALL CLOCK against the same artifacts")
    print("  %-44s %9s %9s %6s" % ("artifact", "pred h", "actual h", "ratio"))
    for ratio, nm, pred, act in tpairs:
        print("  %-44s %9.2f %9.2f %5.2fx" % (nm[:44], pred, act, ratio))
    # IS THIS A PREDICTION OR A FIT? The model is derived from sources.json, so
    # any artifact already built is in its training set. Scoring against those
    # measures how well the model reproduces what it learned, which is not the
    # question anyone asks a scope estimate. Say which it is, every time.
    overlap = [t[1] for t in tpairs if t[1] in tm.get("trained_on", ())]
    tr = sorted(t[0] for t in tpairs)
    tp2 = sum(t[2] for t in tpairs); ta2 = sum(t[3] for t in tpairs)
    print("  " + "-" * 70)
    print("  %-44s %9.2f %9.2f %5.2fx" % ("TOTAL over %d built artifacts" % len(tpairs),
                                          tp2, ta2, tp2 / max(ta2, 1e-9)))
    print("  per-artifact ratio: median %.2fx, worst low %.2fx, worst high %.2fx"
          % (tr[len(tr) // 2], tr[0], tr[-1]))
    if overlap:
        print()
        print("  *** THIS IS AN IN-SAMPLE FIT, NOT A PREDICTION. %d of %d scored"
              % (len(overlap), len(tpairs)))
        print("  artifacts are in the model's own training set, because the model is")
        print("  derived from sources.json and these are already built. It measures how")
        print("  well the model reproduces what it learned. The honest number is a")
        print("  HELD-OUT one: derive from an earlier pass and score a later one.")
        print("  Measured that way on 2026-09-14 - model from pass 1 alone, scoring the")
        print("  19 artifacts of pass 2 - the answer was 0.93x, not the %.2fx above."
              % (tp2 / max(ta2, 1e-9)))
    print()
    print("  For comparison, the hand-computed chunks/162 that produced the numbers")
    print("  in DECISIONS and the scope files scores %.2fx on the same artifacts."
          % ((sum(r["chunks"] for r in rows if real.get(r["name"])) / 162.0 / 3600.0)
             / max(ta2, 1e-9)))


def selftest():
    """The timing model, on values with a known answer.

    This tool had no selftest until 2026-09-13, which is part of why a wrong
    time constant lived outside it for a day without anything to contradict it.
    The real validation is the held-out one - a model derived from pass 1 alone
    predicted pass 2's eight artifacts at 0.87x total, median 0.93x, against
    0.54x for the chunks/162 arithmetic it replaces - but that needs a built
    index. This needs nothing, so it can run anywhere."""
    import tempfile, shutil
    ok = fail = 0

    def check(label, cond):
        nonlocal ok, fail
        if cond:
            ok += 1; print("  ok    %s" % label)
        else:
            fail += 1; print("  FAIL  %s" % label)

    global ROOT
    keep = ROOT
    tmp = tempfile.mkdtemp()
    try:
        # 1. NO REGISTRY MEANS NO ANSWER. This is the behaviour the file was
        #    written with and it must survive the change, or the fix replaces a
        #    refusal with a guess, which is worse than either.
        ROOT = tmp
        check("no sources.json -> timing_model() is None", timing_model() is None)
        check("no model -> hours() is None", hours({"chunks": 1000}, None) is None)

        # 2. A registry with no usable rows is also None, not a division by zero.
        os.makedirs(os.path.join(tmp, "10-index"))
        reg = os.path.join(tmp, "10-index", "sources.json")
        json.dump({"artifacts": {}}, open(reg, "w", encoding="utf-8"))
        check("empty registry -> None", timing_model() is None)

        # 3. Rates come back exactly as constructed. 100,000 chunks in 1,000 s
        #    is 100/s; 500 s of reading over 100,000 chunks is 0.005 s/chunk.
        json.dump({"artifacts": {
            "1": {"file": "a.zim", "chunks": 100000, "seconds": 1500,
                  "seconds_embed": 1000, "seconds_read_chunk": 500,
                  "stats": {"html_docs": 40000, "pdf_pages": 0}},
            "2": {"file": "b/", "chunks": 10000, "seconds": 1100,
                  "seconds_embed": 100, "seconds_read_chunk": 1000,
                  "stats": {"html_docs": 0, "pdf_files": 9, "pdf_pages": 5000}},
        }}, open(reg, "w", encoding="utf-8"))
        tm = timing_model()
        check("embed rate is chunk-weighted, not a mean of rates",
              abs(tm["embed_per_sec"] - 110000 / 1100.0) < 1e-6)
        check("html read term from the HTML artifact only",
              abs(tm["read_per_chunk_html"] - 0.005) < 1e-9)
        check("pdf read term is per PAGE from the PDF artifact only",
              abs(tm["read_per_pdf_page"] - 0.2) < 1e-9)

        # 4. THE TWO PATHS MUST NOT BE CHARGED THE SAME WAY. A row carrying
        #    pages_total is a PDF directory and pays per page; a row without one
        #    is a ZIM and pays per chunk. Getting this backwards is invisible in
        #    a total and wrong in every artifact.
        e, r, w = hours({"chunks": 100000}, tm)
        check("ZIM row charged per chunk", abs(r - 100000 * 0.005 / 3600) < 1e-9)
        e2, r2, w2 = hours({"chunks": 100000, "pages_total": 5000}, tm)
        check("PDF row charged per page, not per chunk",
              abs(r2 - 5000 * 0.2 / 3600) < 1e-9 and r2 != r)
        check("wall = embed + read", abs(w - (e + r)) < 1e-9)
        check("embedding is the same either way", abs(e - e2) < 1e-9)

        # 5. The defect this whole change exists to stop: an ETA taken from the
        #    embed rate alone. It must come out LOWER than wall clock, always.
        check("chunks/embed_rate is strictly below wall clock",
              (100000 / tm["embed_per_sec"] / 3600) < w)

        # 6. A mixed artifact must not pollute either term. Adding one with both
        #    html_docs and pdf_pages leaves the two rates untouched.
        d = json.load(open(reg, encoding="utf-8"))
        d["artifacts"]["3"] = {"file": "c.zim", "chunks": 50000, "seconds": 900,
                               "seconds_embed": 400, "seconds_read_chunk": 500,
                               "stats": {"html_docs": 10, "pdf_files": 3, "pdf_pages": 800}}
        json.dump(d, open(reg, "w", encoding="utf-8"))
        tm2 = timing_model()
        check("mixed artifact excluded from the html term",
              abs(tm2["read_per_chunk_html"] - 0.005) < 1e-9)
        check("mixed artifact excluded from the pdf term",
              abs(tm2["read_per_pdf_page"] - 0.2) < 1e-9)
        check("mixed artifact still counts toward the embed rate",
              tm2["n_embed"] == 3)
    finally:
        ROOT = keep
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n  %d/%d" % (ok, ok + fail))
    return 0 if not fail else 1


def main():
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    full = "--full" in sys.argv
    sample = 2000
    if "--sample" in sys.argv:
        sample = int(sys.argv[sys.argv.index("--sample") + 1])

    tm = timing_model()

    print("Ark index measurement - scope: %s" % os.path.basename(SCOPE))
    print("mode: %s\n" % ("FULL (reads every article)" if full else "sample of %d HTML ARTICLES per ZIM (not entries)" % sample))

    rows = []
    for rel in load_scope():
        path = os.path.join(ROOT, rel)
        if not os.path.exists(path):
            print("  MISSING: %s" % rel); continue
        name = os.path.basename(rel.rstrip("/")) or rel
        sys.stdout.write("  %-52s " % name[:52]); sys.stdout.flush()
        try:
            r = measure_pdfs(path, full, sample) if os.path.isdir(path) else measure_zim(path, full, sample)
        except Exception as e:
            print("ERROR %s" % e); continue
        r["name"] = name
        r["bytes"] = os.path.getsize(path) if os.path.isfile(path) else sum(
            os.path.getsize(os.path.join(rt, f)) for rt, _, fs in os.walk(path) for f in fs)
        # COUNTED IF THE MEASURER COUNTED THEM. measure_zim runs the builder's
        # own splitter over each sampled document, so its number is chunks, not
        # an inference from a characters-per-chunk constant that the 2026-09-12
        # calibration showed is wrong for every corpus on the shelf.
        chunks = r.get("chunks_direct")
        if chunks is None:
            chunks = int(r["chars"] / (CHARS_PER_CHUNK * (1 - OVERLAP))) if r["chars"] else 0
        r["chunks"] = chunks
        rows.append(r)
        hh = hours(r, tm)
        r["hours_wall"] = round(hh[2], 2) if hh else None
        print("%9s text, %8d docs, %9s chunks, %s  [%s]" % (
            human(r["chars"]), r["articles"], "{:,}".format(chunks),
            ("%5.1f h" % hh[2]) if hh else "   ? h",
            r.get("method", r.get("note", ""))))

        # Second line only when something deserves an argument. A mean far above
        # the median means a few giant entries are carrying the extrapolation and
        # the total is not to be trusted; a ZIM whose entries are mostly PDFs is
        # not indexable as HTML at all and is a SCOPE defect, not a small number.
        flags = []
        if r.get("mean_text") and r.get("median_text"):
            skew = r["mean_text"] / float(max(1, r["median_text"]))
            if skew > 3:
                flags.append("SKEW %.1fx (median %s, p90 %s, max %s) - extrapolation unsafe"
                             % (skew, "{:,}".format(r["median_text"]),
                                "{:,}".format(r["p90_text"]), "{:,}".format(r["max_text"])))
        if r.get("pdf_entries_seen") and tm and r["chunks"]:
            # measure_zim counts PDF ENTRIES, not PDF PAGES, so hours() charges
            # these at the HTML rate. Measured, the in-ZIM PDF path runs ~14x
            # the per-chunk extraction cost of HTML (zimgit-post-disaster 0.0304
            # s/chunk against 0.0023). Name the direction of the error.
            flags.append("%d in-ZIM PDFs - WALL CLOCK UNDERSTATED, this path measured"
                         " ~14x the HTML extraction cost per chunk and pages are not counted"
                         % r["pdf_entries_seen"])
        if r.get("pdf_entries_seen"):
            flags.append("%d in-ZIM PDFs (%s) -> %s chars%s%s"
                         % (r["pdf_entries_seen"], human(r.get("pdf_bytes_seen", 0)),
                            "{:,}".format(r.get("pdf_chars", 0)),
                            "" if r.get("exhaustive") else " [scaled from partial scan]",
                            ", %d with no extractable text" % r["pdf_no_text"]
                            if r.get("pdf_no_text") else ""))
        if r.get("article_count_reported") and r.get("articles"):
            drift = r["articles"] / float(max(1, r["article_count_reported"]))
            if drift < 0.5 or drift > 1.5:
                flags.append("density says ~%s docs, archive reports article_count=%s (%.2fx)"
                             " - article_count is not a document count here"
                             % ("{:,}".format(r["articles"]),
                                "{:,}".format(r["article_count_reported"]), drift))
        if r.get("article_count_reported", r.get("articles", 0)) <= 1 and r.get("entry_count", 0) > 100:
            flags.append("article_count=%d but entry_count=%s - this is a document container, not a wiki"
                         % (r.get("article_count_reported", r["articles"]),
                            "{:,}".format(r["entry_count"])))
        if r["bytes"] and r["chars"] / float(r["bytes"]) > 3:
            flags.append("text is %.1fx the compressed file - check before believing"
                         % (r["chars"] / float(r["bytes"])))
        for f in flags:
            print("  %-52s   ^ %s" % ("", f))

    tot_chunks = sum(r["chunks"] for r in rows)
    tot_chars = sum(r["chars"] for r in rows)
    tot_bytes = sum(r["bytes"] for r in rows)
    print("\n" + "=" * 74)
    print("  source material          %s across %d artifacts" % (human(tot_bytes), len(rows)))
    print("  extractable text         %s" % human(tot_chars))
    # COUNTED, and the label used to say "AT 2048 CHARS" because they were
    # derived from a constant that is wrong for every corpus on the shelf.
    print("  CHUNKS, COUNTED          %s   (real splitter, per sampled document)"
          % "{:,}".format(tot_chunks))
    print()
    emb_f32 = tot_chunks * DIM * 4
    emb_f16 = tot_chunks * DIM * 2
    print("  embeddings, float32      %s   (the .npy archive of record)" % human(emb_f32))
    print("  embeddings, float16      %s   (half the bytes, negligible recall cost)" % human(emb_f16))
    ram = tot_chunks / 1e6 / CHROMA_MCHUNKS_PER_GB
    pq = tot_chunks * PQ_BYTES_PER_VECTOR / 1e9
    print("  chroma hnsw RAM          %.1f GB   (retired 2026-09-12, kept to compare)" % ram)
    print("  PQ64 + rerank RAM        %.1f GB   <-- THE INDEX THIS WOULD ACTUALLY BUILD" % pq)
    print()
    # THE VERDICT KEYS ON PQ, AND UNTIL 2026-09-12 IT COULD NOT. This block
    # quoted only the Chroma figure, so on a CORE-sized scope it would have
    # reported the scope as impossible - true of an architecture the build no
    # longer uses and false about the one it chose. The index type is settled;
    # what a measurement decides now is SCOPE.
    if pq > 8:
        print("  VERDICT: large even for PQ64. Cut the scope, not the index type.")
        print("           The lever is chunk COUNT - lead sections rather than whole")
        print("           articles, and dropping list/disambiguation/stub pages.")
    elif ram > 48:
        print("  VERDICT: impossible under Chroma, comfortable under PQ64 at %.1f GB." % pq)
        print("           This is the scope the 2026-09-01 decision to keep embeddings")
        print("           as plain .npy bought, and the whole reason the index TYPE")
        print("           stayed re-decidable. DECISIONS.md 2026-09-12.")
    elif ram > 16:
        print("  VERDICT: Chroma would fit the M18 and not a solar node; PQ64 fits")
        print("           both, at %.1f GB. Build it with faiss either way." % pq)
    else:
        print("  VERDICT: comfortable. Chroma HNSW is fine for pass 1 on either machine.")
    print()
    if not tm:
        print("  NOT MEASURED HERE: throughput. 10-index/sources.json does not exist,")
        print("  so this machine has no record of its own rates, and a figure borrowed")
        print("  from someone else's hardware would be precisely the kind of confident")
        print("  wrong number this build keeps catching. Build something, then ask.")
    else:
        emb_h = tot_chunks / tm["embed_per_sec"] / 3600.0
        rd_h = sum((hours(r, tm) or (0, 0, 0))[1] for r in rows)
        wall = emb_h + rd_h
        print("  TIME, from this machine's own record of %s built chunks:" % "{:,}".format(tm["built_chunks"]))
        print("    embedding              %6.1f h   at %.0f chunks/s (per-artifact %.0f to %.0f, n=%d)"
              % (emb_h, tm["embed_per_sec"], tm["embed_lo"], tm["embed_hi"], tm["n_embed"]))
        print("    extraction             %6.1f h   %.4f s/chunk HTML, %.4f s/page PDF dir"
              % (rd_h, tm["read_per_chunk_html"], tm["read_per_pdf_page"]))
        print("    WALL CLOCK             %6.1f h   <-- what the run actually costs" % wall)
        print("                                      (%.0f%% of it is extraction, not GPU)"
              % (100.0 * rd_h / max(wall, 1e-9)))
        print()
        print("  EXTRACTION IS NOT OVERHEAD. It is %.0f%% of the clock here and 42%% across"
              % (100.0 * rd_h / max(wall, 1e-9)))
        print("  the 37 artifacts this model is derived from. Quoting chunks/embed-rate")
        print("  as an ETA - which is what produced the 8.0 h figure for a pass now")
        print("  landing near 12.2 - understates by about half on ZIMs and by 10x on")
        print("  PDF directories. DECISIONS.md 2026-09-13.")

    # NAMED FOR ITS SCOPE. This was one fixed filename, so measuring a second
    # corpus would have overwritten the pass-1 numbers with CORE's and left
    # nothing in the file saying which run it described.
    tag = re.sub(r"[^A-Za-z0-9]+", "-",
                 os.path.splitext(os.path.basename(SCOPE))[0])
    out = os.path.join(ROOT, "_incoming", "index-measurement-%s.json" % tag)
    json.dump({"rows": rows, "total_chunks": tot_chunks, "total_chars": tot_chars,
               "chars_per_chunk": CHARS_PER_CHUNK, "ram_gb_chroma": ram},
              open(out, "w", encoding="utf-8"), indent=1)
    print("\n  written: %s" % out)
    if "--calibrate" in sys.argv:
        calibrate(rows, tm)


if __name__ == "__main__":
    main()
