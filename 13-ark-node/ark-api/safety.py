#!/usr/bin/env python3
"""
safety.py - the safety-term detector. Standard library only, no model, no index.

    python 13-ark-node/ark-api/safety.py --selftest
    python 13-ark-node/ark-api/safety.py "amoxicillin 500 mg every 8 hours"

WHAT IT ANSWERS. "Is this the kind of statement rule 9.2 says to verify against
the archive before acting on it?" Nothing else. It does not know whether a dose is
correct, safe, or even real. A detector that appeared to judge correctness would be
worse than none, because §11.1's failure mode is fluent confident wrongness and a
green light from this file would be one more fluent confident thing.

WHY IT IS ONE COMPONENT. NODE-ARCHITECTURE §7 item 5: three consumers, one
mechanism.

    §9.5  safety-consequential answers are rendered as TEXT, never audio alone
    §9.4  disagreement between model families is worth its cost only sometimes
    §9.2  "verify this against the archive" needs to say WHICH part to verify

THEY DO NOT WANT THE SAME ANSWER, AND THAT IS THE WHOLE DESIGN. Suppressing audio
costs nothing, so §9.5 should fire on anything plausible. A cross-check on Phase 1
hardware costs a model swap - roughly double latency plus load time, against
constraint 3.1, which is priority one - so §9.4 needs a much higher bar. A boolean
would force one threshold on both, and whichever way it was set the other consumer
would be wrong. So this returns a graded level and each consumer picks its own line.

THE ASYMMETRY THAT SETS THE DEFAULTS. A missed dose gets spoken aloud and cannot be
re-read (§11.8). A false alarm merely costs a rendered line of text. That argues for
recall over precision - but only up to the point where the operator stops believing
it. A detector that fires on "the room was 20 C" teaches its user to ignore it, and
an ignored detector is a false negative with extra steps. Hence two tiers: units
that are safety-consequential on their own, and units that need a reason.

BILINGUAL BY REQUIREMENT, NOT BY COURTESY. The archive is English and Spanish (§1).
Units are language-neutral; the context words that lift an ambiguous number are not.
An English-only detector would silently exempt half the corpus, which is the same
class of error as a verify pass that skips the shelves with no manifest.
"""

import re
import sys
import unicodedata

# --------------------------------------------------------------------------
# 1. UNITS
#
# HARD  - safety-consequential wherever it appears. Nobody writes "500 mg" or
#         "20 psi" about something that does not matter.
# SOFT  - safety-consequential only in context. "100 C" is a kettle or an
#         autoclave; "5 kg" is a sack of rice or a roof load. These count only
#         when a context word from section 2 sits near them.
#
# Ordered longest-first inside each pattern so that "mg/kg" is not matched as
# "mg", and "mAh" is not matched as "mA". Getting this wrong does not throw; it
# silently reports the wrong category, which is the kind of defect that survives
# a passing test suite.
# --------------------------------------------------------------------------

HARD = {
    "dose": [
        "mg/kg/day", "mg/kg/dia", "mg/kg", "mcg/kg", "ug/kg",
        "mg", "mcg", "ug", "IU", "UI", "mEq", "mmol", "mmol/L",
        "milligram", "milligrams", "miligramo", "miligramos",
        "microgram", "micrograms", "microgramo", "microgramos",
    ],
    "electrical": [
        "kV", "mV", "uV", "mA", "uA", "kA", "mAh", "Ah", "VA", "kVA",
        "AWG", "kOhm", "MOhm", "ohm", "ohms", "ohmio", "ohmios",
    ],
    "pressure": [
        "psi", "psig", "kPa", "MPa", "mmHg", "inHg", "cmH2O",
        "bar", "bares", "atm", "kgf/cm2",
    ],
    "torque_tolerance": [
        "Nm", "N.m", "ft-lb", "ft.lb", "lbf-ft", "lb-ft", "in-lb", "inlb",
        "kgf.m", "um", "micron", "microns", "micra", "thou", "mils",
    ],
    "load": ["kN", "MN", "kgf", "psf", "kg/m2", "lb/ft2", "tonne", "tonnes"],
    "toxicity": [
        "ppm", "ppb", "mg/L", "ug/L", "mg/m3", "ug/m3",
        "LD50", "LC50", "TLV", "PEL", "IDLH",
    ],
}

