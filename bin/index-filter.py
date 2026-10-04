#!/usr/bin/env python3
"""
index-filter.py - decide which chunks the INDEX should ignore, without deleting
anything and without re-embedding anything.

    python bin/index-filter.py --inspect          # look before touching
    python bin/index-filter.py --apply            # write the exclusion list
    python bin/index-build.py --load-only         # rebuild Chroma without them

WHAT THIS DOES NOT DO. It does not modify 10-index/chunks or 10-index/vectors.
Those are the archive of record (see 10-index/README.md) and they stay complete.
This writes an EXCLUSION LIST, and the Chroma loader skips those ids. Every
decision here is therefore reversible by deleting one file and reloading - which
is the entire reason the vectors were kept as plain files in the first place.

WHY IT EXISTS. `index-audit.py` on the completed pass-1 build, 2026-09-02:

    491,115 chunks (15.4%) byte-identical to another chunk
    701,872 chunks (22.0%) sharing their first 240 characters
    620,886 chunks (19.4%) with almost no sentence punctuation

and the acceptance test returning, for "how do I make flood water safe to drink",
a Stack Exchange tag listing and two MediaWiki licence footers. The single most
repeated text in a medical corpus was "The text is available under Creative
Commons Attribution-ShareAlike License", 5,643 times.

TWO RULES ARE APPLIED, AND A THIRD IS DELIBERATELY NOT.

  dup      Byte-identical to a chunk already kept. Keeps the first, drops the
           rest. Unambiguous: retrieval cannot benefit from returning the same
           text twice, and the duplicate carries no provenance the original lacks.

  boiler   Shares its opening with at least --boiler-min other chunks. This is the
           signature of navigation furniture: licence footers, category farms,
           drug navboxes. A real article does not begin the same way as 5,000
           others.

  lowprose NOT APPLIED BY DEFAULT, and this is a judgement rather than an
           oversight. iFixit is 38% low-prose - but iFixit is a repair manual, and
           "Step 3: remove the two 4mm screws" is terse on purpose, not junk. A
           blanket prose filter would delete the most practical content in the
           archive to fix a Wikipedia problem. Use --lowprose only after reading
           what --inspect shows for the artifact you mean to filter.
"""

import os, sys, json, re, glob, hashlib, argparse
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(ROOT, "10-index")
CHUNKS = os.path.join(OUT, "chunks")
EXCLUDE = os.path.join(OUT, "excluded-ids.txt")

_W = re.compile(r"\s+")
_SENT = re.compile(r"[.!?]")


def prose_score(t):
    w = t.split()
    return 99.0 if len(w) < 20 else 100.0 * len(_SENT.findall(t)) / len(w)


