#!/usr/bin/env python3
"""
measure-retrieval.py - how much of a result page is the same passage twice.

    python bin/measure-retrieval.py                 # keyword, needs no model
    python bin/measure-retrieval.py --mode hybrid   # needs the keeper venv
    python bin/measure-retrieval.py --show          # print the duplicate pairs
    python bin/measure-retrieval.py --scoped        # the UNSHIPPED scoped rule
    python bin/measure-retrieval.py --scoped --show 12   # + pairs to read

IT CALLS THE SHIPPED CODE, NOT A COPY OF IT. Every number comes from
`serve.py`'s own `search()` by import - the same function the browser reaches.
The first version of this script reimplemented that filter loop so it could
count what the loop discarded, and it was wrong the moment the loop changed: a
measurement that mirrors the code measures the mirror. `measure-spatial-cues.py`
makes the same argument at length, and this build has been bitten by it twice.

WHY THIS EXISTS. `07-corpora-supplemental` carries two overlapping Wikipedia
medical mirrors - `mdwiki_en_all_maxi` (src 1, 300,100 chunks) and
`wikipedia_en_medicine_maxi` (src 2, 304,769) - which hold many of the same
articles. The surface suppressed repeats of one document by keying on
`(src, dnum)`, so the SAME PASSAGE arriving from two artifacts passed the filter
twice and took two slots on a page of ten. Measured 2026-09-08 on one query:
40 results, 28 distinct passages, 12 duplicate groups.

WHAT COUNTS AS A DUPLICATE HERE. Byte-identical text after whitespace
normalisation, which is what the surface itself compares. That is deliberately
strict: near-duplicates exist too (a paragraph re-worded between mirror
snapshots) and are NOT counted, so every number this prints is a floor rather
than an estimate. A looser rule would need a threshold, and a threshold needs
its own measurement.

THE QUERIES ARE REAL AND THEY ARE LISTED. Twenty-four questions spanning the
shelves the index actually holds - medicine, water, repair, agriculture,
electrical - plus the five from `serve.py`'s own selftest so the two agree.
Listed in full below rather than summarised, so anyone can disagree by reading
them.

AND THE ORIGINAL TWENTY-FOUR ARE NOW FROZEN, 2026-09-15. Pass 2 added 4,610,461
chunks over 19 artifacts - physics, chemistry, biology, astronomy, earthscience,
unix, networking, security, retrocomputing, electronics, arduino, raspberrypi,
dsp, iot, mechanics, engineering, robotics and two textbook directories - and
NOT ONE of those twenty-four questions lands in any of them. A query set that
never reaches two thirds of the index measures the easy third and reports it as
the index. That is the same defect as `serve.py`'s five selftest probes, all of
which were medical or water against the same pass, corrected the same day.

The twenty-four are kept EXACTLY as they were so the 2026-09-08 duplicate
measurement and the 2026-09-11 filter sweep stay comparable - changing a
baseline to improve it is how a comparison stops being one. `QUERIES_PASS2`
below is additive, and `QUERIES_ALL` is what a tool asks for when it wants the
index as it now stands rather than as it stood on 09-08.

WHAT QUERIES_PASS2 IS NOT. It is HAND-WRITTEN, like the twenty-four, and the
2026-09-08 lesson applies to it in full: a hand-written set of twelve decided an
answer before the metric did, and 120 real questions reversed it. For recall
against exhaustive search that matters less than it did there - the query only
chooses which neighbourhood gets probed, and any honest spread does that job -
but it is not a sample of anything. THE MEASURED VERSION IS AVAILABLE AND IS NOT
BUILT YET: `provenance.sqlite3` holds the real question titles of every Stack
Exchange artifact, which is the population `measure-spatial-cues.py` already
draws its 83,856 from. Sampling those gives an unbiased query set over the new
material, with the caveat that a title taken from the corpus has itself as its
own nearest neighbour, which is the degenerate case the 2026-09-15 `--check`
work showed is easy to mistake for a result.
"""

import argparse
import collections
import hashlib
import importlib.util
import os
import random
import re
import sys

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.environ.get("ARK_ROOT") or os.path.dirname(HERE)
API = os.path.join(ROOT, "13-ark-node", "ark-api")
if API not in sys.path:
    sys.path.insert(0, API)
import store                                                   # noqa: E402
import serve                                                   # noqa: E402

