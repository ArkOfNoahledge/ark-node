#!/usr/bin/env python3
"""
index-audit.py - what is actually IN the index, and how much of it is junk?

    python bin/index-audit.py                # whole index
    python bin/index-audit.py --artifact mdwiki_en_all_maxi_2025-11.zim

Runs entirely on 10-index/chunks/*.jsonl. No GPU, no model, no Chroma - so it can
be re-run after any extraction change without re-embedding anything.

WHY. The acceptance test asked "how do I make flood water safe to drink" against
3.2 million chunks and returned, at rank 1, a Stack Exchange tag-listing page about
watermelons, followed by two identical MediaWiki footer templates - and suppressed
17 near-duplicates behind them. Nothing in the top results was a document; they
were the STRUCTURAL FURNITURE of documents. The same query worked on a 1,283-chunk
pilot, which is the trap: retrieval noise does not appear until there is enough
corpus for the noise to win.

Three things are measured here, all of them cheap:

  1. BOILERPLATE. Navigation blocks, category footers and "see also" farms repeat
     across thousands of articles. They are near-identical, they are semantically
     mush, and they match weak queries better than real prose does.
  2. PROSE DENSITY. Real writing has sentences. A link farm has hundreds of short
     capitalised phrases and almost no full stops.
  3. EXACT DUPLICATES. The cheapest possible check, and it has never been run.
"""

import os, sys, json, re, glob, hashlib, argparse
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CHUNKS = os.path.join(ROOT, "10-index", "chunks")

_W = re.compile(r"\s+")
_SENT = re.compile(r"[.!?]")


def prose_score(t):
    """Full stops per 100 words. Ordinary prose lands around 5-8; a navigation
    block or a tag listing lands near zero because it is a list of names."""
    w = t.split()
    if len(w) < 20:
        return 99.0
    return 100.0 * len(_SENT.findall(t)) / len(w)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--artifact")
    ap.add_argument("--prose-threshold", type=float, default=1.5)
    ap.add_argument("--show", type=int, default=12)
    args = ap.parse_args()

    files = sorted(glob.glob(os.path.join(CHUNKS, "*.jsonl")))
    if args.artifact:
        files = [f for f in files if args.artifact in os.path.basename(f)]
    if not files:
        sys.exit("no chunk files found - run the build first")

    exact = Counter()          # hash of the whole chunk
    prefix = Counter()         # hash of the first 240 chars: catches shared footers
    prefix_example = {}
    per_art = {}
    total = 0
    lowprose = 0

    for f in files:
        name = os.path.basename(f)[:-6]
        a = per_art.setdefault(name, {"n": 0, "lowprose": 0, "chars": 0})
        with open(f, encoding="utf-8") as fh:
            for line in fh:
                m = json.loads(line)
                t = _W.sub(" ", m["text"]).strip()
                total += 1
                a["n"] += 1
                a["chars"] += len(t)
                exact[hashlib.sha1(t.encode("utf-8", "ignore")).hexdigest()] += 1
                key = hashlib.sha1(t[:240].encode("utf-8", "ignore")).hexdigest()
                prefix[key] += 1
                if key not in prefix_example:
                    prefix_example[key] = (name, t[:150])
                if prose_score(t) < args.prose_threshold:
                    lowprose += 1
                    a["lowprose"] += 1

    print("chunks examined: %s across %d artifact(s)\n" % ("{:,}".format(total), len(per_art)))

    dup_chunks = sum(c - 1 for c in exact.values() if c > 1)
    print("EXACT DUPLICATES")
    print("  %s chunks are byte-identical to another chunk (%.1f%% of the index)"
          % ("{:,}".format(dup_chunks), 100.0 * dup_chunks / max(1, total)))

    print("\nSHARED OPENINGS  (same first 240 characters - the boilerplate signature)")
    shared = sum(c - 1 for c in prefix.values() if c > 1)
    print("  %s chunks share an opening with another (%.1f%%)"
          % ("{:,}".format(shared), 100.0 * shared / max(1, total)))
    print("  worst offenders:")
    for key, n in prefix.most_common(args.show):
        if n < 2:
            break
        art, ex = prefix_example[key]
        print("   %7s x  [%s]" % ("{:,}".format(n), art[:34]))
        print("            %s..." % ex[:110])

    print("\nPROSE DENSITY  (< %.1f full stops per 100 words = not prose)" % args.prose_threshold)
    print("  %s chunks (%.1f%%) look like link farms or listings rather than writing"
          % ("{:,}".format(lowprose), 100.0 * lowprose / max(1, total)))
    print("\n  %-46s %10s %10s %7s" % ("artifact", "chunks", "low-prose", "share"))
    for n, a in sorted(per_art.items(), key=lambda kv: -kv[1]["lowprose"])[:14]:
        print("  %-46s %10s %10s %6.1f%%" % (
            n[:46], "{:,}".format(a["n"]), "{:,}".format(a["lowprose"]),
            100.0 * a["lowprose"] / max(1, a["n"])))

    print("\nWhat to do with this: nothing yet. These counts decide whether the fix is")
    print("a query-time filter, an extraction change plus a re-chunk (no re-embedding")
    print("of good chunks needed), or a full rebuild. Measure first.")


if __name__ == "__main__":
    main()
