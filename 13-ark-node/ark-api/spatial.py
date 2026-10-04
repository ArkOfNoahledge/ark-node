#!/usr/bin/env python3
"""
spatial.py - does this question name a place, and is it asking about that place?

    import spatial
    spatial.classify("what is the climate in Kathmandu")

WHY THIS IS NOT A MODEL. Deciding intent is exactly the kind of thing a language
model is good at, and it is exactly the kind of thing NODE-ARCHITECTURE section
3.4 forbids on this path: retrieval has to work with no model running, so
anything that gates retrieval must too. This is a table of cue words, a
capitalisation rule and a lookup in `10-index/gazetteer.sqlite3`. It runs in
about a millisecond with nothing loaded.

WHY THE RULES ARE THESE RULES. Measured, on 2026-09-07, against 83,856 question
titles from the seven Stack Exchange sites the index holds. None of them asks
where a place is, so anything that fires there is a false positive. The shipping
rule fires on 26 of the 83,856, and reading all 26 is what set the shape below:
most were *grow a mango plant in the Phoenix climate*, where the place's climate
is help rather than noise. `bin/measure-spatial-cues.py` imports THIS FILE and
measures it, so the numbers describe the code that runs rather than a copy of it.

THREE ANSWERS, NOT TWO. See BUILD-LOG.md 2026-09-07.

    a cue AND a place   ->  answer the spatial question
    a place, no cue     ->  answer normally, and attach the place beside it
    a cue, no place     ->  say there is nothing to look up, and do not guess
"""

import os
import re
import sqlite3
import sys

# NO .pyc BESIDE THE SOURCE, for the reason in serve.py: bin/ is checksummed per
# R7, and this module imports a tool out of it. An import that leaves a
# __pycache__ there turns the archive's integrity signal into noise.
sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import store                                                   # noqa: E402

GAZ_DB = os.path.join(store.INDEX, "gazetteer.sqlite3")

# ---------------------------------------------------------------------------
# the cue table, and what each entry cost
# ---------------------------------------------------------------------------
# Firing rates are on the 83,856 real questions, for the cue ALONE. The rule
# that ships is the conjunction with a place name, which is what makes the
# noisier entries affordable.
CUES_EN = (
    "terrain",           # 0.02%
    "elevation",         # 0.03%
    "region",            # 0.03%
    "location",          # 0.05%
    "how far",           # 0.09%
    "altitude",          # 0.11%
    "climate",           # 0.13%
    "latitude", "longitude", "hemisphere", "in my area",        # 0.00%
    "coordinates", "map of", "sea level",                       # 0.01%
    "which country", "what country",
    # added one at a time, kept only where they bought recall at no cost
    "how high", "nearest", "which side", "how cold", "growing season",
    "hardiness zone", "rainfall", "kilometres", "whereabouts",
)

# SPANISH, BECAUSE HALF THIS NODE IS SPANISH.
# Spec section 1 puts English and Spanish in v1, the operator manual exists in
# both, and until 2026-09-07 the cue table was English only. Measured
# consequence, found by asking: *cual es el clima en Kathmandu* came back as
# `place` rather than `spatial` - the gazetteer found Kathmandu, because it holds
# 42 languages, and then nothing recognised that the question was about it. A
# feature that works in one of the node's two languages is half a feature, and
# the failure was silent because the place card still attached.
#
# Written WITHOUT ACCENTS on purpose: cues are matched against the same
# normaliser the gazetteer uses, which folds accents, so `clima` and `climá` and
# `CLIMA` are one key and an operator with an English keyboard is not locked out.
CUES_ES = (
    "clima", "terreno", "altitud", "elevacion", "ubicacion",
    "latitud", "longitud", "hemisferio", "coordenadas", "mapa de",
    "nivel del mar", "que pais", "cual pais",
    "que tan lejos", "a que distancia", "que tan alto", "que altura",
    "que tan frio", "kilometros", "mas cercano", "mas cercana",
    "que lado", "temporada de siembra", "temporada de cultivo",
    "zona de rusticidad", "lluvias", "en mi zona",
)

CUES = CUES_EN + CUES_ES

