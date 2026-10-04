#!/usr/bin/env python3
"""compare-revisions.py - read differing mdwiki / wikipedia pairs by hand.

WHY THIS EXISTS. `store.excluded` drops the 69,980 mdwiki articles that
wikipedia_en_medicine_maxi also carries, and keeps the wikipedia copy. On the
52% that are byte-identical that choice costs nothing. On the 18% that genuinely
differ it is an ASSUMPTION: mdwiki is the 2025-11 snapshot and Wikipedia
medicine is 2026-04, and newer is usually better for a Wikipedia article - but
mdwiki is a curated medical wiki and a vetted revision is a real editorial
thing. Nobody has looked.

This is the build's own rule from the 2026-09-09 safety-detector decision,
applied to a different filter: measure the firing rate, read forty by hand, then
set the tier. Here it is twenty pairs, because each one is a medical article
read end to end rather than a yes/no on a unit.

WHAT IT DOES NOT DO. It does not decide anything and it writes nothing. It
prints pairs and a unified diff so a person can read them. If the reading says
mdwiki wins on a class of article, the fix is to invert the rule for that class
in store.excluded, not to edit anything here.

    python bin/compare-revisions.py              20 pairs, seed 20260911
    python bin/compare-revisions.py -n 40        more of them
    python bin/compare-revisions.py --seed 7     a different sample
    python bin/compare-revisions.py --full       whole documents, not a diff

REQUIRES: 10-index/provenance.sqlite3 and the two .jsonl files, which are about
half a gigabyte each. Standard library only.
"""

import argparse
import difflib
import json
import os
import random
import sqlite3
import sys

ROOT = os.environ.get("ARK_ROOT") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))
INDEX = os.path.join(ROOT, "10-index")
PROV = os.path.join(INDEX, "provenance.sqlite3")
CHUNKS = os.path.join(INDEX, "chunks")

OLD_PREFIX = "mdwiki_"
NEW_PREFIX = "wikipedia_en_medicine"


def artifact(conn, prefix):
    for r in conn.execute("SELECT src, file, jsonl FROM artifact"):
        if r[1].startswith(prefix):
            return r[0], r[1], r[2]
    return None


def doc_text(conn, handle, src, dnum):
    """Every chunk of one document, in order, joined back into the article."""
    parts = []
    for off, ln in conn.execute(
            "SELECT off, len FROM chunk WHERE src=? AND dnum=? ORDER BY i",
            (src, dnum)):
        handle.seek(off)
        parts.append(json.loads(handle.read(ln).decode("utf-8"))["text"])
    return "\n".join(parts)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", type=int, default=20, help="pairs to print")
    ap.add_argument("--seed", type=int, default=20260911)
    ap.add_argument("--full", action="store_true",
                    help="print both documents rather than a diff")
    ap.add_argument("--threshold", type=float, default=0.90,
                    help="below this similarity a pair counts as a real "
                         "revision difference (default 0.90)")
    a = ap.parse_args()

    if not os.path.exists(PROV):
        sys.exit("missing %s" % PROV)
    conn = sqlite3.connect("file:%s?mode=ro" % PROV.replace("\\", "/"), uri=True)

    old = artifact(conn, OLD_PREFIX)
    new = artifact(conn, NEW_PREFIX)
    if not old or not new:
        sys.exit("need both %s* and %s* in the index" % (OLD_PREFIX, NEW_PREFIX))
    print("old: %s\nnew: %s\n" % (old[1], new[1]))

    newpaths = {r[0]: r[1] for r in
                conn.execute("SELECT path, dnum FROM doc WHERE src=?", (new[0],))}
    shared = [(d, p) for d, p in
              conn.execute("SELECT dnum, path FROM doc WHERE src=?", (old[0],))
              if p in newpaths]
    print("%s shared articles\n" % format(len(shared), ","))

    fo = open(os.path.join(CHUNKS, old[2]), "rb")
    fn = open(os.path.join(CHUNKS, new[2]), "rb")

    # SAMPLE, THEN FILTER - not the other way round. Scanning for differing
    # pairs first and taking the first twenty would bias toward whatever sorts
    # early, which in this corpus is punctuation and numerals.
    rng = random.Random(a.seed)
    order = shared[:]
    rng.shuffle(order)

    shown = 0
    scanned = 0
    buckets = {"identical": 0, "near": 0, "differs": 0}
    for dnum, path in order:
        if shown >= a.n:
            break
        scanned += 1
        to = doc_text(conn, fo, old[0], dnum)
        tn = doc_text(conn, fn, new[0], newpaths[path])
        if to == tn:
            buckets["identical"] += 1
            continue
        ratio = difflib.SequenceMatcher(None, to[:6000], tn[:6000]).ratio()
        if ratio >= a.threshold:
            buckets["near"] += 1
            continue
        buckets["differs"] += 1
        shown += 1

        print("=" * 78)
        print("%2d. %s" % (shown, path))
        print("    similarity %.3f   old %s chars   new %s chars"
              % (ratio, format(len(to), ","), format(len(tn), ",")))
        print("=" * 78)
        if a.full:
            print("--- OLD (%s) ---\n%s\n" % (old[1], to))
            print("--- NEW (%s) ---\n%s\n" % (new[1], tn))
        else:
            d = difflib.unified_diff(
                to.splitlines(), tn.splitlines(),
                fromfile="old " + old[1], tofile="new " + new[1],
                lineterm="", n=1)
            for line in d:
                print(line)
        print()

    print("=" * 78)
    print("scanned %d shared articles to find %d that differ by more than %.2f"
          % (scanned, shown, 1 - a.threshold))
    print("  byte-identical %d   >=%.2f similar %d   differs %d"
          % (buckets["identical"], a.threshold, buckets["near"], buckets["differs"]))
    print()
    print("THE QUESTION TO ANSWER WHILE READING: on these, is the 2026-04")
    print("Wikipedia text the one an operator should be shown? If it is, the")
    print("mirror rule in ark-api/store.py is right as written. If mdwiki wins")
    print("on some recognisable class of article, that class is what the rule")
    print("has to spare - and this script found it rather than assumed it.")


if __name__ == "__main__":
    main()