def scan(files, boiler_min, prose_min, use_lowprose):
    """Two passes. First counts openings, second decides. Returns everything the
    caller needs to JUDGE the decision, not merely to apply it."""
    prefix = Counter()
    for f in files:
        with open(f, encoding="utf-8") as fh:
            for line in fh:
                t = _W.sub(" ", json.loads(line)["text"]).strip()
                prefix[hashlib.sha1(t[:240].encode("utf-8", "ignore")).hexdigest()] += 1

    seen = {}                       # text hash -> artifact that kept it first
    ids = []
    per_art = defaultdict(Counter)
    # Samples are collected PER ARTIFACT. The first version collected the first 400
    # of each reason globally, which meant every example came from whichever file
    # sorted first - and made the output useless for judging any other artifact.
    samples = defaultdict(lambda: defaultdict(list))
    # Which artifact absorbed whose duplicates. Deduplicating two overlapping
    # Wikipedia dumps is correct; letting alphabetical order silently decide which
    # one gets cited is not, so the transfer is made visible.
    lost_to = defaultdict(Counter)

    for f in files:
        name = os.path.basename(f)[:-6]
        with open(f, encoding="utf-8") as fh:
            for line in fh:
                m = json.loads(line)
                t = _W.sub(" ", m["text"]).strip()
                h = hashlib.sha1(t.encode("utf-8", "ignore")).hexdigest()
                pk = hashlib.sha1(t[:240].encode("utf-8", "ignore")).hexdigest()
                reason = None
                if h in seen:
                    reason = "dup"
                    lost_to[name][seen[h]] += 1
                elif prefix[pk] >= boiler_min:
                    reason = "boiler"
                elif use_lowprose and prose_score(t) < prose_min:
                    reason = "lowprose"
                if reason:
                    per_art[name][reason] += 1
                    ids.append(m["id"])
                    if len(samples[name][reason]) < 3:
                        samples[name][reason].append((m["id"], t[:150],
                                                      prefix[pk] if reason == "boiler" else 0))
                else:
                    seen[h] = name
                    per_art[name]["kept"] += 1
    return ids, per_art, samples, lost_to


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--inspect", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--boiler-min", type=int, default=50,
                    help="an opening shared with this many chunks is furniture")
    ap.add_argument("--lowprose", action="store_true")
    ap.add_argument("--prose-min", type=float, default=1.5)
    ap.add_argument("--no-cross-dedupe", action="store_true",
                    help="deduplicate only WITHIN an artifact, so no source loses "
                         "its content to another that happens to sort earlier")
    args = ap.parse_args()
    if not (args.inspect or args.apply):
        sys.exit("choose --inspect or --apply")

    files = sorted(glob.glob(os.path.join(CHUNKS, "*.jsonl")))
    if not files:
        sys.exit("no chunks found")

    if args.no_cross_dedupe:
        ids, per_art, samples, lost_to = [], defaultdict(Counter), defaultdict(lambda: defaultdict(list)), defaultdict(Counter)
        for f in files:
            i2, p2, s2, l2 = scan([f], args.boiler_min, args.prose_min, args.lowprose)
            ids += i2
            for k, v in p2.items():
                per_art[k].update(v)
            for k, v in s2.items():
                samples[k] = v
    else:
        ids, per_art, samples, lost_to = scan(files, args.boiler_min, args.prose_min, args.lowprose)

    kept = sum(c["kept"] for c in per_art.values())
    total = kept + len(ids)
    counts = Counter()
    for c in per_art.values():
        for r in ("dup", "boiler", "lowprose"):
            counts[r] += c[r]

    print("chunks %s   keep %s   exclude %s (%.1f%%)   [%s]\n"
          % ("{:,}".format(total), "{:,}".format(kept), "{:,}".format(len(ids)),
             100.0 * len(ids) / max(1, total),
             "within-artifact dedupe" if args.no_cross_dedupe else "cross-artifact dedupe"))
    for r in ("dup", "boiler", "lowprose"):
        if counts[r]:
            print("  %-9s %9s" % (r, "{:,}".format(counts[r])))

    # An artifact reduced to almost nothing is a finding, not a statistic.
    gutted = [(n, c["kept"], c["kept"] + c["dup"] + c["boiler"] + c["lowprose"])
              for n, c in per_art.items()]
    gutted = [(n, k, t) for n, k, t in gutted if t > 20 and k < 0.25 * t]
    if gutted:
        print("\n  ARTIFACTS REDUCED BELOW A QUARTER OF THEIR CHUNKS")
        print("  Their text survives under another artifact's citation. Decide whether")
        print("  that is acceptable before applying - --no-cross-dedupe avoids it.")
        for n, k, t in sorted(gutted, key=lambda x: x[1] / float(x[2])):
            print("    %-46s %s of %s kept (%.0f%%)"
                  % (n[:46], "{:,}".format(k), "{:,}".format(t), 100.0 * k / t))
            for other, cnt in lost_to.get(n, Counter()).most_common(2):
                print("        %s chunks absorbed by %s" % ("{:,}".format(cnt), other[:52]))

    if args.inspect:
        print("\n  %-46s %9s %9s %9s %9s" % ("artifact", "kept", "dup", "boiler", "lowprose"))
        for n, c in sorted(per_art.items(),
                           key=lambda kv: -(kv[1]["dup"] + kv[1]["boiler"] + kv[1]["lowprose"]))[:16]:
            print("  %-46s %9s %9s %9s %9s" % (
                n[:46], "{:,}".format(c["kept"]), "{:,}".format(c["dup"]),
                "{:,}".format(c["boiler"]), "{:,}".format(c["lowprose"])))

        print("\n=== SAMPLES, PER ARTIFACT (this is the part to actually read) ===")
        for n, c in sorted(per_art.items(),
                           key=lambda kv: -(kv[1]["dup"] + kv[1]["boiler"]))[:6]:
            print("\n%s" % n)
            for r in ("dup", "boiler", "lowprose"):
                for i, (cid, txt, seen_n) in enumerate(samples[n].get(r, [])):
                    tag = "%s x%d" % (r, seen_n) if seen_n else r
                    print("  [%-10s] %s" % (tag, txt[:120]))
        print("\nNothing was written. Re-run with --apply to write the exclusion list.")
        return

    with open(EXCLUDE, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("# written by bin/index-filter.py  (%s)\n"
                 % ("within-artifact dedupe" if args.no_cross_dedupe else "cross-artifact dedupe"))
        fh.write("# %s ids excluded from the Chroma index. The chunks and vectors\n"
                 "# themselves are untouched - delete this file and reload to undo.\n"
                 % "{:,}".format(len(ids)))
        for i in ids:
            fh.write(i + "\n")
    print("\nwritten: %s" % EXCLUDE)
    print("now: python bin/index-build.py --load-only")


if __name__ == "__main__":
    main()