# DROPPED, AND WORTH KNOWING WHY: `distance`, `north of`, `south of`,
# `miles from` and `how long would it take` each bought no recall on the
# fixture and cost firings. `where` on its own fires 542 times in 83,856,
# five to thirty times noisier than anything above, because in these corpora
# it means *where can I buy*: "Where can I buy kosher salt in London?" is not
# a question about London.
#
# THE COPULA IS DIFFERENT. "where is", "where are", "whereabouts is": 7
# firings in 83,856, all seven genuine location questions. The earlier
# decision to drop `where` was right about the word and wrong about the phrase.
# THREE WORDS BETWEEN, NOT TWO. `where in the world is Tromso` has three, and
# the fixture caught it. Widening costs exactly one more firing across 83,856
# real questions - `Where in New Zealand is wild camping permitted?`, which is a
# location question - and a fourth word buys nothing at all. Measured both ways
# rather than picked.
# ONE \s* WHERE THERE WERE TWO (2026-10-04). `where\s*(?:abouts)?\s*` put two
# adjacent \s* side by side whenever `abouts` was absent, which a long run of
# spaces can make backtrack polynomially (CodeQL, first scan). Moving the first
# one inside the optional group matches exactly the same strings.
WHERE_IS = re.compile(
    r"^\s*where(?:\s*abouts)?\s*(?:\w+\s+){0,3}(?:is|are|was|were)\b")

# The same shape in Spanish. `donde queda X` and `donde esta X` are the copula;
# `donde puedo comprar` is the shopping question the English measurement caught,
# and it is excluded by requiring the copula verb.
DONDE_ESTA = re.compile(
    r"^\s*(?:a\s+)?donde\s*(?:\w+\s+){0,3}"
    r"(?:queda|quedan|esta|estan|se encuentra|se encuentran)\b")

_WORD = re.compile(r"[0-9A-Za-zÀ-ɏ']+")
_CLAUSE_END = frozenset(".?!:;")

# WORDS THAT ARE TOWNS SOMEWHERE AND NEVER MEAN ONE HERE.
# Measured on the 83,856: `To`, `On`, `Can`, `Most`, `Ski` and `Vine` each
# matched a real place because a question had capitalised them, and every one of
# those firings was wrong. There is a hamlet for nearly every short English
# word; a question is not referring to it.
FUNCTION_WORDS = frozenset("""
a an the and or but if then than so as at by for from in into of off on onto out
over to up with within without is are was were be been being am do does did done
have has had having can could may might must shall should will would this that
these those it its i we you he she they them us my your our their his her no not
none all any both each few many more most much several some such only own same
very just also back next last here there when where which who whom whose what
how why does isnt dont
""".split())

# A LOCATIVE PREPOSITION, for the titles where capitalisation says nothing.
# `to` is deliberately absent: it is the preposition of *how to*, not of place.
LOCATIVE = frozenset(("in", "near", "at", "from", "around", "outside", "of",
                      "for", "across", "beyond", "between"))

# The narrower set `is_place_reference` uses. `of` and `for` are out of this one
# because they were measured and they cost more than they bought: `Grains of
# Paradise`, `Use Stand Mixer for Scone`.
LOCATIVE_ANCHOR = frozenset(("in", "near", "at", "from", "around", "outside"))

# ---------------------------------------------------------------------------
# the gazetteer, loaded once, degrading rather than raising
# ---------------------------------------------------------------------------

_gz = None            # bin/index-gazetteer.py, imported by path
_db = None
_error = None
_names = None         # an optional in-memory set, for bulk measurement only


def _open():
    """Load the gazetteer module and open the index. Never raises: a node with
    no gazetteer must still answer everything else, so the failure is a string
    the caller can show rather than an exception that takes the route out."""
    global _gz, _db, _error
    if _db is not None or _error is not None:
        return
    try:
        # ONE DEFINITION OF `normalise`, NOT TWO. The `norm` column is that
        # function's output, so a copy of it here would drift from the column
        # it is supposed to match and every mismatch would look like a missing
        # place. Same argument store._load_bin already makes for the BM25
        # stoplist, and the same mechanism.
        _gz = store._load_bin("index-gazetteer.py")
        if not os.path.exists(GAZ_DB):
            _error = ("10-index/gazetteer.sqlite3 is not there, so no question "
                      "can be resolved to a place. Build it with the tile "
                      "server running:  python bin/index-gazetteer.py --build")
            return
        # WRAPPED, NOT BARE. Every request thread reads this one, and from
        # 2026-09-07 the passage matcher asks `_is_place` a few hundred times per
        # passage. A bare shared connection hands both threads the same cached
        # statement; store.Shared says what that does and how it was found.
        db = store.Shared(sqlite3.connect(
            "file:%s?mode=ro" % GAZ_DB.replace("?", "%3f"),
            uri=True, check_same_thread=False))
        stale = _gz.norm_check(db)
        if stale:
            _error = "the gazetteer is stale: " + stale
            db.close()
            return
        _db = db
    except Exception as e:                                   # noqa: BLE001
        _error = "the gazetteer could not be opened: %s: %s" % (type(e).__name__, e)


