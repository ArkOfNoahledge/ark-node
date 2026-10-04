#!/usr/bin/env python3
"""
index-query.py - ask the index a question and see WHERE the answer came from.

    python bin/index-query.py "how do I make flood water safe to drink"
    python bin/index-query.py "como purificar agua"  --n 8
    python bin/index-query.py --selftest

This is the acceptance test for the retrieval layer, and it tests the thing that
actually matters. A build that completes is not a build that works: the question is
whether a real question returns a passage that answers it, ATTACHED TO A CITATION
AN OPERATOR CAN OPEN. An answer this system cannot trace back to a document on the
drive is the failure mode spec 2 exists to prevent.

No LLM is involved here. This is retrieval only - deliberately, because when the
full stack later gives a wrong answer, the first question is always "did retrieval
find the right passage, or did the model invent one?" This script answers that
question in isolation and will keep answering it after the model changes.

REWRITTEN 2026-09-16 ONTO store.py, AND THE REASON IS THE ENTRY.

This tool opened `chromadb.PersistentClient` and `get_collection("ark_pass1")`
unconditionally, on line 183, before anything else ran. Chroma was retired the
same day, so the file that calls itself the acceptance test for the retrieval
layer WOULD HAVE DIED ON ITS FIRST LINE OF WORK - and `13-ark-node/RUNBOOK.md`
names it as the tool to reach for when the question is *did retrieval fail*.

That is this project's own rule arriving from a new direction: a script that has
never been executed is not a procedure, and neither is one that stopped being
executable while every document went on naming it. `index-measure.py` was dead for
ten days in exactly that way. This one was caught BEFORE the deletion rather than
after, which is the only difference.

IT NOW CALLS THE SHIPPED CODE INSTEAD OF A COPY OF IT. `13-ark-node/ark-api/
store.py` is the retrieval layer; `serve.py` is the HTTP layer, and this tool
still does not touch it. Four definitions that used to be duplicated here are
imported instead - `fuse`, `agreement` (was `confidence`), `is_navigation` and the
citation builder - which is the `measure-retrieval.py` lesson: a measurement that
mirrors the code measures the mirror. The thresholds and the RRF constant were
always shared by comment ("same constant and same reasoning as
bin/index-query.py"); now they are shared by import.

TWO BEHAVIOURS CHANGED AND BOTH ARE IMPROVEMENTS, SAID OUT LOUD SO A COMPARISON
AGAINST AN OLDER RUN IS NOT MADE BY ACCIDENT:

  1. THE INDEX-QUALITY FILTERS NOW APPLY. store.py removes iFixit contributor
     profiles, wikem pages in languages the operator does not read, and mdwiki
     articles the Wikipedia copy already contains - 725,686 chunks, 10.5%. This
     tool used to search the raw index. It now searches WHAT THE NODE SEARCHES,
     6,200,585 chunks, which is the only number an acceptance test should use.
  2. THE DENSE HALF IS WHATEVER THE NODE IS SERVING. Flat faiss PQ64 with an
     exact rerank as of 2026-09-15. The backend and its coverage are printed on
     every run, because the two possible backends did not cover the same corpus
     and silence about which one answered was the defect that cost three hours on
     2026-09-15.
"""

import os, sys, re, argparse, importlib.util

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.environ.get("ARK_ROOT") or os.path.dirname(HERE)
OUT = os.path.join(ROOT, "10-index")

os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"

# THE RETRIEVAL LAYER, IMPORTED. Same path and same reasoning as
# bin/measure-retrieval.py and bin/measure-filters.py.
API = os.path.join(ROOT, "13-ark-node", "ark-api")
if API not in sys.path:
    sys.path.insert(0, API)
import store                                                   # noqa: E402

# Questions a person in the situation this archive is for would actually ask.
# Deliberately mixed EN/ES: the corpus is bilingual and cross-lingual retrieval was
# measured at 0.917 similarity on a probe pair - this checks it on real documents.
SELFTEST = [
    "how do I make flood water safe to drink",
    "what do I do for someone in shock",
    "como purificar agua despues de una inundacion",
    "how much water does a person need per day in an emergency",
    "signs of dehydration in a child",
]


def shingles(t, k=5):
    w = re.sub(r"[^a-z0-9 ]", " ", t.lower()).split()
    return set(tuple(w[i:i + k]) for i in range(max(0, len(w) - k + 1)))


def near_dupe(a, b, thresh=0.55):
    """Jaccard on word 5-grams. No embeddings needed, and it catches the case the
    pilot showed: the SAME passage of FM 3-05.70 sitting in two differently-named
    PDFs inside one archive, taking two of five retrieval slots. Harmless when a
    person reads the results; expensive in the RAG layer, where those two slots are
    context that should have held two DIFFERENT sources.

    KEPT LOCAL ON PURPOSE, unlike the four definitions now imported from store.py.
    The surface suppresses passages that are byte-identical after whitespace
    normalisation, which is deliberately strict so every figure it prints is a
    floor. This is the looser rule, and having both is the point: a near-duplicate
    the surface shows is one a person reading results should still be told about.
    Two rules that disagree are evidence; one rule copied twice is drift."""
    A, B = shingles(a), shingles(b)
    if not A or not B:
        return False
    return len(A & B) / float(min(len(A), len(B))) >= thresh


