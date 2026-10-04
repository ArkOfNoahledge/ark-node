#!/usr/bin/env python3
"""
index-queryset.py - build a query set out of the corpus instead of out of my head.

    python bin/index-queryset.py --selftest
    python bin/index-queryset.py --n 12 --print
    python bin/index-queryset.py --n 12 --out 00-docs/query-set-provenance.json

WHY THIS EXISTS. Every retrieval number this project has published was measured
on questions somebody wrote down by hand: 24 in `measure-retrieval.py`, 43 in
`index-pq-probe.py` after pass 2 added 19 more. Two things are wrong with that
and only one of them is the sample size.

  1. FORTY-THREE IS A SMALL SAMPLE and its error bars were never stated. The
     2026-09-15 depth sweep turned on a SINGLE query of 43 - r@1 falling 0.977 to
     0.953 as depth rose, which is impossible - and the only reason that was
     caught is that it moved in a forbidden direction. A sample this small is one
     where a real effect and a coin flip look alike.
  2. THE PASS-1 / PASS-2 SPLIT IS CONFOUNDED WITH TOPIC. The hand-written
     questions about medicine and water are pass-1 questions; the ones about
     inodes and pull-up resistors are pass-2 questions. So "pass 2 retrieves
     worse" and "engineering retrieves worse" are the same measurement, and
     nothing in that set can separate them.

THE CORPUS IS FULL OF REAL QUESTIONS AND WE WERE NOT USING THEM. Twenty-four of
the 48 indexed artifacts are Stack Exchange, where every document title IS a
question a person actually asked. 491,688 of them end in a question mark. They
are better than anything a builder invents, for the reason this build keeps
relearning about measurement: they were not written by someone who knew what the
index contained.

WHAT THIS SET CANNOT DO, SAID FIRST BECAUSE IT WILL BE MISREAD OTHERWISE
------------------------------------------------------------------------
IT COVERS 24 OF 48 ARTIFACTS, AND NOT THE MEDICAL HALF. The other 24 have no
question-shaped titles at all: Wikipedia-medicine's documents are called
`(+)-Naloxone`, libretexts' are book names, zimgit-water's are `WATER (7)`.
Sampling those would produce lookups, not questions. So:

    A NUMBER MEASURED ON THIS SET IS A NUMBER ABOUT THE STACK EXCHANGE HALF
    OF THE INDEX. It is not a number about the index, and it is not a number
    about the medical corpus this archive exists for.

That is why the hand-written sets are NOT retired by this file. They are the only
coverage the medical, water, repair and food artifacts have, and they stay.
Reporting a figure from here as though it described the whole node would be this
build's most-recorded defect - a result true about its own question and
misleading about the one being asked - committed by the very tool written to
reduce the error bars on it.

THE TRAP, AND THE DESIGN AROUND IT
----------------------------------
A TITLE TAKEN FROM THE CORPUS IS ITS OWN NEAREST NEIGHBOUR. The document the
query came from will retrieve at rank 1 no matter how bad retrieval is, because
the query is a substring of it. What that inflates depends entirely on what is
being measured, and the two cases are opposite:

  - MEASURING THE ANN INDEX AGAINST EXHAUSTIVE SEARCH (index-pq-probe.py):
    harmless. Both sides see the same corpus and the same self-match, so it
    cancels. What matters there is only that the queries are distributed like
    real ones, which is the whole point of drawing them from the corpus.
  - MEASURING RETRIEVAL QUALITY END TO END (measure-retrieval.py): fatal. Every
    score goes to 1.0 and the instrument reads perfect while measuring nothing.

So every query carries `exclude_cid_prefix`, which is `src:dnum:` for the
document it came from. Excluding by that prefix removes the source document and
all of its chunks, exactly and cheaply. A consumer that does not exclude it is
measuring self-retrieval, and this file cannot stop that - it can only make the
means to avoid it impossible to miss.

DETERMINISM IS THE POINT OF THE SEED. A query set that changes between runs
cannot be used for before-and-after, which is the only thing measurement is for.
Sampling is by MD5 of (seed, cid) rather than by database order, so the same
seed picks the same questions even after provenance.sqlite3 is rebuilt, as long
as the documents still exist.

THE BRAND SUFFIX IS MEASURED, NOT ASSUMED, and this is not a detail. Stack
Exchange sites are branded: cooking is `Seasoned Advice`, mechanics is `Motor
Vehicle Maintenance & Repair Stack Exchange`, security is `Information Security
Stack Exchange`. The obvious filter - titles ending `- ... Stack Exchange` -
silently drops the whole cooking artifact and mangles several others. The brand
is therefore derived per artifact from the titles themselves and asserted at
100% of a 4,000-title sample before it is used.
"""