def available():
    _open()
    return _db is not None


def status():
    """For /api/health. Says which of the two failure states it is in, because
    'not loaded' and 'failed to load' are different facts (DECISIONS 2026-09-06)."""
    _open()
    out = {"gazetteer": _db is not None, "error": _error, "path": GAZ_DB}
    if _db is not None:
        out["names"] = _db.execute("SELECT COUNT(*) FROM place").fetchone()[0]
        out["built"] = dict(_db.execute("SELECT k, v FROM meta")).get("built")
    return out


def preload():
    """Pull every place key and its population rank into memory. For
    `bin/measure-spatial-cues.py`, which asks these questions 83,856 times and
    would otherwise spend most of its run in SQLite. Same keys, same source, one
    code path for the matching rule."""
    global _names
    if not available():
        return 0
    _names = dict(_db.execute(
        "SELECT norm, MAX(COALESCE(poprank, 0)) FROM place GROUP BY norm"))
    return len(_names)


def _is_place(key):
    if _names is not None:
        return key in _names
    return _db.execute("SELECT 1 FROM place WHERE norm = ? LIMIT 1",
                       (key,)).fetchone() is not None


def _rank(key):
    """The highest population rank any place under this name carries. 0 when the
    tiles record none, which is most hamlets."""
    if _names is not None:
        return _names.get(key, 0)
    row = _db.execute("SELECT MAX(COALESCE(poprank, 0)) FROM place WHERE norm = ?",
                      (key,)).fetchone()
    return (row and row[0]) or 0


# ---------------------------------------------------------------------------
# matching
# ---------------------------------------------------------------------------

def _fold(text):
    """Casefold, strip accents, flatten. THE SAME FUNCTION THE `norm` COLUMN WAS
    BUILT WITH, taken from the gazetteer module rather than written again here,
    so a cue and a place name are folded identically. Falls back to a plain
    lowercase only when the gazetteer could not be loaded, at which point
    nothing can be resolved anyway."""
    if _gz is not None:
        return _gz.normalise(text)
    return " ".join(text.lower().split())


def cue(question):
    """The first cue phrase in the question, or None. English and Spanish."""
    _open()
    words = _WORD.findall(question)
    t = _fold(" ".join(words))
    padded = " " + t + " "
    for c in CUES:
        if (" " + c + " ") in padded:
            return c
    if WHERE_IS.match(t):
        return "where is"
    if DONDE_ESTA.match(t):
        return "donde queda"
    return None


def _words(text):
    """Words with a flag for the ones that start a clause. A word after `.`,
    `?`, `!`, `:` or `;` is capitalised by convention and its capital says
    nothing, exactly like the first word of the title. Measured: without this,
    `On which side...`, `To shave or not shave?` and `Can these plants...` all
    matched hamlets called On, To and Can.

    SPANS ARE CARRIED because the caller needs to know WHERE each word is, not
    only what it is. `mentions` used to locate a match with `text.find(word)`,
    which returns the FIRST occurrence - so in "Ottawa, Canada is cold. We drove
    through Ottawa without stopping" the second Ottawa reported the first one's
    trailing comma, and `is_place_reference` reads exactly that flag. In a
    ten-word question a name repeats almost never; in a thousand-word passage it
    is systematic. Found 2026-09-07 while measuring passages."""
    out = []
    prev_end = 0
    for i, m in enumerate(_WORD.finditer(text)):
        gap = text[prev_end:m.start()]
        clause = i == 0 or any(c in _CLAUSE_END for c in gap)
        out.append((m.group(0), clause, m.start(), m.end()))
        prev_end = m.end()
    return out


