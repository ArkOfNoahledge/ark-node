#!/usr/bin/env python3
"""
measure-spatial-cues.py - what the node's spatial rule costs, on real questions.

    python bin/measure-spatial-cues.py              # the full measurement
    python bin/measure-spatial-cues.py --selftest   # the intents only, seconds
    python bin/measure-spatial-cues.py --sweep      # what each cue word buys
    python bin/measure-spatial-cues.py --passages  # the PASSAGE rule, on chunks
    python bin/measure-spatial-cues.py --passages --derive   # + the exclusion list

IT MEASURES THE SHIPPED CODE. Every rule comes from
`13-ark-node/ark-api/spatial.py` by import. A measurement script carrying its own
copy of the rule would agree with itself forever, and this build has already been
bitten by that twice: a verifier holding its own table of canonical pages, and a
selftest holding its own idea of a correct label.

THE NEGATIVE SET IS THE ARCHIVE'S OWN. Every question title in the seven Stack
Exchange sites the index holds, read out of `10-index/provenance.sqlite3`:
cooking, gardening, woodworking, outdoors, ham radio, homebrew, sustainability.
83,856 questions written by other people, none of them asking where a place is,
so ANY firing there is a false positive. It regenerates offline in seconds, which
is the property the first version of this measurement did not have: it was run
against a corpus that no longer exists and was never written into the archive, so
nobody on this node could have reproduced its numbers.

THE POSITIVE SET IS SMALL AND IT IS MINE. 41 questions written by hand. That is
a weakness, so it is listed in full below rather than summarised, and anyone can
disagree with it by reading it.
"""

import argparse
import collections
import random
import os
import sqlite3
import sys

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.environ.get("ARK_ROOT") or os.path.dirname(HERE)
API = os.path.join(ROOT, "13-ark-node", "ark-api")
if API not in sys.path:
    sys.path.insert(0, API)
import spatial                                                 # noqa: E402

PROV = os.path.join(ROOT, "10-index", "provenance.sqlite3")

POSITIVE = [
 # bare location questions, the shape `where` actually covers
 "where is Maracaibo", "where is Pereira", "where exactly is Ushuaia",
 "where in the world is Tromso", "where is Franklin Tennessee",
 "whereabouts is Merida",
 # the place is the subject and a property is asked for
 "what is the elevation of Quito", "what is the latitude of Anchorage",
 "how high is Lhasa", "is Reykjavik above sea level",
 "what hemisphere is Perth in", "which country is Cairo in",
 "what are the coordinates of Nairobi", "show me a map of Medellin",
 "what region is Franklin Tennessee in", "how far north is Tromso",
 "what is the terrain like around Ushuaia",
 # climate and growing, which is what the archive can actually answer
 "what climate is Pereira in", "what is the climate in Kathmandu",
 "what is the climate zone for Chicago", "will citrus grow in Medellin",
 "what is the growing season in Pereira", "what is the rainfall like in Lagos",
 "how cold does it get in Reykjavik", "what is the driest month in Cairo",
 "what crops grow at this altitude in Bogota",
 "is it too dry to keep bees in Maracaibo", "what hardiness zone is Nashville in",
 # distance and between
 "how far is Bogota from Cali", "how far is Nashville from Memphis",
 "how many kilometres from Tokyo to Osaka",
 "what is the terrain between Cali and Buenaventura",
 "how long would it take to walk from Cusco to Machu Picchu",
 # practical, on the ground
 "is there fresh water near Ushuaia", "what is the altitude at Cusco",
 "how far above sea level is Mexico City",
 "which side of the mountain is Merida on",
 "where can I find a river near Maracaibo", "what is the nearest town to Ushuaia",
 "how far inland is Lagos",
]

# KNOWN GAPS, AND THE REASON IS CHECKED RATHER THAN ASSERTED.
# A question the rule cannot answer because the GAZETTEER does not hold the
# place is a coverage limit, not a broken rule, and deleting it from the fixture
# above would hide a real limitation. So it sits here with the name it needs, and
# the selftest verifies that the name is genuinely absent. If the gazetteer ever
# gains it, this entry FAILS and has to move back up: a known-gap list that
# cannot notice its own gap closing is just a way of laundering a failure.
KNOWN_GAPS = [
 ("where can I find Lake Titicaca", "Titicaca",
  "the basemap's places layer carries localities, regions and countries. "
  "Lakes, mountains and ruins are drawn on the map but are not labelled in "
  "that layer, so they have no searchable name here."),
]