SOFT = {
    # "g" alone is deliberately absent: "Molar mass 217.268 g" is on every drug
    # page in mdwiki, and it is not a dose. Spelled-out grams are kept.
    "dose": ["mL", "ml", "cc", "gram", "grams", "gramo", "gramos",
             "tablet", "tablets", "tableta", "tabletas", "comprimido",
             "comprimidos", "capsule", "capsules", "capsula", "capsulas",
             "drop", "drops", "gota", "gotas", "teaspoon", "cucharadita"],
    "electrical": ["V", "volt", "volts", "voltio", "voltios", "A", "amp",
                   "amps", "amperio", "amperios", "W", "kW", "watt", "watts",
                   "vatio", "vatios", "Hz", "kHz", "MHz"],
    # "K" removed: kelvin is rare in this corpus and "5 K/min" and potassium are
    # not. C and F stay - they are the units the archive actually writes.
    "temperature": ["C", "F", "degC", "degF", "celsius", "centigrade",
                    "centigrado", "centigrados", "fahrenheit", "grados"],
    # "in" removed: it matched "1949 in Bukit Timah". Inches spelled out survive.
    "torque_tolerance": ["mm", "cm", "inch", "inches", "pulgada",
                         "pulgadas", "milimetro", "milimetros"],
    # "N" removed: "C 13 H 15 N O 2" is a chemical formula, and bare newtons in
    # prose are rare enough not to be worth the whole of mdwiki firing as loads.
    "load": ["kg", "kilo", "kilos", "kilogram", "kilograms", "kilogramo",
             "kilogramos", "lb", "lbs", "pound", "pounds", "libra", "libras",
             "ton", "tons", "tonelada", "toneladas"],
}

# Dosing frequency carries no number of its own but is unambiguous where it
# appears, and is the half of a prescription that a transcription error ruins
# most quietly: "q4h" heard as "q6h" is a plausible sentence and a wrong dose.
# po / im / iv / sc / bid / qd were here and are removed. They are routes and
# schedules in a clinic and ordinary text everywhere else: "PO Box", "IV" as a
# Roman numeral, "bid" as a verb. On the real corpus "IV" alone was the second
# most common dose finding in mdwiki and every instance was prose.
FREQUENCY = [
    "q4h", "q6h", "q8h", "q12h", "q24h", "qid", "tid", "qhs", "prn",
    "sublingual", "c/4h", "c/6h", "c/8h", "c/12h",
    "cada 4 horas", "cada 6 horas", "cada 8 horas", "cada 12 horas",
]

# --------------------------------------------------------------------------
# 2. CONTEXT WORDS
#
# These lift a SOFT unit, or a bare number, into a finding. Both languages, and
# unaccented because the text is folded before matching - "presion" matches
# "presión" without needing the corpus to be consistently accented, which it is
# not.
# --------------------------------------------------------------------------

CONTEXT = {
    "dose": ["dose", "doses", "dosage", "dosis", "posologia", "administer",
             "administered", "administrar", "administre", "take", "tome",
             "tomar", "give", "dar", "inject", "inyectar", "inyeccion",
             "infusion", "overdose", "sobredosis", "prescrib", "receta",
             "pediatric", "pediatrico", "adult", "adulto", "daily", "diaria",
             "diario", "per kg", "por kg", "oral", "intravenous", "intravenosa"],
    "electrical": ["voltage", "voltaje", "tension", "current", "corriente",
                   "mains", "red electrica", "supply", "alimentacion", "fuse",
                   "fusible", "breaker", "disyuntor", "circuit", "circuito",
                   # "shock" and "descarga" REMOVED - the fourth homonym this
                   # file has learned the same way, found 2026-09-05 when "what do
                   # I do for someone in shock" came back ELECTRICAL. In an archive
                   # holding both field medicine and wiring guides, a patient in
                   # shock and an electric shock are the same five letters, and
                   # "descarga" is also a discharge of water. The PHRASES survive,
                   # because a phrase cannot hide inside another word - which is
                   # the rule this whole vocabulary is built on, after "dar" inside
                   # standard, "nut" inside NUT carcinoma, and a canner load.
                   "electric shock", "descarga electrica",
                   "earth", "ground", "tierra", "live",
                   "neutral", "wire", "cable", "battery", "bateria", "charge",
                   "carga", "rated", "nominal", "solar", "inverter", "inversor"],
    "pressure": ["pressure", "presion", "psi", "inflate", "inflar", "cylinder",
                 "cilindro", "tank", "tanque", "valve", "valvula", "regulator",
                 "regulador", "boiler", "caldera", "burst", "rotura", "relief"],
    "temperature": ["sterilize", "sterilise", "esterilizar", "autoclave",
                    "pasteuriz", "boil", "hervir", "store", "storage",
                    "conservar", "almacenar", "refrigerate", "refrigerar",
                    "freeze", "congelar", "kiln", "temper", "anneal", "recocido",
                    "melting", "fusion", "ignition", "ignicion", "burn",
                    "quemadura", "fever", "fiebre", "hypothermia", "hipotermia",
                    "cook", "cocinar", "internal temperature"],
    "torque_tolerance": ["torque", "par de apriete", "tighten", "apretar",
                         "tolerance", "tolerancia", "clearance", "holgura",
                         "gap", "fit", "ajuste", "bolt", "perno", "tornillo",
                         "thread", "rosca", "calibrat", "calibrar"],
    "load": ["safe working load", "working load", "breaking load", "dead load",
             "live load", "point load", "load bearing", "load capacity",
             "carga de trabajo", "carga de rotura", "carga viva", "carga muerta",
             "capacity", "capacidad", "breaking", "rotura", "yield strength",
             "tensile", "beam", "viga", "joist", "vigueta", "anchor", "anclaje",
             "sling", "eslinga", "rappel", "belay", "harness", "arnes",
             "scaffold", "andamio", "hoist", "winch", "cabrestante"],
    "toxicity": ["toxic", "toxico", "toxicity", "toxicidad", "lethal", "letal",
                 "poison", "veneno", "venenoso", "exposure", "exposicion",
                 "inhalation", "inhalacion", "carcinogen", "cancerigeno",
                 "contaminant", "contaminante", "potable", "drinking water",
                 "agua potable", "chlorine", "cloro", "bleach", "lejia",
                 "lead", "plomo", "mercury", "mercurio", "arsenic", "arsenico"],
}