def is_title_case(words, floor=4, share=0.6):
    """Is this text Title Cased, so that capitalisation carries no information?

    Measured: *Which of These Bonsais Are The Most Appropriate For My Climate?*
    and *Beginner Ski Touring - Avalanche Terrain* both matched places called
    Most and Ski. In a title where nearly every word is capitalised, a capital
    is not evidence, and the rule has to fall back on something else."""
    cand = [t[0] for t in words if len(t[0]) > 2]
    if len(cand) < floor:
        return False
    caps = sum(1 for w in cand if w[:1].isupper())
    return caps / float(len(cand)) > share


def mentions(question, max_words=3, min_chars=3):
    """Every place the question names, left to right, longest match first.

    CAPITALISED, AND NOT WHERE A CAPITAL IS FREE. Measured: a gazetteer name
    anywhere matches 12.43% of real questions; requiring a locative preposition
    in front of it drops that to 1.56% but costs more than half the recall,
    because *where is Maracaibo* and *how high is Lhasa* have no preposition.
    Capitalisation gives the same recall far more cheaply, and it is what
    separates Turkey from turkey, Reading from reading, and Mobile, Bath, Nice
    and Sandwich from the words.

    That rule holds for QUESTIONS and not for passages, which is why an earlier
    measurement rejected it: passages are not written in sentence case. And it
    stops holding inside a Title Cased question, where it falls back to the
    preposition.

    Four exclusions, each of which was a wrong firing before it was a rule:
    a clause's first word, an English function word, an acronym of four or more
    capitals (`USDA hardiness zone` is not a town), and a name under three
    characters."""
    if not available():
        return []
    words = _words(question)
    raw = [t[0] for t in words]
    low = [w.lower() for w in raw]
    titled = is_title_case(words)
    out = []
    i = 0
    while i < len(raw):
        w = raw[i]
        free_capital = words[i][1] or titled
        if not free_capital and not w[:1].isupper():
            i += 1
            continue
        if low[i] in FUNCTION_WORDS:
            i += 1
            continue
        if w.isupper() and len(w) >= 4:
            i += 1
            continue
        # In a Title Cased question the capital proves nothing, so something
        # else has to point at the place: the preposition in front of it.
        if titled and not (i > 0 and low[i - 1] in LOCATIVE):
            i += 1
            continue
        hit = None
        for k in range(min(max_words, len(raw) - i), 0, -1):
            part = raw[i:i + k]
            if not all(x[:1].isupper() or x.lower() in FUNCTION_WORDS for x in part):
                continue
            key = _gz.normalise(" ".join(low[i:i + k]))
            if len(key) >= min_chars and _is_place(key):
                hit = (k, " ".join(part), key)
                break
        if hit:
            k, text, key = hit
            nxt = raw[i + k] if i + k < len(raw) else ""
            # The commas matter and are not in `raw`, so they are read off the
            # original string around THIS match - by its own span, never by
            # searching for the word, which finds an earlier one. See `_words`.
            at = words[i][2]
            before = question[:at]
            after = question[words[i + k - 1][3]:]
            out.append({
                "text": text,
                "key": key,
                # WHETHER THE CAPITAL WAS FREE. `_words` already computes this
                # and until 2026-09-07 nothing read it. Reported rather than
                # acted on here: the question matcher's measured rates were
                # taken with it unused, so honouring it would change a shipped
                # rule, and the passage measurement is what should decide that.
                "clause": words[i][1],
                "prev": low[i - 1] if i > 0 else "",
                # THE WORD IN FRONT, AS WRITTEN. `prev` is lowercased, which
                # loses exactly what a compound needs: `George Washington` and
                # `in Washington` have the same `prev` in lower case and are not
                # the same thing.
                "prev_capital": bool(i > 0 and raw[i - 1][:1].isupper()),
                "prev_is_place": bool(i > 0 and _is_place(_gz.normalise(raw[i - 1]))),
                "next": nxt,
                "next_is_place": bool(nxt) and _is_place(_gz.normalise(nxt)),
                "comma_before": before.rstrip().endswith((",", "-")),
                "comma_after": after.lstrip().startswith(","),
                "poprank": _rank(key),
            })
            i += k
        else:
            i += 1
    return out