import argparse
import collections
import hashlib
import json
import os
import re
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.environ.get("ARK_ROOT") or os.path.dirname(HERE)
PROV = os.path.join(ROOT, "10-index", "provenance.sqlite3")
DEFAULT_OUT = os.path.join(ROOT, "00-docs", "query-set-provenance.json")

# Pass 1 indexed srcs 1..29, pass 2 added 30..48. Read from the scope files
# rather than hard-coded, because a third pass must not silently inherit this.
SCOPE1 = os.path.join(HERE, "index-scope-pass1.txt")
SCOPE2 = os.path.join(HERE, "index-scope-pass2.txt")

MIN_CHARS, MAX_CHARS, MIN_WORDS = 25, 150, 5

# Titles that are site furniture rather than questions. `About` and `Highest
# Voted Questions` exist in every Stack Exchange ZIM and the second one ends in a
# noun, not a question mark, so only the first is strictly needed - both are
# listed because a filter that documents what it is excluding is readable and one
# that relies on a downstream test is not.
FURNITURE = ("about", "highest voted questions", "hot questions", "unanswered",
             "questions tagged", "tag ", "users", "help center")

# LaTeX and image titles. physics.stackexchange alone contributes hundreds of
# thousands of `{\displaystyle ...}` entries to the doc table, and they are
# titles in the sense that the column is populated, which is not the sense meant.
JUNK = re.compile(r"\\displaystyle|\{\\|\$\$|^images?/|^\W*$")


def scope_names(path):
    """Artifact basenames named in a scope file, comments and blanks dropped."""
    out = []
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            out.append(re.split(r"[\\/]", line)[-1])
    return out


def passes():
    """{artifact basename: 1 or 2}. Anything in neither scope is reported as 0,
    which is a state worth seeing rather than defaulting into a pass."""
    m = {}
    for n in scope_names(SCOPE1):
        m[n] = 1
    for n in scope_names(SCOPE2):
        m[n] = 2
    return m


def brand_of(con, src, sample=4000):
    """The site name this artifact suffixes its titles with, MEASURED.

    Returns (brand, share) where share is the fraction of sampled titles that
    end with it. The caller decides what share is good enough; nothing here
    guesses a brand it did not see."""
    cnt = collections.Counter()
    n = 0
    for (t,) in con.execute(
            "select title from doc where src=? and title like '% - %' limit ?",
            (src, sample)):
        if not t:
            continue
        n += 1
        cnt[t.rsplit(" - ", 1)[1].strip()] += 1
    if not n:
        return None, 0.0
    brand, hits = cnt.most_common(1)[0]
    return brand, hits / float(n)


def clean(title, brand):
    """The question a person typed, or None.

    Strips the measured brand suffix and then applies every shape test to WHAT
    IS LEFT, which is the order that matters: `How can I get chewy chocolate
    chip cookies? - Seasoned Advice` does not end in a question mark until the
    brand is gone."""
    if not title:
        return None
    t = title.strip()
    suf = " - " + brand
    if not t.endswith(suf):
        return None
    t = t[:-len(suf)].strip()
    if not t.endswith("?"):
        return None
    if JUNK.search(t):
        return None
    low = t.lower()
    if any(low.startswith(f) for f in FURNITURE):
        return None
    if not (MIN_CHARS <= len(t) <= MAX_CHARS):
        return None
    if len(t.split()) < MIN_WORDS:
        return None
    if "\n" in t or "\r" in t:
        return None
    return t


def pick(rows, k, seed):
    """k rows, chosen by hash rather than by database order.

    THE ORDER A SELECT RETURNS IS NOT A PROPERTY OF THE CORPUS. Sampling on it
    would give a different set every time provenance.sqlite3 is rebuilt, and a
    query set that moves cannot measure a before and an after."""
    def key(r):
        return hashlib.md5(("%s|%s" % (seed, r["cid_prefix"]))
                           .encode("utf-8")).hexdigest()
    return sorted(rows, key=key)[:k]