# Units that belong to two categories in this corpus, checked in order. Pressure
# before load for pounds because "11 pounds pressure" in a canner is the reading
# that matters: the USDA guide writes canner pressure in pounds, and botulism is
# the consequence of getting it wrong.
SHARED_UNITS = {
    "lb": ["pressure"], "lbs": ["pressure"],
    "pound": ["pressure"], "pounds": ["pressure"],
    "libra": ["pressure"], "libras": ["pressure"],
    "kg": ["dose"], "kilogram": ["dose"], "kilograms": ["dose"],
    "kilogramo": ["dose"], "kilogramos": ["dose"],
}

# Words that make a bare number safety-relevant with no unit at all:
# "give two tablets", "set the regulator to 30", "torque to 90".
BARE_NUMBER_CONTEXT = (
    CONTEXT["dose"] + CONTEXT["torque_tolerance"] + CONTEXT["toxicity"]
)

# --------------------------------------------------------------------------
# 3. PART NUMBERS
#
# THE CATEGORY MOST LIKELY TO EMBARRASS THIS FILE. A part number is "letters and
# digits together", and so are COVID-19, IPv4, H2O, v1.6, ISO9001 and every
# section reference in the spec. The rule below is deliberately narrow: it wants
# a shape that looks like a component marking, and it refuses anything that a
# document would produce by accident. Recall is knowingly traded away here,
# because a detector that fires on "§9.5" in its own documentation loses the
# operator's trust for every OTHER category, and those are the ones that matter.
# --------------------------------------------------------------------------

PART_RE = re.compile(r"""
    (?<![\w/§.-])
    (
        (?:\d{1,2}[A-Z]{1,3}\d{2,5}[A-Z]?)      # 2N3904, 1N4007, 74HC595
      | (?:[A-Z]{2,4}\d{2,5}[A-Z]{0,3})         # LM317T, BC547, NE555, ATmega
      | (?:\d{4,5})(?=\s*(?:cell|battery|bateria|celda))   # 18650 cell
      | (?:M\d{1,2}(?:x\d{1,2}(?:\.\d)?)?)      # M8, M8x1.25
      | (?:AWG\s?\d{1,2})
    )
    (?![\w-])
""", re.VERBOSE)

# A component marking now needs a REASON, exactly as an ambiguous unit does.
# Shape alone matched DrugBank identifiers (DB08888), KEGG codes and fragments of
# SMILES strings (CCOC12CCC) across every drug page in the medical corpus: 120
# findings in a random sample, none of them a part.
PART_CONTEXT = [
    "part", "parts", "part number", "part no", "model", "modelo", "replacement",
    "repuesto", "spare", "datasheet", "pinout", "transistor", "resistor",
    "resistencia", "capacitor", "condensador", "diode", "diodo", "mosfet",
    "opamp", "regulator", "regulador", "relay", "rele", "fuse", "fusible",
    "chip", "microcontroller", "microcontrolador", "bearing", "rodamiento",
    "belt", "correa", "filter", "filtro", "gasket", "junta",
    "bolt", "perno", "screw", "tornillo", "tuerca", "washer", "arandela",
    "valve", "valvula", "pump", "bomba", "motor", "socket", "connector",
    "conector", "battery", "bateria", "thread", "rosca",
]