def is_place_reference(m):
    """Is this mention a reference to the PLACE, or a proper noun that merely
    contains a place name?

    THIS IS A SECOND, STRICTER THRESHOLD AND IT EXISTS BECAUSE THE LOOSE ONE WAS
    MEASURED. `mentions()` alone fires on 3.48% of 83,856 real questions, and
    reading a random sample of 40 of those put roughly six in ten in the wrong:
    `Turkey Stock`, `Norway Spruce`, `Star Jasmine`, `Split Pea Soup`,
    `Philodendron Hope`, `Sore left knee`, `Beach lifeguard`. The most-matched
    names across the whole corpus were `Baofeng`, `Green`, `Mango`, `Turkey`,
    `Canning`, `Sauce`, `Orange`, `Tea` - common nouns and brands that are also
    hamlets somewhere.

    That rate is fine for the `spatial` intent, where a cue word carries the
    evidence and the conjunction fires on 0.041%. It is NOT fine for the `place`
    intent, whose whole action is to attach an unsolicited card beside an answer
    about something else: **a card that is wrong six times in ten teaches the
    operator to ignore the card.**

    Two conditions, both measured:

    ANCHORED. A locative preposition in front (`in`, `near`, `at`, `from`,
    `around`, `outside`), or a comma or dash on either side. The comma is what
    catches how people actually write locations - `Buffalo, NY`, `Riga, Latvia`,
    `plants - Sydney, Australia` - and requiring only the preposition lost all
    of those. `to`, `of` and `for` are excluded: they gave `Grains of Paradise`
    and `Use Stand Mixer for Scone`.

    NOT A COMPOUND. A capitalised word immediately after the match, which is not
    itself a place, makes this a compound proper noun: `Turkey Stock`, `Norfolk
    Island Pine`. A comma before that word reverses it, because `Ottawa, Canada`
    is two places and not a compound - an earlier version without that exception
    threw away every `City, Country` in the corpus.

    Measured together: 655 firings, 0.78%, and 22 of a random 22 were questions
    where the place genuinely bears on the answer. The cost is about 18% of the
    true positives, all of them bare place names with nothing beside them:
    `Texas weed identification`, `Is this Florida poison-Ivy?`.

    A `poprank >= 12` alternative was measured too: it recovers 445 of those at
    roughly seven in ten precision, which would take the whole rule to about 89%.
    Declined, because the ones it adds are led by `Brussels sprouts`, `Turkey
    bacon`, `Chili`, `Philadelphia cooking creme` and `Victoria sandwich` - the
    cooking corpus colliding with the map - and precision is what this action
    needs. The numbers are here so the choice can be reversed on purpose."""
    anchored = (m["prev"] in LOCATIVE_ANCHOR
                or m["comma_before"] or m["comma_after"])
    compound = (m["next"][:1].isupper() and not m["next_is_place"]
                and not m["comma_after"])
    return anchored and not compound


def resolve(text, limit=6):
    """The places that answer to this name, best first, with the label a human
    should read. Straight through to the gazetteer: no second ranking here."""
    got = _gz.candidates(_db, text, limit=limit)
    for g in got:
        g["label"] = _gz.label(_db, g)
    return got[:limit]


def classify(question):
    """What the node should do with this question.

    `intent` is one of:
      spatial            - a cue and a place: answer the spatial question
      place              - a place and no cue: answer normally, attach the place
      cue_without_place  - a cue and no place: nothing to look up, say so
      None               - neither
    """
    out = {"question": question, "intent": None, "cue": None, "places": [],
           "distance_km": None, "why": None, "degraded": None}
    if not available():
        out["degraded"] = _error
        out["why"] = "no gazetteer is loaded, so no question can name a place."
        return out

    out["cue"] = cue(question)
    found = mentions(question)
    # TWO THRESHOLDS, ONE MATCHER. A cue word is evidence, so with one the loose
    # set is used; without one the question has to point at the place itself.
    # See is_place_reference for what each costs.
    use = found if out["cue"] else [m for m in found if is_place_reference(m)]
    for m in use:
        cands = resolve(m["text"])
        if cands:
            out["places"].append({"text": m["text"], "best": cands[0],
                                  "others": cands[1:]})

    if out["cue"] and out["places"]:
        out["intent"] = "spatial"
        out["why"] = ("the question says %r and names %s, so it is asking about "
                      "a place." % (out["cue"],
                                    ", ".join(p["text"] for p in out["places"])))
    elif out["places"]:
        out["intent"] = "place"
        out["why"] = ("the question is anchored on %s, which is a place, but "
                      "nothing in it asks about the place itself. Context rather "
                      "than the question."
                      % ", ".join(p["text"] for p in out["places"]))
    elif out["cue"]:
        out["intent"] = "cue_without_place"
        out["why"] = ("the question says %r but names no place the gazetteer "
                      "holds, so there is nothing to look up. Naming a town "
                      "would make it answerable." % out["cue"])
    else:
        out["why"] = "no spatial cue and no place name."

    # TWO PLACES IS A DISTANCE QUESTION, and the answer is free: the gazetteer
    # module already measures great-circle distance, wrapped across the
    # antimeridian, because it needs it to decide what counts as the same place.
    if len(out["places"]) >= 2:
        a, b = out["places"][0]["best"], out["places"][1]["best"]
        out["distance_km"] = round(_gz._km(a["lat"], a["lon"], b["lat"], b["lon"]), 1)
    return out



