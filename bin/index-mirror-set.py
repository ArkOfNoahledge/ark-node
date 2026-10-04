#!/usr/bin/env python3
"""
index-mirror-set.py - which mdwiki articles are a copy of the Wikipedia one.

    python bin/index-mirror-set.py              write the set at the default threshold
    python bin/index-mirror-set.py --report     measure and print, write nothing
    python bin/index-mirror-set.py --threshold 0.98   (the band below 0.99)
    python bin/index-mirror-set.py --show 0.90 0.98    name articles in a band

WHY THIS EXISTS, AND WHY IT REPLACED A ONE-LINE RULE.
`mdwiki_en_all_maxi` shares 69,980 article paths with `wikipedia_en_medicine_maxi`
- 97.7% of it - so the first mirror rule excluded every shared path and kept the
newer Wikipedia copy. Reading twenty of the pairs that differ (bin/compare-revisions.py,
2026-09-11) showed that rule was wrong on the class this archive exists for.

**mdwiki is not an old copy of Wikipedia.** It is WikiProject Medicine's offline
build, and its articles carry a structured clinical infobox - symptoms, causes,
risk factors, diagnostic method, differential diagnosis, treatment, prognosis,
frequency, main uses, typical dose - that the 2026-04 Wikipedia articles have in
many cases collapsed to a single `Specialty:` line. Eight of the twenty read by
hand carried something actionable the newer article had lost:

    Fixed_drug_reaction            5,179 chars -> 1,079. No treatment, no
                                   diagnosis, no differential in the new one
    Transfusion-dependent_anemia   "restrictive threshold 7 to 8 g/dL, liberal
                                   9 to 10" -> "a range between 6 and 10 may be
                                   considered, depending on circumstances"
    Tafamidis                      20 to 80 mg once daily -> no dose at all
    Risankizumab                   150 mg at week 0, 4, then q12w -> no dose
    Baylisascaris_procyonis        "20% bleach (1% sodium hypochlorite)" -> the
                                   sentence kept, the concentration dropped
    Carcinoid, Atrial_septal_defect, Mansonelliasis
                                   infobox with risk factors, frequency,
                                   differential and treatment -> Specialty only

and five were clearly better in Wikipedia, mostly on CURRENCY of indications:
Risankizumab gained Crohn's and ulcerative colitis, Oxaliplatin gained pancreatic
and gastric and the CAPOX regimen, Dubin-Johnson gained a histology section.

**So neither side wins, and that is the finding.** Wikipedia is more current on
what a drug is approved for; mdwiki is more complete on how much to give.
Suppressing an identical copy removes noise. Suppressing a differing revision
hides a dose. This script separates the two.

HOW IT DECIDES, AND WHY NOT BY SIMILARITY.
The test is ASYMMETRIC: what fraction of the mdwiki article already appears in
the Wikipedia one. That is the question the rule needs answered - *does mdwiki
add anything* - and it is not the same as *are these similar*.

Symmetric similarity is wrong in both directions here:

  - `difflib` is ORDER-SENSITIVE. `Atrial_septal_defect` scores **0.062** while
    being largely the same prose in a different section order. Dropping on low
    similarity would keep two near-copies.
  - Plain Jaccard is order-INSENSITIVE and symmetric, so the same article scores
    high and mdwiki's infobox - the thing worth keeping - is invisible to it.

Coverage of mdwiki by Wikipedia answers it directly: reordering does not change
it, and a paragraph present in only one of them does.

AND LENGTH IS NOT A PROXY, which is why this needs the text at all.
`Atrial_septal_defect` is 30,236 characters against 31,290, a 3% difference, at
difflib similarity 0.062. Nothing in `provenance.sqlite3` can tell those apart,
so the comparison has to read both .jsonl files - about 1.1 GB - and that is why
this is an offline pass writing a file rather than a rule computed at startup.

WHAT IT WRITES, AND HOW THAT FILE IS KEPT HONEST.
`10-index/mirror-mdwiki.json`, a list of mdwiki `dnum` values plus a STAMP: the
size and mtime of both .jsonl files and of provenance.sqlite3. `store.py` checks
the stamp and, when it does not match, DISABLES the mirror rule and says so
rather than filtering on a stale set. That is the same guard `kiwix-books.json`
carries against a rebuilt `library.xml`, written for the same reason: on
2026-09-04 a stale generated file produced citations that looked verified and
404'd, and the only symptom was a dead link.

A generated file inside a checksummed shelf changes that shelf's manifest, so
`bin/rehash.sh 10-index` after running this.

MEMORY. About 500 MB. mdwiki's shingles are held as `array('I')` of 32-bit
hashes - 4 bytes each rather than the ~40 a Python set costs - and Wikipedia is
streamed one document at a time and freed.

Standard library only.
"""