# CHEMICAL DATA CARDS ARE NOT INSTRUCTIONS.
# Every drug page in mdwiki and wikipedia_medicine carries an infobox whose
# standard-state boilerplate is "at 25 C [77 F], 100 kPa" and whose formula line
# is "C 13 H 15 N O 2". Read as prose that is a temperature, a pressure and a
# 15-newton load. It is none of those; it is a property table, and rule 9.2 is
# about a statement someone is about to ACT on. Three or more of these markers in
# one passage means the passage is a data card, and the whole of it is suppressed.
INFOBOX_MARKERS = [
    "chemical formula", "molar mass", "smiles", "inchi", "drugbank",
    "chemspider", "pubchem", "cas number", "kegg", "unii", "iuphar",
    "formula quimica", "peso mol", "datos quimicos", "3d model",
]
INFOBOX_MIN = 3

PART_STOPWORDS = {
    "COVID", "IPV4", "IPV6", "MP3", "MP4", "H2O", "CO2", "NO2", "SO2",
    "ISO", "IEC", "ANSI", "ASTM", "NFPA", "OSHA", "USB", "HDMI", "PDF",
    "HTML", "HTTP", "JSON", "SQL", "GPS", "LED", "USA", "UTC", "WGS84",
}

# --------------------------------------------------------------------------
# 4. MATCHING
# --------------------------------------------------------------------------

def fold(s):
    """Lowercase and strip accents, so 'presión' and 'presion' are one word.

    NFKD then dropping combining marks, rather than a hand-written table: the
    corpus contains Spanish, and a table would be one missing character away
    from a silent miss in exactly the language that is already thinner."""
    s = unicodedata.normalize("NFKD", s.lower())
    return "".join(c for c in s if not unicodedata.combining(c))


def _unit_alternation(units):
    # longest first, so mg/kg beats mg and mAh beats mA
    esc = sorted((re.escape(u) for u in units), key=len, reverse=True)
    return "|".join(esc)


WORDNUM = ("one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|half|"
           "un|una|dos|tres|cuatro|cinco|seis|siete|ocho|nueve|diez|once|doce|media")
NUM = (r"(?:\d{1,3}(?:[ \u00a0]\d{3})+"
       r"|\d{1,5}(?:[.,]\d{1,4})?(?:\s*(?:-|to|a|hasta)\s*\d{1,5}(?:[.,]\d{1,4})?)?"
       r"|(?:" + WORDNUM + r"))")

# CASE IS EVIDENCE FOR SOME UNITS AND NOISE FOR OTHERS.
# The USDA canning guide writes "10 to 15 PSIG" and "15 PSI" in capitals, and a
# case-sensitive match missed four of its six canner-pressure passages - in the
# one book in this archive where the consequence of a missed pressure is
# botulism. But matching case-insensitively everywhere turns "A" into amperes in
# every sentence, "C" into celsius, "IU" into "ui", and "cc" into a carbon copy.
# So the short, capitalised and single-letter units keep their case and the
# spelled-out ones do not.
CASE_STRICT = {
    "V", "A", "W", "K", "C", "F", "N", "g", "in", "cc", "T", "M",
    "IU", "UI", "Ah", "mAh", "VA", "kVA", "mA", "uA", "kA", "mV", "uV", "kV",
    "Nm", "N.m",
}


def _build(units):
    # (?<![\w./]) - the slash keeps "05/02/18 Ah" from being 18 amp-hours.
    strict = [u for u in units if u in CASE_STRICT]
    loose = [u for u in units if u not in CASE_STRICT]
    out = []
    for group, flags in ((strict, 0), (loose, re.IGNORECASE)):
        if group:
            out.append(re.compile(
                r"(?<![\w./])(" + NUM + r")\s*(" + _unit_alternation(group)
                + r")(?![A-Za-z0-9])", flags))
    return out

HARD_RE = {c: _build(u) for c, u in HARD.items()}
SOFT_RE = {c: _build(u) for c, u in SOFT.items()}
FREQ_RE = re.compile(r"(?<![\w])(" + _unit_alternation(FREQUENCY) + r")(?![\w])",
                     re.IGNORECASE)