# ---------------------------------------------------------------------------
# THE PASSAGE SIDE. Measured 2026-09-07 on 5,800 chunks sampled uniform by line
# from all 29 artifacts, population 2,315,810. BUILD-LOG has the cascade.
#
#   mentions() as shipped                        50.7% of chunks
#   + is_place_reference()                       16.3%
#   + the capital was not free (clause start)    15.1%
#   + not an acronym of 3 or more capitals       14.6%
#   + poprank >= 10                               9.4%   ~83% by hand on 40
#   + not an AMBIGUOUS name                       see measure-spatial-cues.py
#
# WHY A QUESTION'S RULES ARE NOT ENOUGH. `mentions()` rests on capitalisation
# and questions are sentence-cased, so one word in a question gets a free
# capital and it is nearly always a function word already excluded. A passage
# has dozens of sentences and their first words are ordinary nouns: Salt,
# Phillips, Since, Page, Guide, Plan, Center. Honouring the clause flag that
# `_words` had been computing and nobody read is worth 7.6 points on passages
# and exactly nothing on questions, where it was re-measured and moved no rate.
# HALF THE ARCHIVE IS SPANISH AND FUNCTION_WORDS IS NOT. It has bitten twice in
# one measurement: `Para`, `Una`, `Este` and `Les` opened sentences and matched
# hamlets, and `en Etiopia` looked like a compound because `En` is capitalised at
# the start of a sentence and is not in the English list.
#
# USED BY THE PASSAGE PATH ONLY, deliberately. Every published question-side rate
# was measured against FUNCTION_WORDS as it stands, and widening it there would
# invalidate those numbers without re-running the measurement. Doing that is a
# candidate, not a side effect of this change.
FUNCTION_WORDS_ES = frozenset("""
    a al ante bajo cabe con contra de del desde durante en entre hacia hasta
    mediante para por segun sin so sobre tras via el la los las un una unos unas
    lo y e o u ni que quien quienes cual cuales cuyo cuya como cuando donde
    porque aunque pero sino si no ya solo tambien tampoco muy mas menos todo
    toda todos todas otro otra otros otras cada mismo misma este esta estos
    estas ese esa esos esas aquel aquella su sus mi mis tu tus nuestro nuestra
    es son era eran ser estar esta estan fue fueron hay haber
    le les leur leurs dans pour avec sans sous chez du des
""".split())

AMBIGUOUS_MIN_LOWER = 0.20
PASSAGE_POPRANK = 10

# NAMES THE CORPUS MOSTLY WRITES IN LOWER CASE, so a capital is an accident of
# position rather than evidence of a place. Derived, not invented: every
# single-word name the gate above let through was counted across 16,643 chunks
# from all 29 artifacts, and this is every one whose lowercase share passed
# AMBIGUOUS_MIN_LOWER with at least 20 occurrences. The share is kept beside each
# so the cut can be argued with. Regenerate with:
#
#     python bin/measure-spatial-cues.py --passages --derive
#
# AN EARLIER DERIVATION FAILED AND IS RECORDED SO IT IS NOT RETRIED. Ranking
# names by how far their firings concentrate in one artifact put Nevada, Tucson,
# Sfax, Gabes, Kosovo, Benin and Ethiopia at the top - every one correct,
# because a groundwater report about Nevada talks about Nevada. It never reached
# Colon. Concentration measures what a corpus is about, not whether a word is a
# place.
AMBIGUOUS = {
    "nice":    0.926,   # the adjective
    "split":   0.913,   # to split
    "mobile":  0.883,   # mobile phone, and iFixit is 34% of the archive
    "colon":   0.832,   # sigmoid colon, colon cancer - the medical corpora
    "springs": 0.832,   # springs of water, in a groundwater report
    "cita":    0.783,   # Spanish for appointment
    "turkey":  0.579,   # the bird, and a cooking corpus
    "lima":    0.481,   # lima beans
    "guinea":  0.333,   # guinea pig, guinea fowl
    "island":  0.271,   # nearly always the tail of a longer name
}