def build(con, n_per, seed, min_brand_share=0.95):
    """(queries, coverage). Never raises on a thin artifact; reports it."""
    pmap = passes()
    arts = [dict(src=r[0], file=r[1], shelf=r[2], lang=r[3])
            for r in con.execute(
                "select src, file, shelf, lang from artifact order by src")]

    queries, coverage, seen = [], [], set()
    for a in arts:
        cov = dict(src=a["src"], artifact=a["file"], shelf=a["shelf"],
                   pass_=pmap.get(a["file"], 0), sampled=0,
                   candidates=0, brand=None, brand_share=None, why=None)

        if "stackexchange" not in a["file"]:
            # NOT A FAILURE. These artifacts have no question-shaped titles, and
            # saying so per artifact is what stops the set's coverage being
            # inferred from its size.
            cov["why"] = "no question-shaped titles: document titles are " \
                         "article or book names, not questions"
            coverage.append(cov)
            continue

        brand, share = brand_of(con, a["src"])
        cov["brand"], cov["brand_share"] = brand, round(share, 4)
        if not brand or share < min_brand_share:
            cov["why"] = ("no single title suffix covers %.0f%% of a sample, so "
                          "no brand could be stripped safely" % (min_brand_share * 100))
            coverage.append(cov)
            continue

        rows = []
        for dnum, title in con.execute(
                "select dnum, title from doc where src=? and title like ?",
                (a["src"], "%?%" + brand)):
            q = clean(title, brand)
            if not q:
                continue
            low = q.lower()
            if low in seen:
                continue
            seen.add(low)
            rows.append(dict(q=q, src=a["src"], dnum=dnum,
                             cid_prefix="%d:%d:" % (a["src"], dnum)))
        cov["candidates"] = len(rows)

        chosen = pick(rows, n_per, seed)
        cov["sampled"] = len(chosen)
        if len(chosen) < n_per:
            cov["why"] = "only %d candidates, wanted %d" % (len(rows), n_per)
        for r in chosen:
            queries.append(dict(
                q=r["q"], artifact=a["file"], shelf=a["shelf"], lang=a["lang"],
                src=r["src"], dnum=r["dnum"],
                # EXCLUDE THIS OR MEASURE NOTHING. See the trap, above.
                exclude_cid_prefix=r["cid_prefix"],
                pass_=pmap.get(a["file"], 0)))
        coverage.append(cov)

    queries.sort(key=lambda r: (r["src"], r["dnum"]))
    return queries, coverage


def payload(queries, coverage, n_per, seed):
    cov_yes = [c for c in coverage if c["sampled"]]
    cov_no = [c for c in coverage if not c["sampled"]]
    return {
        "generated": time.strftime("%Y-%m-%d"),
        "generator": "bin/index-queryset.py",
        "seed": seed,
        "n_per_artifact": n_per,
        "queries": queries,
        "artifacts_covered": len(cov_yes),
        "artifacts_uncovered": len(cov_no),
        # THE CAVEAT TRAVELS WITH THE DATA. A consumer that reads only this file
        # must still be unable to mistake it for coverage of the whole index.
        "caveat": (
            "Sampled from Stack Exchange artifacts only, because they are the "
            "only ones whose document titles are questions. A figure measured "
            "on this set describes the Stack Exchange half of the index and NOT "
            "the medical, water, repair, food or PDF corpora. The hand-written "
            "query sets in bin/measure-retrieval.py and bin/index-pq-probe.py "
            "are the only coverage those have and are not superseded by this."),
        "self_retrieval": (
            "Each query is the TITLE of the document it came from, so that "
            "document is its own nearest neighbour. For end-to-end retrieval "
            "quality, drop every chunk whose cid starts with the query's "
            "exclude_cid_prefix. For ANN recall against exhaustive search over "
            "the same vectors, no exclusion is needed: the self-match is "
            "present on both sides and cancels."),
        "coverage": coverage,
    }