# Questions that must NOT come back as `spatial`, chosen to be the shapes this
# archive is actually for. A router that answers these with a map is worse than
# one that answers nothing.
NEGATIVE_FIXTURE = [
 "how do I make flood water safe to drink",
 "what dose of amoxicillin for a 20 kg child",
 "how do I sharpen a chisel",
 "what is the compressive strength of concrete after 7 days",
 "how do I treat a snake bite with no antivenom",
 "what temperature kills botulism spores",
 "how do I splice a rope",
 "why is my sourdough not rising",
]


# THE SAME QUESTIONS IN SPANISH, because spec section 1 puts both languages in
# v1 and the cue table was English-only until 2026-09-07. Found by asking the
# running node rather than by reading the code: *cual es el clima en Kathmandu*
# came back as `place`, not `spatial`. The gazetteer had found Kathmandu; nothing
# had recognised the question.
POSITIVE_ES = [
 "donde queda Maracaibo", "donde esta Pereira", "donde queda exactamente Ushuaia",
 "cual es la elevacion de Quito", "cual es la latitud de Anchorage",
 "que tan alto esta Lhasa", "esta Reykjavik sobre el nivel del mar",
 "en que hemisferio esta Perth", "en que pais esta Cairo",
 "cuales son las coordenadas de Nairobi", "muestrame un mapa de Medellin",
 "cual es el clima en Kathmandu", "que clima tiene Pereira",
 "cual es la temporada de siembra en Pereira", "como son las lluvias en Lagos",
 "que tan frio se pone en Reykjavik", "que altitud tiene Bogota",
 "a que distancia esta Bogota de Cali", "cuantos kilometros hay de Tokyo a Osaka",
 "cual es el terreno alrededor de Ushuaia",
 "que tan lejos esta Nashville de Memphis",
 "cual es el pueblo mas cercano a Ushuaia",
 "en que zona de rusticidad esta Nashville",
 "cual es la ubicacion de Cusco",
]

# Spanish text the archive holds that is NOT a spatial question. WEAKER EVIDENCE
# THAN THE ENGLISH SET AND IT IS WORTH SAYING SO: these are encyclopedia article
# titles and repair-guide titles, not questions, so a cue firing here does not
# mean quite what a cue firing on a question means. It is what the archive has.
def titles(lang="en"):
    """Every Stack Exchange question title in the index."""
    if not os.path.exists(PROV):
        raise SystemExit("10-index/provenance.sqlite3 is missing; this "
                         "measurement reads the negative set out of it.")
    db = sqlite3.connect("file:%s?mode=ro" % PROV, uri=True)
    out = []
    if lang == "en":
        for src, f in db.execute("SELECT src, file FROM artifact "
                                 "WHERE file LIKE '%.stackexchange.com%'"):
            site = f.split(".stackexchange")[0]
            for (t,) in db.execute("SELECT title FROM doc WHERE src=? AND "
                                   "path LIKE 'questions/%/%'", (src,)):
                if t:
                    t = t.rsplit(" - ", 1)[0].strip()
                    if t:
                        out.append((site, t))
    else:
        for src, f in db.execute("SELECT src, file FROM artifact WHERE lang='es'"):
            site = f.split("_")[0][:12]
            for (t,) in db.execute("SELECT title FROM doc WHERE src=? AND "
                                   "title IS NOT NULL", (src,)):
                t = t.strip()
                if t:
                    out.append((site, t))
    db.close()
    return out


CHUNKS = os.path.join(ROOT, "10-index", "chunks")
PASSAGE_SEED = 20260907
PASSAGE_K = 200