def in_passage(m):
    """Should this mention, found in PASSAGE text, be shown to the operator?

    Everything `is_place_reference` asks, plus what only a passage needs. Each
    condition below cost something measurable and is not here on taste:

    NOT A FREE CAPITAL. See above. The single largest passage-only error class.

    NOT A SHORT ACRONYM. `mentions` already drops four capitals or more, which
    lets `FAO` through 123 times in a corpus published by the FAO.

    POPRANK >= 10. Without it the residue is led by `Valley` and `University`,
    the generic tails of longer proper nouns. With it the most-matched names are
    United States, California, India, Rome, China, Canada, Australia, Utah.

    NOT AMBIGUOUS, unless the next word is itself a place after a comma. The
    exception is the `Ottawa, Canada` shape that `is_place_reference` already
    relies on: `Lima, Peru` and `Turkey, Greece` are places being listed, and
    excluding the name outright would throw those away with the guinea pigs."""
    if not is_place_reference(m):
        return False
    if m["clause"]:
        return False
    if m["text"].isupper() and len(m["text"]) >= 3:
        return False
    if (m["poprank"] or 0) < PASSAGE_POPRANK:
        return False
    if m["text"].lower() in AMBIGUOUS:
        return m["comma_after"] and m["next_is_place"]
    # THE MIRROR OF THE COMPOUND RULE, LOOKING BACKWARD. `is_place_reference`
    # rejects `Turkey Stock` by what follows and has nothing to say about
    # `George Washington University`, `Lake Victoria`, `WRNO New Orleans`,
    # `Avenue New York` or `Institute Rome` - the last being how the FAO corpora
    # write their own publisher. A comma in front reverses it, exactly as a comma
    # behind reverses the forward rule, because `Wadi Labka, Eritrea` is two
    # things and not one.
    #
    # THE FUNCTION-WORD EXEMPTION IS NOT OPTIONAL and was measured: without it
    # the rule threw away `In Australia`, `In California`, `In India` - a
    # sentence-initial preposition is capitalised, so the most common correct
    # construction in the corpus looked like a compound. It removed four
    # Australias to catch one George.
    if (m["prev_capital"] and not m["prev_is_place"]
            and not m["comma_before"]
            and m["prev"] not in FUNCTION_WORDS
            and m["prev"] not in FUNCTION_WORDS_ES):
        return False
    return True


def passage_places(text, limit=3):
    """The places a retrieved passage is about, best first, deduplicated.

    A passage that names Wisconsin four times is one pin, not four. Ordered by
    population rank because the operator sees at most `limit` of them and the
    prominent one is the likelier subject; ties keep the order they appear in.
    """
    seen = {}
    for m in mentions(text):
        if not in_passage(m):
            continue
        if m["key"] in seen:
            seen[m["key"]]["count"] += 1
            continue
        cands = resolve(m["text"], limit=3)
        if not cands:
            continue
        seen[m["key"]] = {"text": m["text"], "count": 1,
                          "best": cands[0], "others": cands[1:]}
    out = sorted(seen.values(),
                 key=lambda p: -(p["best"]["poprank"] or 0))[:limit]
    return [card(p) for p in out]


def climate_runner():
    """The interpreter that can actually run bin/koppen-lookup.py, or None.

    CHECKED, NOT ASSUMED. The card used to print `python bin/koppen-lookup.py`,
    and on the machine this was built on that command FAILS: the script needs
    rasterio, which lives in the geospatial environment and not in the
    interpreter that runs this server. The operator manual carried the same
    wrong command until 2026-09-07, which is a printed instruction that does not
    work - the worst kind this build produces.

    `find_spec` answers the question without importing rasterio, which drags
    GDAL into this process and costs about a second."""
    import importlib.util
    try:
        if importlib.util.find_spec("rasterio") is not None:
            return sys.executable
    except (ImportError, ValueError):
        pass
    return None