FREQ_PHRASE_RE = re.compile(
    r"(?:every|each|cada)\s+\d{1,2}\s*(?:hours?|hrs?|horas?|days?|dias?)"
    r"|\d{1,2}\s*(?:times?|veces)\s+(?:a|per|al|por)\s+(?:day|dia)",
    re.IGNORECASE)
BARE_RE = re.compile(r"(?<![\w.])(" + NUM + r")(?![\w.%])")

WINDOW = 60     # characters either side counted as "near"


_TOKEN = re.compile(r"[a-z0-9]+")

def _near(folded, start, end, words):
    """Context words must match as WORDS, not as substrings.

    THIS WAS THE DEFECT THAT MADE THE FIRST MEASUREMENT USELESS. `"dar" in ctx`
    is true inside "standard", "dark" and "dará"; `"take"` is true inside
    "mistake"; `"lead"` matches "leading" and `"fit"` matches "profit". Over a
    random sample of the real corpus those five words alone produced more than a
    thousand findings, and every one inspected was wrong. The detector looked
    like it was working because it was firing.

    Single words are matched against the tokenised window. Multi-word phrases
    ("per kg", "safe working", "internal temperature") still match as substrings,
    which is correct for a phrase and safe because a phrase is long enough not to
    hide inside another word."""
    left = max(0, start - WINDOW)
    ctx = folded[left:min(len(folded), end + WINDOW)]
    toks = set(_TOKEN.findall(ctx))
    for w in words:
        if " " in w:
            if w in ctx:
                return w
        elif w in toks:
            return w
    return None


def scan(text):
    """Every finding in one piece of text. Order is document order.

    Overlaps are resolved by span, not by category priority: '500 mg/kg' must not
    also report '500 mg'. The first (longest, hardest) match on a span wins and
    later ones are dropped."""
    if not text:
        return []
    folded = fold(text)
    if sum(1 for k in INFOBOX_MARKERS if k in folded) >= INFOBOX_MIN:
        return []
    found, taken = [], []

    def claim(a, b):
        for x, y in taken:
            if a < y and b > x:
                return False
        taken.append((a, b))
        return True

    for cat, rxs in HARD_RE.items():
      for rx in rxs:
        for m in rx.finditer(text):
            if claim(m.start(), m.end()):
                found.append({"text": m.group(0), "start": m.start(),
                              "end": m.end(), "category": cat, "tier": "hard",
                              "why": "unit is safety-consequential on its own"})

    for rx in (FREQ_RE, FREQ_PHRASE_RE):
     for m in rx.finditer(text):
        if claim(m.start(), m.end()):
            found.append({"text": m.group(0), "start": m.start(), "end": m.end(),
                          "category": "dose", "tier": "hard",
                          "why": "dosing frequency"})

    for cat, rxs in SOFT_RE.items():
      for rx in rxs:
        for m in rx.finditer(text):
            if not claim(m.start(), m.end()):
                continue
            # OFFERING EVERY UNIT TO EVERY VOCABULARY WAS TOO MUCH. It let a
            # context word rename the unit's category outright, so "5 mm" beside
            # "battery" became an electrical finding and ifixit's electrical
            # count tripled overnight on measurements that were lengths. Only the
            # units that GENUINELY belong to two categories get the second
            # lookup - pounds are a mass or a canner pressure, kilograms are a
            # mass or a per-kg dose - and the ambiguity is in the unit itself,
            # which is the only place it can honestly be declared.
            owner, hit = cat, None
            unit = m.group(2).lower()
            for cand in [cat] + SHARED_UNITS.get(unit, []):
                hit = _near(folded, m.start(), m.end(), CONTEXT.get(cand, []))
                if hit:
                    owner = cand
                    break
            if hit:
                tier = "lifted" if owner in HIGH_STAKES else "soft"
                found.append({"text": m.group(0), "start": m.start(),
                              "end": m.end(), "category": owner, "tier": tier,
                              "why": "ambiguous unit resolved by '%s'" % hit})
            else:
                # RECORDED, NOT COUNTED. Kept out of the findings entirely rather
                # than returned with a zero weight: a consumer that iterated the
                # list would otherwise treat "20 C" in a recipe as a finding, and
                # the reason this tier exists is to stop exactly that.
                taken.remove((m.start(), m.end()))

    for m in PART_RE.finditer(text):
        tok = m.group(1)
        if fold(tok).upper().rstrip("0123456789") in PART_STOPWORDS:
            continue
        if tok.upper() in PART_STOPWORDS:
            continue
        hit = _near(folded, m.start(), m.end(), PART_CONTEXT)
        if not hit:
            continue
        if claim(m.start(), m.end()):
            found.append({"text": tok, "start": m.start(), "end": m.end(),
                          "category": "part_number", "tier": "soft",
                          "why": "component marking near '%s'" % hit})

    # THE BARE-NUMBER RULE IS GONE, AND ITS ABSENCE IS THE MEASUREMENT.
    # A number with no unit, "near" a context word, was 1,688 of ~2,240 findings
    # over a random sample of the real corpus - three quarters of everything this
    # file said. Inspected, it was a chemistry infobox ("25", "77", "1"), a figure
    # number ("38.7") and a table row ("2.9 to 1.3"). Fixing the substring bug
    # above removes most of it; the rest is unfixable, because a number with no
    # unit carries no evidence and the rule was inventing some. "Give two tablets"
    # is still caught, by the word "tablets" - which is a unit, and is the point.

    found.sort(key=lambda f: f["start"])
    return found