def sample_chunks(k=PASSAGE_K, seed=PASSAGE_SEED, big=False):
    """k chunks from every artifact, UNIFORM BY LINE and reproducible.

    NOT BY BYTE. Seeking to a random offset and taking the next line is cheaper
    and wrong for this question: it favours long lines, and a long chunk is more
    likely to contain a place name, so the sampling method itself would inflate
    the rate it is being used to measure. Reservoir sampling reads each file
    once, keeps k lines with equal probability and needs no line count.

    The seed is fixed, so the same 5,800 chunks come back on every machine and
    the numbers below are checkable rather than merely repeatable-in-spirit."""
    if not os.path.isdir(CHUNKS):
        raise SystemExit("10-index/chunks is missing; this measurement reads "
                         "the passage set out of it.")
    import io
    import json
    rnd = random.Random(seed)
    for name in sorted(os.listdir(CHUNKS)):
        if not name.endswith(".jsonl"):
            continue
        keep, n = [], 0
        with io.open(os.path.join(CHUNKS, name), encoding="utf-8",
                     errors="replace") as f:
            for line in f:
                n += 1
                if len(keep) < k:
                    keep.append(line)
                else:
                    j = rnd.randrange(n)
                    if j < k:
                        keep[j] = line
        rows = []
        for line in keep:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            rows.append(d)
        yield name, n, rows


def passages(derive=False):
    """What the passage rule costs, on real chunks.

    THE CASCADE IS PRINTED, not only the final number, because every gate here
    was chosen by looking at what the previous one left behind, and a reader who
    disagrees with one of them should be able to see what it bought."""
    if not spatial.available():
        raise SystemExit("no gazetteer: %s" % spatial.status()["error"])

    rows, pop = [], 0
    for name, n, chunk_rows in sample_chunks():
        pop += n
        for d in chunk_rows:
            rows.append((name, d.get("title") or "", d.get("text") or ""))
    print("passage set: %s chunks sampled from %d artifacts, population %s"
          % ("{:,}".format(len(rows)), len(set(r[0] for r in rows)),
             "{:,}".format(pop)))

    ment = [spatial.mentions(t) for _, _, t in rows]
    N = float(len(rows))

    def rate(g):
        keep = [[m for m in ms if g(m)] for ms in ment]
        return (100.0 * sum(1 for ms in keep if ms) / N,
                sum(len(ms) for ms in keep),
                collections.Counter(m["text"] for ms in keep for m in ms))

    cascade = [
        ("mentions() as shipped", lambda m: True),
        ("+ is_place_reference()", spatial.is_place_reference),
        ("+ the capital was not free",
         lambda m: spatial.is_place_reference(m) and not m["clause"]),
        ("+ not a 3-capital acronym",
         lambda m: spatial.is_place_reference(m) and not m["clause"]
         and not (m["text"].isupper() and len(m["text"]) >= 3)),
        ("+ poprank >= %d" % spatial.PASSAGE_POPRANK,
         lambda m: spatial.is_place_reference(m) and not m["clause"]
         and not (m["text"].isupper() and len(m["text"]) >= 3)
         and (m["poprank"] or 0) >= spatial.PASSAGE_POPRANK),
        ("in_passage()  <- what ships", spatial.in_passage),
    ]
    print()
    for label, g in cascade:
        pct, n, names = rate(g)
        print("  %-32s %5.1f%% of chunks  %6s mentions" %
              (label, pct, "{:,}".format(n)))
        print("        top: " +
              ", ".join("%s x%d" % (t, c) for t, c in names.most_common(10)))

    if not derive:
        print("\n  --derive re-counts the AMBIGUOUS list from the corpus.")
        return 0

    # THE EXCLUSION LIST, RE-DERIVED. Every single-word name the poprank gate
    # lets through, counted by how the corpus writes it. A place name is
    # capitalised nearly always; a common noun that is also a place is not.
    import re as _re
    word = _re.compile(r"[^\W\d_]+", _re.UNICODE)
    cand = set()
    for ms in ment:
        for m in ms:
            if (spatial.is_place_reference(m) and not m["clause"]
                    and (m["poprank"] or 0) >= spatial.PASSAGE_POPRANK
                    and " " not in m["text"]):
                cand.add(m["text"].lower())
    lo, up = collections.Counter(), collections.Counter()
    for name, n, chunk_rows in sample_chunks(k=600, seed=PASSAGE_SEED):
        for d in chunk_rows:
            for w in word.findall(d.get("text") or ""):
                k = w.lower()
                if k not in cand:
                    continue
                if w[:1].islower():
                    lo[k] += 1
                elif not w.isupper():
                    up[k] += 1
    out = []
    for k in cand:
        tot = lo[k] + up[k]
        if tot >= 20:
            out.append((lo[k] / float(tot), k, lo[k], up[k]))
    out.sort(reverse=True)
    print("\n  AMBIGUOUS, re-derived (share >= %.2f of %d candidates):"
          % (spatial.AMBIGUOUS_MIN_LOWER, len(cand)))
    for share, k, l, u in out:
        mark = "  <- in the shipped list" if k in spatial.AMBIGUOUS else ""
        if share >= spatial.AMBIGUOUS_MIN_LOWER:
            print('    "%s": %.3f,   # lower %d / upper %d%s' % (k, share, l, u, mark))
    missing = [k for k in spatial.AMBIGUOUS
               if k not in [r[1] for r in out if r[0] >= spatial.AMBIGUOUS_MIN_LOWER]]
    if missing:
        print("    NOT re-derived from this sample: %s" % ", ".join(sorted(missing)))
    return 0