# The five serve.py --selftest asks, so a change that helps there shows here too.
QUERIES = [
    "how do I make flood water safe to drink",
    "what do I do for someone in shock",
    "como purificar agua despues de una inundacion",
    "how much water does a person need per day in an emergency",
    "signs of dehydration in a child",
    # medicine
    "treating a deep wound without stitches",
    "how much chlorine to disinfect drinking water",
    "signs of a broken rib",
    "what antibiotic for a urinary infection",
    "how to reduce a dislocated shoulder",
    "symptoms of tetanus",
    # water and hydrology
    "groundwater in Nevada and Utah",
    "how to dig a well by hand",
    "arsenic in well water",
    "building a slow sand filter",
    # agriculture
    "spate irrigation",
    "how deep to plant maize",
    "storing grain without pesticide",
    "diagnosing nitrogen deficiency in wheat",
    # repair and electrical
    "replacing a laptop screen",
    "wiring a three way switch",
    "how to solder a broken wire",
    "sharpening a chisel",
    "repairing a bicycle inner tube",
]

# ---------------------------------------------------------------------------
# PASS 2, added 2026-09-15. Additive. See the docstring for why QUERIES above is
# frozen rather than extended. Grouped by the shelf each one is meant to reach,
# so a group that stops returning its own corpus is visible as a group.
#
# Three of these are word for word the three probes added to `serve.py
# --selftest` the same day, for the reason the original five were: a change that
# helps in one place should show in the other.
QUERIES_PASS2 = [
    # unix / networking / security / retrocomputing
    "no space left on device but df shows free space",
    "recovering a filesystem that will not mount",
    "setting a static ip address on a local network",
    "generating an ssh key pair and where it is stored",
    "reading a floppy disk on modern hardware",
    # electronics / arduino / raspberrypi / dsp / iot
    "choosing a pull-up resistor value",
    "powering a raspberry pi from a solar panel",
    "debouncing a mechanical switch",
    "reading a datasheet for a voltage regulator",
    # mechanics / engineering / robotics
    "torque needed to lift a load with a lead screw",
    "why a diesel engine will not start in the cold",
    "setting the current limit on a stepper motor driver",
    # physics / chemistry / biology / astronomy / earthscience
    "difference between phase velocity and group velocity",
    "how to neutralize an acid spill safely",
    "finding true north from the stars",
    "why does water expand when it freezes",
    "what causes an earthquake aftershock",
    # libretexts / openstax textbook directories
    "solving a system of linear equations by elimination",
    "what is the ideal gas law used for",
]

# The index as it now stands. NOT proportional to it: pass 2 is two thirds of the
# chunks and gets 19 of 43 questions. Coverage is the goal - every shelf reached
# by something - and a weighted set would need a weighting nobody has measured.
QUERIES_ALL = QUERIES + QUERIES_PASS2


def norm(t):
    return " ".join(t.split())