# --------------------------------------------------------------------------
# 5. THE THREE CONSUMERS
# --------------------------------------------------------------------------

HIGH_STAKES = {"dose", "toxicity", "electrical", "pressure", "load"}

# A QUESTION CAN BE SAFETY-CONSEQUENTIAL WITH NO NUMBER IN IT.
# "what dose of amoxicillin for a child" contains no unit, so span matching found
# nothing and the verify banner stayed silent - on the single most §9.2 question
# a person could type. Every figure was in the passages that came back, which is
# exactly the moment the rule applies. Intent is read from the question's own
# vocabulary and lands at `possible`: worth telling the operator to verify, not
# worth a model swap, because there is still no number to cross-check.
INTENT_CATS = HIGH_STAKES | {"torque_tolerance"}


def intent(query):
    """Categories a question ASKS about, independent of any figure it contains.

    Terms of three characters or fewer are skipped: they are the ones that turn
    into other words, and that lesson has been learned three times in this file."""
    if not query:
        return []
    folded = fold(query)
    toks = set(_TOKEN.findall(folded))
    out = set()
    for cat in INTENT_CATS:
        for w in CONTEXT.get(cat, []):
            if len(w) <= 3:
                continue
            if (w in folded) if " " in w else (w in toks):
                out.add(cat)
                break
    return sorted(out)


def assess(text, query=None, unsourced=False):
    """The whole answer, for all three consumers.

    `unsourced` says the answer carried an UNSOURCED - NOT FROM THE ARCHIVE
    block, i.e. the archive did not cover the question and the model offered its
    own training instead. It changes two things and not the detection: the verify
    prompt, because "verify against the archive" is not available advice when the
    archive is precisely what lacked it, and the cross-check, because a second
    model family disagreeing is then the ONLY verification there is.

    `query` is scanned too and folded into the same level. A question that asks
    for a dose is safety-consequential even when the answer that came back was
    'the archive does not say' - the operator is about to act on a number either
    way, and §9.2 is about the decision, not the sentence."""
    findings = scan(text or "")
    qfind = scan(query or "") if query else []
    all_f = findings + qfind
    asked = intent(query)

    firm = [f for f in all_f if f["tier"] in ("hard", "lifted")]
    cats = sorted({f["category"] for f in all_f} | set(asked))

    if firm:
        level = "present"      # a unit that means what it says. Both rules fire.
    elif all_f or asked:
        level = "possible"     # a hint, or a question that asks for one. Text-only
    else:                      # is free so it fires; a model swap is not, so it
        level = "none"         # does not.

    return {
        "level": level,
        "categories": cats,
        "asked_about": asked,
        "findings": findings,
        "query_findings": qfind,
        # §9.5. Cheap consequence, so it fires on anything at all. The spec's
        # wording is "any answer containing a dose, voltage, tolerance, pressure,
        # temperature, or part number" - a list with no threshold in it.
        "render_text_only": level != "none",
        # §9.4. Expensive consequence on Phase 1 hardware: sequential model swap,
        # double latency, and power against priority-one constraint 3.1. So it
        # needs a hard unit, not a hint.
        # §9.4. Expensive on Phase 1 hardware, so normally it needs a hard unit.
        # AN UNSOURCED ANSWER ALWAYS WANTS IT, at any level: there is no passage
        # to read, so the second model family is the only instrument left. It is
        # attempted and reported, never gating - Juan's 2026-09-09 decision is
        # that an operator who is told is better served than one who is blocked,
        # so a figure is shown at once and the check updates behind it.
        # ALWAYS. Juan's decision 2026-09-10: the second model runs on every
        # answer, and an operator who wants to wait may wait. The threshold is
        # gone rather than lowered - it existed to ration a 2.5 minute CPU job,
        # and cancellation (2026-09-10, commit 7ee78ff) means an abandoned check
        # costs nothing, so the rationing has no purpose left. `level` and
        # `unsourced` still shape the WORDING below; they no longer decide
        # whether the check happens.
        "cross_check": True,
        # §9.2. The prompt has to name what to verify, or it is a nag.
        "verify_prompt": _prompt(all_f, level, unsourced),
    }