def selftest():
    """The intents, on the fixture. No corpus, so it runs in seconds."""
    if not spatial.available():
        print("no gazetteer: %s" % spatial.status()["error"])
        return 1
    fails = 0
    print("questions that must resolve to a place and read as spatial:")
    for q in POSITIVE:
        c = spatial.classify(q)
        ok = c["intent"] in ("spatial", "place")
        if not ok:
            fails += 1
        print("  %-56s %-18s %s" % (q[:56], c["intent"] or "none",
                                    "ok" if ok else "MISSED"))
    print("\nthe same questions in Spanish:")
    for q in POSITIVE_ES:
        c = spatial.classify(q)
        ok = c["intent"] in ("spatial", "place")
        strong = c["intent"] == "spatial"
        if not ok:
            fails += 1
        print("  %-56s %-18s %s" % (q[:56], c["intent"] or "none",
                                    "ok" if strong else
                                    "place only, no cue fired" if ok else "MISSED"))
    weak = [q for q in POSITIVE_ES if spatial.classify(q)["intent"] != "spatial"]
    print("  %d of %d reach 'spatial'; %d resolve the place but fire no cue"
          % (len(POSITIVE_ES) - len(weak), len(POSITIVE_ES), len(weak)))

    print("\nknown gaps, where the gazetteer holds no such place:")
    for q, missing, why in KNOWN_GAPS:
        c = spatial.classify(q)
        absent = not spatial.resolve(missing)
        ok = absent and c["intent"] != "spatial"
        if not ok:
            fails += 1
        print("  %-56s %s" % (q[:56],
              "ok, %r is absent" % missing if ok
              else "THIS GAP HAS CLOSED: %r resolves now, move it into POSITIVE" % missing))
        print("        %s" % why)

    print("\nquestions the archive is for, which must NOT read as spatial:")
    for q in NEGATIVE_FIXTURE:
        c = spatial.classify(q)
        ok = c["intent"] != "spatial"
        if not ok:
            fails += 1
        print("  %-56s %-18s %s" % (q[:56], c["intent"] or "none",
                                    "ok" if ok else "FIRED, and should not have"))
    print("\ntwo places in one question should measure the distance:")
    c = spatial.classify("how far is Bogota from Cali")
    d = c["distance_km"]
    # Bogota to Cali is about 300 km by air. A rule that returns None here, or a
    # number off by a factor, is a broken rule and not a rounding difference.
    ok = d is not None and 250 <= d <= 350
    fails += 0 if ok else 1
    print("  Bogota to Cali = %s km   %s" % (d, "ok" if ok else "WRONG"))
    print("\n%s" % ("selftest: PASS" if not fails else "selftest: %d FAILURES" % fails))
    return 1 if fails else 0