def fingerprint(t):
    return hashlib.sha1(norm(t).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- scoped mode
SCOPED_K = 400
SCOPED_SEED = 20260908


def _sibling(name):
    """Import a bin/ tool by path. Same reason as store._load_bin: the filenames
    carry hyphens. Used to borrow `titles()` rather than copy the query that
    reads the archive's own Stack Exchange titles out of provenance."""
    spec = importlib.util.spec_from_file_location(
        name.replace("-", "_").replace(".py", ""), os.path.join(HERE, name))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def has(text, words):
    tl = text.lower()
    return any(re.search(r"\b%s\b" % re.escape(w), tl) for w in words)


def scoped(args, S):
    """Does the unshipped scoped rule put the SUBJECT back on the page?

    THE METRIC IS TOPIC PRESENCE, NOT MY OPINION. The defect was not "the
    ranking felt wrong"; it was that `groundwater in Nevada and Utah` returned
    twelve results and **not one of them contained the word groundwater**. That
    is countable without a human in the loop, and it is what this measures:
    of the top N, how many carry at least one topic term, at least one place
    term, and both.

    WHAT IT CANNOT MEASURE. Whether a passage that does contain both is a good
    answer. On a twelve-query hand read the rule produced one large improvement,
    nine no-ops, one wash and one mild regression - `earthquake building codes
    in Japan`, where enforcing the place promoted a US flood timeline over an
    earthen-building text. Relevance stays a hand read, so `--show K` prints K
    sampled before/after pairs to read. **Topic presence carries the argument;
    the hand read is the veto.**

    THE QUESTIONS ARE THE ARCHIVE'S OWN. Every Stack Exchange question title the
    index holds, via `titles()` in measure-spatial-cues.py rather than a second
    copy of that query, filtered to the ones where the rule would actually fire:
    the question must name a place AND have something left over as a subject.
    Sampled with a fixed seed, so the same set comes back on every machine."""
    bm = _sibling("index-bm25.py")
    src = _sibling("measure-spatial-cues.py")
    all_titles = [t for _, t in src.titles("en")]

    pool = []
    for t in all_titles:
        pw = S.place_words(t)
        if not pw:
            continue
        words = bm.content_words(t)
        topic = [w for w in words if w not in pw]
        place = [w for w in words if w in pw]
        if topic and place and bm.query_scoped(topic, place):
            pool.append((t, topic, place))
    rnd = random.Random(SCOPED_SEED)
    rnd.shuffle(pool)
    qs = pool[:args.k]

    print("%d of %s question titles name a place and have a subject left over; "
          "measuring %d of them (seed %d)\n"
          % (len(pool), "{:,}".format(len(all_titles)), len(qs), SCOPED_SEED))

    agg = {"old": collections.Counter(), "new": collections.Counter()}
    moved = 0
    shown = 0
    lost_topic = lost_place = 0          # PAGES that lost the term, not slots
    for t, topic, place in qs:
        old = S.keyword(t, args.n)
        new = S.keyword_scoped(t, args.n)
        if old != new:
            moved += 1
        page = {}
        for label, ids in (("old", old), ("new", new)):
            nt = np_ = 0
            for cid in ids:
                m = S.meta(cid)
                if not m:
                    continue
                txt = S.text(m)
                ht, hp = has(txt, topic), has(txt, place)
                nt += 1 if ht else 0
                np_ += 1 if hp else 0
                agg[label]["slots"] += 1
                agg[label]["topic"] += 1 if ht else 0
                agg[label]["place"] += 1 if hp else 0
                agg[label]["both"] += 1 if (ht and hp) else 0
            page[label] = (nt, np_)
        if page["old"][0] == 0:
            lost_topic += 1
        if page["old"][1] == 0:
            lost_place += 1
        if args.show and shown < args.show and old != new:
            shown += 1
            print("--- %s\n    topic=%s place=%s" % (t, topic, place))
            for label, ids in (("OLD", old[:3]), ("NEW", new[:3])):
                for cid in ids:
                    m = S.meta(cid)
                    body = " ".join(S.text(m).split())[:66] if m else "?"
                    print("    %s %-13s %s" % (label, cid, body))

    print("\n%-8s %8s %8s %8s %8s" % ("", "slots", "topic", "place", "both"))
    for label in ("old", "new"):
        a = agg[label]
        sl = a["slots"] or 1
        print("%-8s %8d %7.1f%% %7.1f%% %7.1f%%"
              % ("shipped" if label == "old" else "scoped", a["slots"],
                 100.0 * a["topic"] / sl, 100.0 * a["place"] / sl,
                 100.0 * a["both"] / sl))
    print("\n  the rule changed the page on %d of %d queries (%.0f%%)"
          % (moved, len(qs), 100.0 * moved / (len(qs) or 1)))

    # THE LINE THAT DECIDED IT, AND THE ONLY ONE THAT MATTERS.
    # The percentages above are per SLOT. The defect this rule was built for is
    # a whole PAGE with no topic term on it - `groundwater in Nevada and Utah`
    # returns twelve results with `groundwater` in none of them. So count pages,
    # not slots. On 2026-09-08 both counts came back ZERO over 120 real
    # questions, which is what rejected the rule: it rewrites three quarters of
    # place-bearing pages to fix something that does not happen.
    print("  shipped pages that lost the SUBJECT entirely: %d of %d (%.1f%%)"
          % (lost_topic, len(qs), 100.0 * lost_topic / (len(qs) or 1)))
    print("  shipped pages that lost the PLACE entirely:   %d of %d (%.1f%%)"
          % (lost_place, len(qs), 100.0 * lost_place / (len(qs) or 1)))
    if not lost_topic:
        print("\n  NOT ONE PAGE LOST ITS SUBJECT. The rule is a rewrite in "
              "search of a defect;\n  the motivating query was written by hand "
              "and is rarer than 1 in %d here." % len(qs))
    print("\n  Relevance is NOT measured here - use --show and read them. A "
          "hand read of twelve\n  queries found one large gain, nine no-ops, "
          "one wash and one regression.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="keyword",
                    choices=["keyword", "hybrid", "dense"],
                    help="keyword needs no model and no Chroma; hybrid and "
                         "dense need the keeper venv")
    ap.add_argument("-n", type=int, default=10,
                    help="results per page, as the surface would return")
    ap.add_argument("--show", nargs="?", type=int, const=10**9, default=0,
                    help="duplicate mode: print every duplicate group. "
                         "scoped mode: print this many before/after pairs")
    ap.add_argument("--scoped", action="store_true",
                    help="measure the UNSHIPPED scoped keyword rule "
                         "(store.keyword_scoped) against the shipped one")
    ap.add_argument("-k", type=int, default=SCOPED_K,
                    help="scoped mode: how many sampled questions")
    args = ap.parse_args()

    if args.scoped:
        serve_store = store.Store()
        return scoped(args, serve_store)

    serve.S = store.Store()          # what serve.py's main() would have built
    S = serve.S

    tot_slots = tot_distinct = tot_pairs = 0
    rep_nav = rep_doc = rep_text = 0
    per_pair = collections.Counter()
    empty = []

    print("mode=%s  n=%d  queries=%d\n" % (args.mode, args.n, len(QUERIES)))
    print("%-52s %5s %5s %5s %5s" % ("query", "slots", "uniq", "dup", "supp"))
    print("-" * 78)

    for q in QUERIES:
        out = serve.search(q, args.n, args.mode)
        kept = out["results"]
        sup = out["suppressed"]
        rep_nav += sup["navigation"]
        rep_doc += sup["same_document"]
        rep_text += sup.get("duplicate_text", 0)

        # THE CHECK IS INDEPENDENT OF THE FIX. This hashes what came back rather
        # than trusting the counter, so a suppression that reports a number and
        # lets the passage through still shows up here as a duplicate.
        groups = collections.defaultdict(list)
        for r in kept:
            m = S.meta(r["cid"])
            groups[fingerprint(r["text"])].append((r["cid"], m["src"] if m else "?"))
        dup_groups = {k: v for k, v in groups.items() if len(v) > 1}
        dup_slots = sum(len(v) - 1 for v in dup_groups.values())

        if not kept:
            empty.append(q)
        tot_slots += len(kept)
        tot_distinct += len(groups)
        tot_pairs += dup_slots
        for v in dup_groups.values():
            per_pair[tuple(sorted(set(str(x) for _, x in v)))] += 1

        print("%-52s %5d %5d %5d %5d"
              % (q[:52], len(kept), len(groups), dup_slots,
                 sup.get("duplicate_text", 0)))
        if args.show and dup_groups:
            for v in dup_groups.values():
                print("        %s" % "  ==  ".join("%s (src %s)" % c for c in v))

    print("-" * 78)
    waste = (100.0 * tot_pairs / tot_slots) if tot_slots else 0.0
    print("%-52s %5d %5d %5d %5d"
          % ("TOTAL", tot_slots, tot_distinct, tot_pairs, rep_text))
    print("\n  %d of %d slots carried a passage already on the page: %.1f%%"
          % (tot_pairs, tot_slots, waste))
    print("  the surface reports it suppressed %d navigation, %d repeats of one "
          "document, %d duplicate passages" % (rep_nav, rep_doc, rep_text))
    if empty:
        print("  %d quer(ies) returned nothing: %s"
              % (len(empty), ", ".join(empty)))

    if per_pair:
        print("\n  which artifacts collide, by number of duplicate groups:")
        names = {}
        for src in set(s for pair in per_pair for s in pair):
            r = S.prov.execute("SELECT file FROM artifact WHERE src=?", (src,))
            row = r.fetchone()
            names[src] = (row[0] if row else "src %s" % src)
        for pair, n in per_pair.most_common(12):
            print("    %3d  %s" % (n, "  +  ".join(names[s] for s in pair)))

    if args.mode != "keyword" and not S.dense_available():
        print("\n  NOTE: dense did not load, so this measured KEYWORD only: %s"
              % S.dense_error())


if __name__ == "__main__":
    main()