import argparse
import array
import collections
import json
import os
import re
import sqlite3
import sys
import time
import zlib

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.environ.get("ARK_ROOT") or os.path.dirname(HERE)
INDEX = os.path.join(ROOT, "10-index")
PROV = os.path.join(INDEX, "provenance.sqlite3")
CHUNKS = os.path.join(INDEX, "chunks")
OUT = os.path.join(INDEX, "mirror-mdwiki.json")

OLD_PREFIX = "mdwiki_"
NEW_PREFIX = "wikipedia_en_medicine"

# WHAT THE THRESHOLD MEANS, MEASURED TWICE - ON SYNTHETIC ARTICLES AND ON THE
# REAL 69,980 PAIRS. `--selftest` reproduces the first table; `--report`
# reproduced the second on 2026-09-12 and is what set this number.
#
#   sensitivity, synthetic 4,000-word article
#     sections reordered, not one word changed        0.993
#     2% of words reworded (ordinary revision drift)  0.902
#     mdwiki carries 2% unique content                0.980
#     mdwiki carries 4% unique content                0.962
#     wikipedia ADDED a whole section                 1.000
#     wikipedia kept only 20% of the article          0.199
#
#   the real distribution, all 69,980 shared paths
#     exactly 1.000   41,875   59.8%
#     0.99 to 1.00     5,133    7.3%
#     0.98 to 0.99     3,779    5.4%
#     0.95 to 0.98     5,488    7.8%
#     0.90 to 0.95     4,317    6.2%
#     0.75 to 0.90     4,469    6.4%
#     0.50 to 0.75     3,186    4.6%
#     0.00 to 0.50     1,733    2.5%
#
# THERE IS NO KNEE IN THAT CURVE. 59.8% sits at exactly 1.000 and the rest is a
# smooth tail, so the threshold cannot be read off a gap in the data and has to
# be argued. The argument is that the two errors are not symmetric:
#
#   dropping too much loses a dose SILENTLY - the operator sees the Wikipedia
#     article and nothing says the other one existed
#   dropping too little costs two near-identical results competing for slots,
#     which is VISIBLE, and the byte-identical ones are caught anyway by the
#     `duplicate_text` suppressor in serve.search
#
# 0.99 RATHER THAN 0.98, AND THE 0.98-0.99 BAND IS WHY. Forty of those 3,779
# were named on 2026-09-12 and they are not taxonomy: `Drug_overdose`,
# `Ferritin`, `ABCD2_score`, `Topical_medication`, `Cholecystectomy`,
# `Albaconazole`, `Drotaverine`, `Norethandrolone`. Most of that missing 1-2% is
# probably mdwiki's classification footer - `ICD-10: Q21.1 ... MedlinePlus:
# 000157`, 30 to 60 words, present in every mdwiki medical article and dropped
# by Wikipedia 2026-04 - which would make the band safe. **Probably is not a
# measurement**, and buying certainty costs 3,779 articles, about 16,000 chunks,
# 0.7% of the index. At 0.99 every article dropped has under 1% unique text,
# below the size of one infobox row on any article long enough to hold a dose.
#
# THE EIGHT READ BY HAND LAND 0.047 TO 0.872, so the fixtures clear this
# threshold by a margin of 0.118 and the choice is not delicate. Raising it
# further buys nothing; lowering it re-enters the band above.
THRESHOLD = 0.99

SHINGLE = 5          # words per shingle; below this a doc is compared exactly