def measure(sweep=False):
    if not spatial.available():
        print("no gazetteer: %s" % spatial.status()["error"])
        return 1
    n = spatial.preload()
    negs = titles("en")
    sites = sorted(set(s for s, t in negs))
    print("negative set: %s questions from %d sites (%s)"
          % ("{:,}".format(len(negs)), len(sites), ", ".join(sites)))
    print("positive set: %d written spatial questions" % len(POSITIVE))
    print("gazetteer keys in memory: %s\n" % "{:,}".format(n))

    def rates(fn, label, show=0):
        fired = [(s, t) for s, t in negs if fn(t)]
        rec = [q for q in POSITIVE if fn(q)]
        print("  %-40s fires on %5d / %s = %5.3f%%   recall %2d/%d = %3.0f%%"
              % (label, len(fired), "{:,}".format(len(negs)),
                 100.0 * len(fired) / len(negs), len(rec), len(POSITIVE),
                 100.0 * len(rec) / len(POSITIVE)))
        for s, t in fired[:show]:
            print("      [%s] %s" % (s, t[:88]))
        return fired

    print("the two signals, separately")
    rates(lambda t: spatial.cue(t) is not None, "a cue phrase")
    rates(lambda t: bool(spatial.mentions(t)), "a capitalised place name")

    print("\nthe rule that ships: both")
    fired = rates(lambda t: spatial.cue(t) is not None and bool(spatial.mentions(t)),
                  "cue AND place  ->  intent 'spatial'")
    print("\n  every one of the %d firings, because the whole design turned on "
          "reading them:" % len(fired))
    for s, t in fired:
        print("      [%s] %s" % (s, t[:92]))

    print("\nthe weaker signal, which only attaches a place card")
    print("  THIS IS THE HIGH-VOLUME PATH and it needs the stricter threshold.")
    rates(lambda t: not spatial.cue(t) and bool(spatial.mentions(t)),
          "any capitalised place, no cue  (rejected)")
    place = rates(lambda t: not spatial.cue(t)
                  and any(spatial.is_place_reference(m) for m in spatial.mentions(t)),
                  "anchored, not a compound  ->  intent 'place'")
    print("\n  a deterministic sample of 20 of those %d, to be READ rather than\n"
          "  summarised. The loose rule was about six in ten wrong here, which is\n"
          "  why the threshold exists:" % len(place))
    rng = random.Random(20260907)
    for s_, t in rng.sample(place, min(20, len(place))):
        print("      [%s] %s" % (s_, t[:88]))

    print("\nthe same rule on SPANISH text from the archive")
    es = titles("es")
    print("  negative set: %s article and guide titles from the two Spanish "
          "artifacts.\n  Titles, not questions, so this is weaker evidence than "
          "the set above." % "{:,}".format(len(es)))
    fired_es = [(a, t) for a, t in es
                if spatial.cue(t) is not None and spatial.mentions(t)]
    rec_es = [q for q in POSITIVE_ES
              if spatial.cue(q) is not None and spatial.mentions(q)]
    print("  cue AND place                            fires on %5d / %s = %5.3f%%"
          "   recall %2d/%d = %3.0f%%"
          % (len(fired_es), "{:,}".format(len(es)),
             100.0 * len(fired_es) / max(1, len(es)),
             len(rec_es), len(POSITIVE_ES),
             100.0 * len(rec_es) / len(POSITIVE_ES)))
    for a, t in fired_es[:20]:
        print("      [%s] %s" % (a, t[:88]))
    if len(fired_es) > 20:
        print("      ... %d more" % (len(fired_es) - 20))

    print("\nper cue, on the negative set, for the cue alone")
    for c in spatial.CUES:
        k = sum(1 for s, t in negs if spatial.cue(t) == c)
        print("   %-16s %5d (%.3f%%)" % (c, k, 100.0 * k / len(negs)))

    if sweep:
        print("\nwhat each cue word buys, inside the conjunction")
        base = list(spatial.CUES)
        place = {t: bool(spatial.mentions(t)) for s, t in negs}
        placep = {q: bool(spatial.mentions(q)) for q in POSITIVE}

        def score(cues):
            saved, spatial.CUES = spatial.CUES, tuple(cues)
            try:
                fp = sum(1 for s, t in negs if spatial.cue(t) and place[t])
                rc = sum(1 for q in POSITIVE if spatial.cue(q) and placep[q])
            finally:
                spatial.CUES = saved
            return fp, rc
        fp0, rc0 = score(base)
        print("   full list: %d firings, %d/%d recall" % (fp0, rc0, len(POSITIVE)))
        for c in base:
            fp, rc = score([x for x in base if x != c])
            print("   without %-16s firings %+4d   recall %+3d"
                  % (c, fp - fp0, rc - rc0))
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--sweep", action="store_true")
    ap.add_argument("--passages", action="store_true",
                    help="the passage rule, on real chunks from 10-index")
    ap.add_argument("--derive", action="store_true",
                    help="with --passages, re-count the AMBIGUOUS list")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if a.passages:
        return passages(a.derive)
    return measure(a.sweep)


if __name__ == "__main__":
    sys.exit(main())