def _sh(word):
    """Quote a token so it survives the shell the manual tells the operator to use.

    THE COMMAND THIS CARD PRINTS IS MEANT TO BE PASTED, and the manual says to
    paste it into Git Bash. Two ways that breaks unquoted:

      C:\\venv\\Scripts\\python.exe      -> bash eats the backslashes and reports
                                       `C:venvScriptspython.exe: command not
                                       found`, naming a command nobody typed
      ... 37.3352 -121.8933 San Jose  -> San and Jose are two arguments

    Both are the same defect this build keeps finding: output that is confident,
    well formed, and wrong in a way the error message hides."""
    if word and not re.search(r"[^\w./:+-]", word):
        return word
    return '"' + word.replace('"', '\\"') + '"'


# ZOOM IS A JUDGEMENT, NOT A MEASUREMENT. The viewer takes `#lat,lon,zoom` and
# falls back to its own default when the zoom is absent, which drew a country
# and a village at the same scale: z11 on a country shows one suburb of its
# capital, z5 on a village is a pixel. These three were chosen by eye. They are
# the kind of number to change on sight rather than to defend.
_ZOOM = {"country": 5, "region": 7, "locality": 11}


def _map_url(g):
    """The viewer link for one gazetteer row, zoomed to suit what it is."""
    return "/map/#%.4f,%.4f,%d" % (g["lat"], g["lon"],
                                   _ZOOM.get(g["kind"], 9))


def card(place):
    """What to show for one resolved place.

    The climate is a command rather than an answer, and the reason is harder
    than it looks: `bin/koppen-lookup.py` needs rasterio, a win_amd64-only
    wheel (spec 11.9, section 12 item 7). Importing it here would make the
    node's spatial answer depend on the one wheel set the archive cannot yet
    build for Linux, so the gap is named instead of hidden."""
    g = place["best"] if "best" in place else place
    runner = climate_runner()
    name = g["label"].split(" (")[0]
    out = {
        "label": g["label"],
        "kind": g["kind"],
        "poprank": g["poprank"],
        "lat": round(g["lat"], 4),
        "lon": round(g["lon"], 4),
        "map": _map_url(g),
        "zoom": _ZOOM.get(g["kind"], 9),
        # ABSOLUTE, because a pasted command should not depend on which
        # directory the operator happens to be in. `bin/koppen-lookup.py` works
        # from the archive root and fails from anywhere else with an error about
        # a missing file, which reads like a broken archive rather than a wrong
        # working directory.
        "climate_command": "%s %s %.4f %.4f %s"
                           % (_sh(runner or "python"),
                              _sh(os.path.join(store.BIN, "koppen-lookup.py")),
                              g["lat"], g["lon"], _sh(name)),
        "climate_note": None if runner else
                        ("the interpreter running this server cannot import "
                         "rasterio, so `python` is a guess. Use the geospatial "
                         "environment (spec 12.9); the manual's opening table "
                         "has a line for its path on this machine."),
        "others": len(place.get("others", [])),
        # THE ALTERNATES, WITH COORDINATES, BECAUSE A COUNT IS NOT ACTIONABLE.
        # `others` alone lets the surface say a name is ambiguous and gives the
        # operator no way to settle it. `cairo` answering Georgia is a defect
        # this build actually had (BUILD-LOG 09-06); proximity identity fixed
        # the index, and this is the half that reaches the person reading.
        #
        # THEY CANNOT BE LABELLED APART. The tiles carry no admin hierarchy, so
        # every one of Franklin's six is the string "Franklin" and there is no
        # honest way to write "Franklin, Wisconsin" from what is stored. What
        # distinguishes them is where they are, so the coordinates travel with
        # each one and the surface settles it on the map rather than in prose.
        # Three, because this is a strip and not a list; the rest are one more
        # /api/spatial call away.
        "others_brief": [{"label": o["label"], "kind": o["kind"],
                          "poprank": o["poprank"],
                          "lat": round(o["lat"], 4), "lon": round(o["lon"], 4),
                          "map": _map_url(o)}
                         for o in place.get("others", [])[:3]],
    }
    return out