def _prompt(all_f, level, unsourced=False):
    # "VERIFY AGAINST THE ARCHIVE" IS THE WRONG SENTENCE FOR AN UNSOURCED
    # ANSWER, AND IT IS THE WORST PLACE TO BE WRONG. It sits next to a number
    # the operator may act on, and it implies a passage exists to check - when
    # the archive not holding one is the entire reason the block is there. So
    # the unsourced wording names the absence instead, and it fires even at
    # level "none", because an unsourced sentence with no unit in it is still
    # something the node cannot stand behind.
    if unsourced:
        seen, terms = set(), []
        for f in all_f:
            t = f["text"].strip()
            if t.lower() not in seen:
                seen.add(t.lower())
                terms.append(t)
            if len(terms) >= 6:
                break
        head = ("NOTHING IN THE ARCHIVE SUPPORTS THIS. It came from the model's "
                "training, not from any passage, so there is no page here to "
                "check it against.")
        if terms:
            head += (" Treat these as unverified: " + ", ".join(terms)
                     + ". Do not act on them without an outside source.")
        return head + (" Rule 9.2 - the model states specifics with confidence "
                       "it has not earned.")
    if level == "none":
        return None
    seen, terms = set(), []
    for f in all_f:
        t = f["text"].strip()
        if t.lower() not in seen:
            seen.add(t.lower())
            terms.append(t)
        if len(terms) >= 6:
            break
    return ("Verify against the archive before acting: " + ", ".join(terms)
            + ". Rule 9.2 - the model states specifics with confidence it has "
              "not earned.")


# --------------------------------------------------------------------------
# 6. SELFTEST
# --------------------------------------------------------------------------

CASES = [
    # (text, minimum level, a category that must appear)
    # ---- must reach PRESENT: a unit that means what it says ----------------
    ("amoxicillin 500 mg every 8 hours for 7 days", "present", "dose"),
    ("paracetamol 15 mg/kg cada 6 horas en ninos", "present", "dose"),
    ("the mains supply is 240 V and the fuse is rated 13 A", "present", "electrical"),
    ("inflate the cylinder to 120 psi and check the relief valve", "present", "pressure"),
    ("torque the head bolts to 90 Nm in three stages", "present", "torque_tolerance"),
    ("free chlorine should reach 2 ppm before the water is potable", "present", "toxicity"),
    ("la carga de rotura de la cuerda es 22 kN", "present", "load"),
    ("process pints at 11 pounds pressure in a dial-gauge canner", "present", "pressure"),
    ("pressure canners operated at 10 to 15 PSIG reach 240 F", "present", "pressure"),
    ("the safe working load of this sling is 400 kg", "present", "load"),
    ("conecte la bateria de 12 voltios al inversor", "present", "electrical"),
    # ---- must reach POSSIBLE: a hint, worth text but not a model swap ------
    ("replace the transistor with a 2N3904", "possible", "part_number"),
    ("autoclave at 121 C for 15 minutes to sterilize", "possible", "temperature"),
    ("give two tablets with food", "possible", "dose"),
    ("cook the chicken to an internal temperature of 74 C", "possible", "temperature"),
    # ---- must stay NONE: the cases that decide whether it is believed ------
    ("the room was about 20 C and the walk took an hour", "none", None),
    ("she wrote 300 pages in 2019 and sold 5000 copies", "none", None),
    ("see section 9.5 and the notes on page 14", "none", None),
    ("Wikipedia has 6 million articles in English", "none", None),
    ("the village had 2000 inhabitants and three schools", "none", None),
    ("published in 1987, reprinted in 2004 by a small press", "none", None),
    ("an average of 21 pounds is needed per canner load of 7 quarts", "none", None),
    ("CD34 positive in about half of NUT carcinoma cases", "none", None),
]