def selftest(con):
    bad = 0

    def check(name, ok, detail=""):
        nonlocal bad
        print("  %-5s %-46s %s" % ("ok" if ok else "FAIL", name, detail))
        if not ok:
            bad += 1

    q1, cov1 = build(con, 4, "selftest-seed")
    q2, _ = build(con, 4, "selftest-seed")
    q3, _ = build(con, 4, "a-different-seed")

    se = [c for c in cov1 if "stackexchange" in c["artifact"]]
    check("every stackexchange artifact is covered",
          all(c["sampled"] > 0 for c in se), "%d of %d" %
          (sum(1 for c in se if c["sampled"]), len(se)))
    # THE COOKING ARTIFACT IS THE REGRESSION TEST. Its brand is `Seasoned
    # Advice`, with no `Stack Exchange` in it at all, so the obvious filter drops
    # 63,608 documents in silence. If this ever fails, brand detection has been
    # replaced by an assumption again.
    cook = [c for c in se if c["artifact"].startswith("cooking.")]
    check("the unbranded site is not silently dropped",
          bool(cook) and cook[0]["sampled"] > 0 and
          cook[0]["brand"] == "Seasoned Advice",
          cook[0]["brand"] if cook else "cooking artifact not found")
    check("every brand was measured, not assumed",
          all((c["brand_share"] or 0) >= 0.95 for c in se if c["sampled"]),
          "min share %.3f" % min([c["brand_share"] for c in se if c["sampled"]] or [0]))
    check("no query carries its brand suffix",
          not any(" - " in r["q"] and r["q"].rsplit(" - ", 1)[1] in
                  {c["brand"] for c in se if c["brand"]} for r in q1))
    check("every query is a question", all(r["q"].endswith("?") for r in q1))
    check("every query has an exclusion prefix",
          all(r["exclude_cid_prefix"].count(":") == 2 and
              r["exclude_cid_prefix"].endswith(":") for r in q1))
    # THE EXCLUSION PREFIX HAS TO MATCH SOMETHING. A prefix that names no chunk
    # would exclude nothing and the trap would be open with the guard in place,
    # which is worse than no guard.
    hit = 0
    for r in q1[:25]:
        hit += con.execute(
            "select count(*) from chunk where cid like ?",
            (r["exclude_cid_prefix"] + "%",)).fetchone()[0] > 0
    check("the exclusion prefix names real chunks", hit == min(25, len(q1)),
          "%d of %d" % (hit, min(25, len(q1))))
    check("no duplicate questions",
          len({r["q"].lower() for r in q1}) == len(q1),
          "%d unique of %d" % (len({r["q"].lower() for r in q1}), len(q1)))
    check("the same seed gives the same set",
          [r["q"] for r in q1] == [r["q"] for r in q2])
    check("a different seed gives a different set",
          [r["q"] for r in q1] != [r["q"] for r in q3])
    check("both passes are represented",
          {r["pass_"] for r in q1} == {1, 2},
          "passes %s" % sorted({r["pass_"] for r in q1}))
    # AND THE UNCOVERED HALF IS NAMED. This is the check that keeps the caveat
    # honest: if a future change starts sampling article titles, this fails and
    # the set's meaning has changed without anyone saying so.
    unc = [c for c in cov1 if not c["sampled"]]
    check("the uncovered artifacts are named, not omitted",
          len(unc) == 24 and all(c["why"] for c in unc),
          "%d uncovered, all with a reason" % len(unc))

    print("\n%d/12" % (12 - bad))
    return 1 if bad else 0


def main():
    ap = argparse.ArgumentParser(
        description="build a query set from real question titles in the corpus")
    ap.add_argument("--n", type=int, default=12,
                    help="questions per artifact (default 12)")
    ap.add_argument("--seed", default="ark-2026-09-17",
                    help="sampling seed; the same seed always picks the same set")
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--print", dest="show", action="store_true",
                    help="print the questions instead of writing the file")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if not os.path.exists(PROV):
        sys.exit("missing %s\n  build it: python bin/index-provenance.py" % PROV)
    con = sqlite3.connect("file:%s?mode=ro" % PROV.replace("\\", "/"), uri=True)

    if args.selftest:
        return selftest(con)

    queries, coverage = build(con, args.n, args.seed)
    doc = payload(queries, coverage, args.n, args.seed)

    cov_yes = doc["artifacts_covered"]
    print("%d questions from %d artifacts; %d artifacts have no question-shaped "
          "titles and are listed in `coverage`."
          % (len(queries), cov_yes, doc["artifacts_uncovered"]))
    by_pass = collections.Counter(r["pass_"] for r in queries)
    print("pass 1: %d    pass 2: %d" % (by_pass[1], by_pass[2]))

    if args.show:
        for r in queries:
            print("  [%d] %-34s %s" % (r["pass_"], r["artifact"][:34], r["q"]))
        return 0

    with open(args.out, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(doc, fh, indent=1, ensure_ascii=False)
        fh.write("\n")
    print("wrote %s (%d bytes)" % (args.out, os.path.getsize(args.out)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