def cite(S, m):
    """artifact -> location -> chunk, from store.py's own citation builder."""
    c = S.citation(m)
    where = c.get("artifact", "?")
    if m.get("kind") == "pdf":
        loc = "%s, page %s" % (m.get("title") or m.get("path"), m.get("page"))
    else:
        loc = m.get("title") or m.get("path")
    return "%s  ->  %s  [%s, chunk %d/%d]" % (
        where, loc, m.get("path"), (m.get("i") or 0) + 1, m.get("n") or 1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("question", nargs="*")
    ap.add_argument("--n", type=int, default=5)
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--chars", type=int, default=260, help="passage preview length")
    ap.add_argument("--dense-only", dest="hybrid", action="store_false", default=True,
                    help="embeddings only - the pass-1 behaviour, kept for comparison")
    ap.add_argument("--keep-navigation", action="store_true",
                    help="show tag listings and contents pages instead of suppressing them")
    ap.add_argument("--no-dedupe", action="store_true",
                    help="show near-duplicate passages instead of suppressing them")
    args = ap.parse_args()

    for p in (store.PROV_DB, store.BM25_DB):
        if not os.path.exists(p):
            sys.exit("missing %s\n  build it: python bin/index-provenance.py" % p)
    S = store.Store()
    st = S.stats()
    print("index: %s artifacts, %s chunks, %s searchable after filters"
          % ("{:,}".format(st["artifacts"]), "{:,}".format(st["chunks"]),
             "{:,}".format(st["chunks_searchable"])))

    questions = SELFTEST if args.selftest else [" ".join(args.question)]
    if not questions or not questions[0]:
        sys.exit("ask a question, or pass --selftest")

    # WHICH BACKEND, BEFORE ANY RESULT. Asking one question warms the lazy load so
    # the line below describes the run rather than the moment before it. A tool
    # that reports a backend it has not used is the 2026-09-15 defect.
    S.dense(questions[0], 1)
    backend, cover = S.dense_backend(), S.dense_coverage()
    if backend:
        print("dense: %s over %s vectors" % (backend, "{:,}".format(cover)))
    else:
        print("dense: NOT LOADED - this run is KEYWORD ONLY")
        print("       %s" % (S.dense_error() or
                             "no cause recorded, which is itself worth reporting"))
        if not args.hybrid:
            sys.exit("--dense-only was asked for and there is no dense half")
    print()

    for q in questions:
        pool = args.n if args.no_dedupe else args.n * 4

        dense_ids, dscore = S.dense(q, pool * 2)
        sparse_ids = S.keyword(q, pool * 2) if args.hybrid else []

        print("=" * 78)
        print("Q: %s" % q)

        if args.hybrid and dense_ids and sparse_ids:
            order = S.fuse(dense_ids, sparse_ids)
            in_both = len(set(dense_ids) & set(sparse_ids))
            label, why = S.agreement(in_both)
            print("   [hybrid: %d dense + %d keyword, %d in both - %s%s]"
                  % (len(dense_ids), len(sparse_ids), in_both, label,
                     ": " + why if why else ""))
        else:
            # HONEST LABEL. One retriever has no agreement to report, and calling
            # that low agreement would read as a finding about the corpus rather
            # than a fact about how the query was run. store.py says the same.
            order = dense_ids or sparse_ids
            if order:
                print("   [single retriever - no agreement signal is available]")

        if not order:
            print("   NOTHING RETURNED - the query matched nothing, or both halves "
                  "are unavailable")
            print()
            continue

        kept, dropped, nav = [], 0, 0
        for cid in order:
            m = S.meta(cid)
            if not m:
                continue
            txt = re.sub(r"\s+", " ", S.text(m) or "").strip()
            if not txt:
                continue
            if not args.keep_navigation and S.is_navigation(m, txt):
                nav += 1
                continue
            if not args.no_dedupe and any(near_dupe(txt, k[0]) for k in kept):
                dropped += 1
                continue
            kept.append((txt, m, dscore.get(cid)))
            if len(kept) >= args.n:
                break

        for rank, (txt, m, sc) in enumerate(kept, 1):
            head = "  keyword " if sc is None else "score %.3f" % sc
            print("\n %d. %s   %s" % (rank, head, cite(S, m)))
            print("    %s%s" % (txt[:args.chars], "..." if len(txt) > args.chars else ""))

        if nav:
            print("\n    (%d navigation page%s suppressed - tag listings and tables of "
                  "contents,\n     which match many words and answer nothing)"
                  % (nav, "s" if nav > 1 else ""))
        if dropped:
            print("\n    (%d near-duplicate passage%s suppressed - the archive carries the "
                  "same manual\n     under more than one filename)"
                  % (dropped, "s" if dropped > 1 else ""))
        print()


if __name__ == "__main__":
    main()