# QUESTIONS, not statements. These exercise intent(), which is the half of the
# detector that has no number to work from and is the half a search surface uses
# most: a person types a question, and the figures arrive in the passages.
QUERY_CASES = [
    ("what dose of amoxicillin for a child", "possible", "dose"),
    ("que dosis de paracetamol para un nino", "possible", "dose"),
    ("how much chlorine to disinfect drinking water", "possible", "toxicity"),
    ("what wire size for a 30 amp circuit", "present", "electrical"),
    ("pressure canning green beans", "possible", "pressure"),
    ("who wrote don quixote", "none", None),
    ("history of the printing press", "none", None),
    ("how do i start a fire without matches", "none", None),
    ("what do I do for someone in shock", "none", None),
    ("the outlet gives an electric shock when touched", "possible", "electrical"),
]

ORDER = ["none", "possible", "present"]


def selfcheck():
    """A one-line liveness answer for /api/health.

    Runs two of the labelled cases rather than reporting a version string: this
    file has no dependencies to fail, so the only way it can be broken is that
    its own tables are wrong, and only running it can tell you that."""
    try:
        a = assess("amoxicillin 500 mg every 8 hours")
        b = assess("the room was about 20 C and the walk took an hour")
        ok = a["level"] == "present" and b["level"] == "none"
        return {"ok": bool(ok), "categories": len(HARD) + len(SOFT),
                "cases": len(CASES)}
    except Exception as e:                                  # pragma: no cover
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}


def selftest():
    bad = 0
    for text, want, cat in CASES + [("?" + q, w, c) for q, w, c in QUERY_CASES]:
        if text.startswith("?"):
            text = text[1:]
        a = assess("", query=text) if (text, want, cat) in QUERY_CASES else assess(text)
        got = a["level"]
        ok = ORDER.index(got) >= ORDER.index(want) if want != "none" else got == "none"
        if ok and cat:
            ok = cat in a["categories"]
        if not ok:
            bad += 1
            print("  FAIL  want>=%-8s got=%-8s cats=%s\n        %s"
                  % (want, got, a["categories"], text))
        else:
            print("  ok    %-8s %-14s %s" % (got, ",".join(a["categories"])[:14], text[:46]))
    # THE UNSOURCED FLAG, WHICH NOTHING TESTED. `assess(unsourced=True)` has to
    # do two things no other input makes it do: force `cross_check` even at
    # level "none", and replace "verify against the archive" with wording that
    # does not promise a page exists. Both were written and neither was
    # checked, on the same day this build twice found a true statement that
    # nobody re-read. Four assertions, each naming the property rather than the
    # string, so a reworded prompt passes and a lost property fails.
    for text, label in [("About 500 mg every 8 hours.", "with a figure"),
                        ("Keep it clean and dry.", "no figure at all")]:
        a = assess(text, unsourced=True)
        checks = [
            ("cross_check forced", a["cross_check"] is True),
            ("prompt present", bool(a["verify_prompt"])),
            ("prompt names the absence",
             "ARCHIVE SUPPORTS THIS" in (a["verify_prompt"] or "").upper()),
            ("prompt does not promise a page",
             "verify against the archive" not in (a["verify_prompt"] or "").lower()),
        ]
        for name, ok in checks:
            if ok:
                print("  ok    unsourced %-28s %s" % (name, label))
            else:
                bad += 1
                print("  FAIL  unsourced %-28s %s\n        %s"
                      % (name, label, a["verify_prompt"]))
    # THE CONTROL, REWRITTEN 2026-09-10 RATHER THAN DELETED.
    # It used to assert that the same text WITHOUT the flag does not force a
    # cross-check - true until the threshold was removed, and it failed the
    # moment `cross_check` became unconditional. That is the assertion doing its
    # job: it noticed a contract change. What it must assert now is the contract
    # that actually holds - the check ALWAYS runs, and what the flag changes is
    # the WORDING, because "verify against the archive" is the wrong sentence
    # next to a figure no passage supports.
    plain = assess("Keep it clean and dry.")
    if plain["cross_check"] is True:
        print("  ok    cross-check is unconditional         (control)")
    else:
        bad += 1
        print("  FAIL  cross_check is not unconditional: %r" % plain["cross_check"])
    if plain["verify_prompt"] is None:
        print("  ok    an unflagged level-none answer gets no verify prompt")
    else:
        bad += 1
        print("  FAIL  level none produced a verify prompt: %r"
              % plain["verify_prompt"])

    total = len(CASES) + len(QUERY_CASES) + 10
    print("\n%d/%d" % (total - bad, total))
    return bad


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(1 if selftest() else 0)
    txt = " ".join(a for a in sys.argv[1:] if not a.startswith("--"))
    if not txt:
        print(__doc__.strip().splitlines()[0]); sys.exit(2)
    import json
    print(json.dumps(assess(txt), ensure_ascii=False, indent=1))