# THE FIXTURES ARE THE ARTICLES THAT WERE READ BY HAND on 2026-09-11, and they
# are asserted rather than described. A threshold that drops Tafamidis is wrong
# no matter how good its histogram looks.
MUST_KEEP = [
    "Fixed_drug_reaction", "Transfusion-dependent_anemia", "Tafamidis",
    "Risankizumab", "Baylisascaris_procyonis", "Carcinoid",
    "Atrial_septal_defect", "Mansonelliasis",
]
MUST_DROP = [
    "Parkland_formula", "Penicillium_phoeniceum", "Prepuce", "Rib_removal",
]

_WORD = re.compile(r"[a-z0-9]+")


def shingles(text):
    """Hashed word 5-grams, deduplicated. Returns a set of 32-bit ints.

    Lowercased and stripped of punctuation first, so a curly quote or an en dash
    swapped for a hyphen - which the 2026-04 Wikipedia articles do constantly -
    is not read as new content."""
    w = _WORD.findall(text.lower())
    if len(w) < SHINGLE:
        return None                      # too short to shingle; compared exactly
    out = set()
    for i in range(len(w) - SHINGLE + 1):
        out.add(zlib.crc32(" ".join(w[i:i + SHINGLE]).encode("utf-8")))
    return out


def artifact(conn, prefix):
    for r in conn.execute("SELECT src, file, jsonl FROM artifact"):
        if r[1].startswith(prefix):
            return {"src": r[0], "file": r[1], "jsonl": r[2]}
    return None


def stamp(path):
    st = os.stat(path)
    return {"path": os.path.basename(path), "size": st.st_size,
            "mtime": int(st.st_mtime)}


def stream_docs(jsonl, wanted):
    """Yield (dnum, full text) for each document in `wanted`, streaming.

    ONE SEQUENTIAL READ, NOT 70,000 SEEKS. provenance stores an offset per chunk
    and seeking to each one would be correct and far slower over half a gigabyte.
    Chunks of one document are normally contiguous, but that is not relied on:
    parts are held per dnum and released the moment a document has all `n` of
    them, so the order in the file does not matter and memory stays bounded."""
    parts = collections.defaultdict(dict)
    with open(jsonl, "rb") as fh:
        for line in fh:
            if not line.strip():
                continue
            row = json.loads(line)
            # THERE IS NO `dnum` KEY IN THE JSONL. A chunk record carries
            # id, src, path, title, i, n, kind, text - and the document number
            # is the MIDDLE FIELD OF THE ID, which is what provenance.sqlite3's
            # own schema comment says: `"10:1:0" -> 1`. The first version of
            # this function read row["dnum"] and died on the first line, after
            # the key list had already been printed in the session that wrote
            # it. `--selftest` now exercises this function against a synthetic
            # jsonl so the shape is asserted rather than remembered.
            try:
                dn = int(row["id"].split(":")[1])
            except (KeyError, IndexError, ValueError):
                raise RuntimeError(
                    "chunk record has no usable id: %r. Expected 'src:dnum:i'; "
                    "the jsonl and provenance.sqlite3 disagree about chunk ids, "
                    "which means one was rebuilt without the other."
                    % (row.get("id"),))
            if dn not in wanted:
                continue
            parts[dn][row["i"]] = row.get("text", "")
            if len(parts[dn]) >= row["n"]:
                d = parts.pop(dn)
                yield dn, "\n".join(d[i] for i in sorted(d))
    # A document whose chunk count disagrees with `n` would otherwise vanish
    # silently, which is the shape of defect this build keeps recording.
    for dn, d in parts.items():
        yield dn, "\n".join(d[i] for i in sorted(d))


def coverage(mine_text, theirs_text):
    """The number the rule turns on, as one function so the selftest and the
    measurement cannot disagree about what is being computed."""
    sa = shingles(mine_text)
    if sa is None:
        return 1.0 if " ".join(mine_text.split()) == " ".join(theirs_text.split()) else 0.0
    sb = shingles(theirs_text) or set()
    return sum(1 for h in sa if h in sb) / float(len(sa)) if sa else 1.0


