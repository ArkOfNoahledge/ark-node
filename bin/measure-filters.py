#!/usr/bin/env python3
"""
measure-filters.py - what the index quality filters cost a real result page.

    python bin/measure-filters.py                 # keyword, needs no model
    python bin/measure-filters.py --mode hybrid   # needs the keeper venv
    python bin/measure-filters.py --show          # name what was excluded
    python bin/measure-filters.py --pool 20 40 80 # find the pool that survives

WHAT IT ANSWERS, AND IT IS ONE QUESTION: is the candidate pool big enough now
that 37.1% of the index is filtered out after retrieval?

`serve.search` asks BM25 and Chroma for `pool` candidates and then removes
navigation pages, duplicates, and - since 2026-09-11 - iFixit user profiles,
wikem translations and the mdwiki mirror. Removal happens AFTER retrieval, so a
query whose top candidates are mostly excluded can run out of pool before it has
five results. The old pool was n*4; it is n*8 now, and THAT NUMBER WAS CHOSEN
RATHER THAN MEASURED. This is the measurement. Whatever it says is what the pool
should be, and the comment in serve.search should then name this run.

IT CALLS THE SHIPPED CODE AND BORROWS THE SHIPPED QUERIES. `search()` by import,
the same function the browser reaches, and the 24 questions from
`measure-retrieval.py` rather than a second list that would drift from it. Both
arguments are made at length in that file and this one only has to not undo
them.

THE COMPARISON IS AGAINST THE FILTERS OFF, NOT AGAINST A REMEMBERED NUMBER.
`ARK_NO_FILTERS=1` is read by `store.Store.EXCLUDE_OFF`, so the same query runs
both ways in one process and the difference is the filters and nothing else.
"""

import argparse
import importlib.util
import os
import sys

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.environ.get("ARK_ROOT") or os.path.dirname(HERE)
API = os.path.join(ROOT, "13-ark-node", "ark-api")
if API not in sys.path:
    sys.path.insert(0, API)
import store                                                   # noqa: E402
import serve                                                   # noqa: E402


def _sibling(name):
    """Import a bin/ tool by path; their filenames carry hyphens. Same helper and
    same reason as measure-retrieval.py, which is where QUERIES lives."""
    spec = importlib.util.spec_from_file_location(
        name.replace("-", "_").replace(".py", ""), os.path.join(HERE, name))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="keyword",
                    choices=["keyword", "hybrid", "dense"])
    ap.add_argument("-n", type=int, default=5, help="results per page")
    ap.add_argument("--pool", type=int, nargs="*", default=None,
                    help="pool sizes to try; default is whatever serve.search uses")
    ap.add_argument("--show", action="store_true",
                    help="name the excluded paths, not just count them")
    a = ap.parse_args()

    queries = _sibling("measure-retrieval.py").QUERIES
    serve.S = store.Store()
    st = serve.S.stats()
    ex = st["excluded"]

    print("mode=%s  n=%d  queries=%d" % (a.mode, a.n, len(queries)))
    print("index %s chunks, searchable %s, excluded %s (%.1f%%): %s\n"
          % ("{:,}".format(st["chunks"]), "{:,}".format(st["chunks_searchable"]),
             "{:,}".format(ex["chunks"]),
             100.0 * ex["chunks"] / max(st["chunks"], 1),
             ", ".join("%s %s" % (k, "{:,}".format(v))
                       for k, v in sorted(ex["by_rule"].items()))))

    # --- how many results survive, with the filters and without ------------
    rows = []
    for q in queries:
        store.Store.EXCLUDE_OFF = True
        off = serve.search(q, a.n, a.mode)
        store.Store.EXCLUDE_OFF = os.environ.get("ARK_NO_FILTERS") == "1"
        on = serve.search(q, a.n, a.mode)
        rows.append((q, off, on))

    print("%-46s %5s %5s %5s  %s"
          % ("query", "off", "on", "got", "excluded"))
    print("-" * 100)
    short = 0
    changed = 0
    for q, off, on in rows:
        s = on["suppressed"]["excluded"]
        if len(on["results"]) < a.n:
            short += 1
        # DID THE PAGE ACTUALLY CHANGE? A filter that removes chunks nobody was
        # going to see is free and also pointless; the number worth knowing is
        # how many pages read differently afterwards.
        if [r["cid"] for r in off["results"]] != [r["cid"] for r in on["results"]]:
            changed += 1
        # SHORT BECAUSE OF THE FILTERS, OR SHORT BECAUSE THE ARCHIVE IS THIN?
        # The first version of this printed POOL EXHAUSTED for both, and
        # `spate irrigation` - one candidate in the whole index, zero
        # exclusions - was the query that showed the difference matters.
        flag = ""
        if on["candidates_exhausted"]:
            flag = ("   FILTERS COST RESULTS" if on["suppressed"]["excluded_total"]
                    else "   thin corpus, nothing excluded")
        print("%-46s %5d %5d %5d  %s%s"
              % (q[:46], len(off["results"]), len(on["results"]), on["retrieved"],
                 ", ".join("%d %s" % (v, k) for k, v in sorted(s.items())) or "-",
                 flag))
        if a.show:
            for r in off["results"]:
                why = serve.S.excluded(serve.S.meta(r["cid"]))
                if why:
                    print("        %-11s %s" % (why, r["citation"]["path"][:70]))

    print("-" * 100)
    print("%d of %d pages returned fewer than %d results with the filters on"
          % (short, len(rows), a.n))
    print("%d of %d pages changed at all" % (changed, len(rows)))

    # --- does a bigger pool fix the short pages? ----------------------------
    # SWEEPS THE SHIPPED CONSTANT, NOT A COPY OF THE RULE. serve.POOL_FACTOR is
    # what `search()` reads, so every number below comes from the same line the
    # browser runs. Restored in a finally, because leaving a measurement's value
    # behind in a long-lived process is the quietest way to make the next
    # measurement wrong.
    if a.pool:
        keep = (serve.POOL_FACTOR, serve.POOL_MIN)
        try:
            print("\npool sweep - pages returning fewer than %d results:" % a.n)
            for p in a.pool:
                serve.POOL_FACTOR, serve.POOL_MIN = max(1, p // a.n), p
                bad = [q for q in queries
                       if len(serve.search(q, a.n, a.mode)["results"]) < a.n]
                print("  pool %-4d %2d short%s"
                      % (p, len(bad), ("   e.g. " + bad[0][:44]) if bad else ""))
        finally:
            serve.POOL_FACTOR, serve.POOL_MIN = keep

    print("\nWHAT TO DO WITH THIS. If no page is short, the pool in "
          "serve.search is big enough\nand the comment there should name this "
          "run. If pages are short, raise it and say so;\na page of three that "
          "could have been a page of five is a retrieval failure the\noperator "
          "cannot see.")


if __name__ == "__main__":
    main()