def selftest():
    """Does coverage answer *does mdwiki add anything* on cases we can construct?

    NO INDEX, NO JSONL, NO MODEL - a 4,000-word synthetic article and six
    transformations of it, the same shape as `bin/ark.py selftest`. What it
    cannot prove is where the real corpus falls, which is what --report is for
    and which is said again beside the green line."""
    import random
    random.seed(4)
    V = ("patient dose mg kg treatment serum ferritin transfusion anemia iron "
         "chelation therapy the of in and with is are may cause blood cells "
         "hemoglobin level syndrome clinical diagnosis symptoms liver bone marrow"
         ).split()
    para = lambda n: " ".join(random.choice(V) for _ in range(n))
    secs = [para(400) for _ in range(10)]
    art = " ".join(secs)
    n = len(art.split())

    ok = fail = 0
    def check(name, got, lo, hi, why):
        nonlocal ok, fail
        good = lo <= got <= hi
        if good:
            ok += 1
            print("  ok    %-34s %.4f   %s" % (name, got, why))
        else:
            fail += 1
            print("  FAIL  %-34s %.4f   wanted %.3f..%.3f  %s"
                  % (name, got, lo, hi, why))

    r = secs[:]; random.shuffle(r)
    check("sections reordered", coverage(art, " ".join(r)), 0.98, 1.0,
          "a reorder is a copy; difflib scores this 0.06")
    w = art.split()
    for i in random.sample(range(n), int(n * 0.02)):
        w[i] = "REWORDED"
    check("2% of words changed", coverage(art, " ".join(w)), 0.80, 0.95,
          "ordinary drift keeps both copies")
    check("mdwiki has 2% unique", coverage(para(int(n * .02)) + " " + art, art),
          0.975, 0.985, "the edge of the threshold, stated")
    check("mdwiki has 4% unique", coverage(para(int(n * .04)) + " " + art, art),
          0.94, 0.975, "an infobox this size survives")
    check("wikipedia added a section", coverage(art, art + " " + para(600)),
          0.999, 1.0, "mdwiki adds nothing, so it goes")
    check("wikipedia kept only 20%", coverage(art, " ".join(art.split()[:n // 5])),
          0.0, 0.30, "the Fixed_drug_reaction case")
    check("identical", coverage(art, art), 1.0, 1.0, "")
    check("unrelated", coverage(art, para(50)), 0.0, 0.10, "")
    short = "a b c"
    check("too short to shingle, equal", coverage(short, short), 1.0, 1.0,
          "falls back to exact text")
    check("too short to shingle, differs", coverage(short, "a b d"), 0.0, 0.0, "")

    # --- the record shape, against a synthetic jsonl -------------------------
    # THIS IS HERE BECAUSE THE FIRST VERSION READ row["dnum"], WHICH DOES NOT
    # EXIST. It died on the first line of a half-gigabyte file after nine
    # minutes of setup. A fixture with the real key set - id, src, path, title,
    # i, n, kind, text - is three lines and catches it in a second.
    import json as _json, os as _os, tempfile
    fx = _os.path.join(tempfile.gettempdir(), "ark-mirror-selftest.jsonl")
    rows = [
        # one document split into three chunks, deliberately OUT OF ORDER, so
        # the reassembly is proved rather than assumed
        {"id": "1:7:2", "src": 1, "path": "Tafamidis", "title": "T", "i": 2,
         "n": 3, "kind": "text", "text": "third"},
        {"id": "1:7:0", "src": 1, "path": "Tafamidis", "title": "T", "i": 0,
         "n": 3, "kind": "text", "text": "first"},
        {"id": "1:9:0", "src": 1, "path": "Other", "title": "O", "i": 0,
         "n": 1, "kind": "text", "text": "unwanted"},
        {"id": "1:7:1", "src": 1, "path": "Tafamidis", "title": "T", "i": 1,
         "n": 3, "kind": "text", "text": "second"},
    ]
    with open(fx, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(_json.dumps(r) + "\n")
    got = dict(stream_docs(fx, {7}))
    check("stream_docs: dnum from id", 1.0 if sorted(got) == [7] else 0.0, 1.0, 1.0,
          "the middle field of 'src:dnum:i', not a dnum key")
    check("stream_docs: chunks in order",
          1.0 if got.get(7) == "first\nsecond\nthird" else 0.0, 1.0, 1.0,
          "reassembled by i, whatever order the file is in")
    check("stream_docs: skips unwanted",
          1.0 if 9 not in got else 0.0, 1.0, 1.0, "")
    try:
        with open(fx, "w", encoding="utf-8") as fh:
            fh.write(_json.dumps({"src": 1, "i": 0, "n": 1, "text": "x"}) + "\n")
        list(stream_docs(fx, {7}))
        check("stream_docs: bad record raises", 0.0, 1.0, 1.0,
              "a record with no id must not be skipped silently")
    except RuntimeError:
        check("stream_docs: bad record raises", 1.0, 1.0, 1.0,
              "a jsonl that lost its ids says so instead of returning nothing")
    _os.unlink(fx)

    print("\n%d/%d" % (ok, ok + fail))
    print("\nWHAT THIS DOES NOT PROVE: where the real pairs fall. --report "
          "showed that on\n2026-09-12 and is what set THRESHOLD to 0.99; the "
          "histogram is in this file's\nheader. Re-run --report after any "
          "re-index, because these are two different\nquestions and only one "
          "of them is answered here.")
    return fail == 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--threshold", type=float, default=THRESHOLD)
    ap.add_argument("--report", action="store_true",
                    help="measure and print the distribution, write nothing")
    ap.add_argument("--show", type=float, nargs=2, metavar=("LO", "HI"),
                    help="name up to 40 articles whose coverage is in [LO, HI)")
    ap.add_argument("--out", default=OUT)
    ap.add_argument("--selftest", action="store_true",
                    help="the coverage metric against synthetic articles; needs "
                         "no index, no jsonl and no model")
    a = ap.parse_args()

    if a.selftest:
        return 0 if selftest() else 1

    if not os.path.exists(PROV):
        sys.exit("missing %s" % PROV)
    conn = sqlite3.connect("file:%s?mode=ro" % PROV.replace("\\", "/"), uri=True)
    old, new = artifact(conn, OLD_PREFIX), artifact(conn, NEW_PREFIX)
    if not old or not new:
        sys.exit("need both %s* and %s* in the index" % (OLD_PREFIX, NEW_PREFIX))

    oj = os.path.join(CHUNKS, old["jsonl"])
    nj = os.path.join(CHUNKS, new["jsonl"])
    for p in (oj, nj):
        if not os.path.exists(p):
            sys.exit("missing %s" % p)

    print("old: %s\nnew: %s\nthreshold: %.3f coverage of mdwiki by wikipedia\n"
          % (old["file"], new["file"], a.threshold))

    newpath = {r[0]: r[1] for r in
               conn.execute("SELECT path, dnum FROM doc WHERE src=?", (new["src"],))}
    pairs = {}                                  # old dnum -> (path, new dnum)
    for dn, p in conn.execute("SELECT dnum, path FROM doc WHERE src=?", (old["src"],)):
        if p in newpath:
            pairs[dn] = (p, newpath[p])
    by_new = {v[1]: k for k, v in pairs.items()}
    print("%s shared article paths" % format(len(pairs), ","))

    t0 = time.time()
    sig = {}                                    # old dnum -> array('I') or exact text
    for dn, text in stream_docs(oj, set(pairs)):
        s = shingles(text)
        sig[dn] = array.array("I", sorted(s)) if s is not None else text
    print("  read mdwiki    %s docs in %.0fs" % (format(len(sig), ","), time.time() - t0))

    t0 = time.time()
    cover = {}
    for ndn, text in stream_docs(nj, set(by_new)):
        odn = by_new[ndn]
        mine = sig.get(odn)
        if mine is None:
            continue
        if isinstance(mine, str):               # too short to shingle
            cover[odn] = 1.0 if " ".join(mine.split()) == " ".join(text.split()) else 0.0
            continue
        # the same arithmetic `coverage()` does, against the packed array rather
        # than re-shingling mdwiki's text, which is no longer in memory
        theirs = shingles(text) or set()
        cover[odn] = (sum(1 for h in mine if h in theirs) / float(len(mine))
                      if len(mine) else 1.0)
    print("  read wikipedia %s docs in %.0fs\n" % (format(len(cover), ","), time.time() - t0))

    # ---------------------------------------------------------------- report
    bands = [(1.0, 1.01), (0.99, 1.0), (0.98, 0.99), (0.95, 0.98),
             (0.90, 0.95), (0.75, 0.90), (0.50, 0.75), (0.0, 0.50)]
    print("coverage of the mdwiki article by the wikipedia one:")
    for lo, hi in bands:
        n = sum(1 for c in cover.values() if lo <= c < hi)
        label = "exactly 1.000" if lo == 1.0 else "%.2f to %.2f" % (lo, hi)
        print("  %-14s %7s  %5.1f%%  %s"
              % (label, format(n, ","), 100.0 * n / max(len(cover), 1),
                 "#" * int(60.0 * n / max(len(cover), 1))))

    if a.show:
        lo, hi = a.show
        named = [(cover[d], pairs[d][0]) for d in cover if lo <= cover[d] < hi]
        named.sort()
        print("\n%s articles with coverage in [%.2f, %.2f), first 40:"
              % (format(len(named), ","), lo, hi))
        for c, p in named[:40]:
            print("   %.3f  %s" % (c, p[:70]))

    drop = sorted(d for d, c in cover.items() if c >= a.threshold)
    print("\nat threshold %.3f: %s of %s excluded (%.1f%%), %s kept"
          % (a.threshold, format(len(drop), ","), format(len(cover), ","),
             100.0 * len(drop) / max(len(cover), 1),
             format(len(cover) - len(drop), ",")))

    # ------------------------------------------------- the hand-read fixtures
    bad = 0
    print("\nthe twenty read by hand on 2026-09-11:")
    dropset = set(drop)
    for want_keep, names in ((True, MUST_KEEP), (False, MUST_DROP)):
        for name in names:
            dn = next((d for d, (p, _) in pairs.items() if p == name), None)
            if dn is None or dn not in cover:
                print("    --    %-30s not in this index" % name)
                continue
            kept = dn not in dropset
            ok = (kept == want_keep)
            if not ok:
                bad += 1
            print("    %s  %-30s coverage %.3f, %s"
                  % ("ok  " if ok else "FAIL", name, cover[dn],
                     "kept" if kept else "dropped"))
    if bad:
        print("\n  !! %d fixture(s) wrong at threshold %.3f. These are articles "
              "that\n     were READ, not guessed - raise the threshold rather "
              "than the fixtures." % (bad, a.threshold))

    if a.report:
        print("\n--report: nothing written.")
        return 1 if bad else 0
    if bad:
        print("\nrefusing to write a set that drops an article read and kept.")
        return 1

    # ------------------------------------------------------------- write it
    doc = {
        "generated": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "tool": "bin/index-mirror-set.py",
        "threshold": a.threshold,
        "metric": ("fraction of the mdwiki article's hashed word-%d-grams that "
                   "also appear in the wikipedia_en_medicine article at the same "
                   "path; asymmetric and order-insensitive by design" % SHINGLE),
        # THE STAMP IS THE WHOLE POINT OF THIS FILE BEING SAFE TO TRUST.
        # store.py refuses to use the set when any of these three has changed,
        # because the dnums are provenance's and the text is the jsonls'.
        "stamp": {"old_jsonl": stamp(oj), "new_jsonl": stamp(nj),
                  "provenance": stamp(PROV)},
        "old": old, "new": new,
        "shared": len(pairs), "compared": len(cover), "excluded": len(drop),
        "dnums": drop,
    }
    tmp = a.out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, separators=(",", ":"))
    os.replace(tmp, a.out)
    print("\nwrote %s (%s bytes)" % (a.out, format(os.path.getsize(a.out), ",")))
    print("NOW RUN: bash bin/rehash.sh 10-index   - this file is inside a "
          "checksummed shelf")
    return 0


if __name__ == "__main__":
    sys.exit(main())
