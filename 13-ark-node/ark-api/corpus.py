"""
corpus.py - what the library holds, what it could hold, and where a question
the library cannot answer might find its material.

Two jobs, one module, standard library only (like store.py, and for the same
reasons: nothing to vendor, nothing a printed procedure has to install).

1. STATUS. Every corpus row of the catalog (a ZIM, PDF or folder on shelves 02
   to 08) gets one of four states, each with a one-line reason:

       green  on the drive and in the index: searchable AND citable
       amber  on the drive, not in the index: browsable in Kiwix (a ZIM) or a
              file only (a PDF or folder), but an answer cannot cite it
       grey   not installed. NOT A FAULT, the same rule the services page has
              followed since 2026-10-04: a starter kit leaves out about a
              hundred corpora on purpose, and a correct install must not look
              broken
       red    something expects it and it is not usable: in the index but gone
              from the drive (its citations would 404), a ZIM on the drive that
              kiwix-serve does not serve, or a file whose size differs from the
              catalog's

   A COLOUR WITHOUT A REASON IS NOT SOMETHING AN OPERATOR CAN ACT ON (the rule
   `degraded` already follows), so every row says why, and every row that can
   be changed says how: the fetch command, the publisher's page, the indexing
   steps, or the repair.

   Computed from file names and sizes only. Hashing is `ark.py verify`'s job and
   takes hours on the full archive; the size test is the cheap proxy that
   catches a truncated download.

2. SUGGESTIONS. When an answer is not grounded, the question is matched against
   `catalog/topics.csv` (hand-written: topics, English and Spanish keywords and
   a one-line description per corpus) and the best matches that are NOT already
   searchable are offered. With no model loaded and no index: a node whose
   model is down still says where the material might be.

   It NEVER claims a corpus holds the answer. It says "may cover", and it is
   shown beside the answer, never inside it, so the grounding badge keeps its
   one meaning.

Decisions behind this file: PLAN-corpus-status.md (Juan, 2026-10-06): four
states; home section plus a /library page; show the command, never download
from the node; curated topics rather than asking the model.
"""

import csv
import os
import re
import unicodedata
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
CATALOG_DIR = os.path.join(os.path.dirname(HERE), "catalog")
CATALOG = os.path.join(CATALOG_DIR, "catalog.csv")
TOPICS_CSV = os.path.join(CATALOG_DIR, "topics.csv")

CORPUS_KINDS = ("zim", "pdf", "folder")
CORPUS_SHELVES = ("02", "03", "04", "05", "06", "07", "08")

# THE TOPIC LIST IS FIXED HERE AND NOWHERE ELSE. topics.csv may only use these
# ids (catalog-build.py --check refuses any other), so a typo cannot quietly
# create a topic with one corpus in it. Order is the order the tiles are drawn.
TOPICS = [
    ("medicine", "Medicine", "Medicina"),
    ("first-aid", "First aid and emergencies", "Primeros auxilios y emergencias"),
    ("water", "Water and sanitation", "Agua y saneamiento"),
    ("food", "Food and agriculture", "Alimentos y agricultura"),
    ("energy", "Energy", "Energía"),
    ("repair", "Repair and tools", "Reparación y herramientas"),
    ("electronics", "Electronics and radio", "Electrónica y radio"),
    ("engineering", "Engineering and building", "Ingeniería y construcción"),
    ("outdoors", "Outdoors and survival", "Aire libre y supervivencia"),
    ("veterinary", "Animals and veterinary", "Animales y veterinaria"),
    ("science", "Science", "Ciencia"),
    ("math", "Mathematics", "Matemáticas"),
    ("computing", "Computing", "Informática"),
    ("education", "Courses and teaching", "Cursos y enseñanza"),
    ("law-economics", "Law, money and economics", "Derecho, dinero y economía"),
    ("language", "Language and dictionaries", "Idioma y diccionarios"),
    ("literature", "Books and literature", "Libros y literatura"),
    ("geography", "Maps and geography", "Mapas y geografía"),
    ("reference", "General reference", "Referencia general"),
]
TOPIC_IDS = tuple(t[0] for t in TOPICS)

STATES = ("green", "amber", "red", "grey")
STATE_LABELS = {"green": "citable", "amber": "browsable only",
                "red": "broken", "grey": "not installed"}
# best first, for a topic tile: one citable corpus makes the topic answerable
_TOPIC_RANK = {"green": 0, "amber": 1, "red": 2, "grey": 3}
# for suggestions: what is already on the drive first, a repair next, a
# download last, because a download is the slowest door
_SUGGEST_RANK = {"amber": 0, "red": 1, "grey": 2}


# ---------------------------------------------------------------- reading

def load_csv(path):
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def is_corpus(row):
    return (row.get("kind") in CORPUS_KINDS
            and (row.get("shelf") or "")[:2] in CORPUS_SHELVES)


def library_books(path):
    """{zim stem: {"title", "lang"}} from a kiwix library.xml, or None when the
    file is missing or unreadable. None means UNKNOWN, and an unknown library
    must never turn rows red: "not served" is only said when it is known."""
    if not path or not os.path.exists(path):
        return None
    try:
        root = ET.parse(path).getroot()
    except Exception:                       # noqa: BLE001 - unknown, not fatal
        return None
    out = {}
    for b in root.findall("book"):
        p = (b.get("path") or "").replace("\\", "/")
        stem = re.sub(r"\.zim$", "", p.rsplit("/", 1)[-1], flags=re.I)
        if stem:
            out[stem] = {"title": b.get("title") or "",
                         "lang": (b.get("language") or "").split(",")[0].strip()}
    return out


def _norm(p):
    return (p or "").replace("\\", "/").strip("/")


def row_path(row):
    return _norm(row["shelf"]) + "/" + _norm(row["file"])


def artifact_path(a):
    """Where an index artifact lives, relative to the archive root.

    A FOLDER ARTIFACT RECORDS ITS SHELF AS THE FOLDER ITSELF: shelf
    `07-corpora-supplemental/hesperian`, file `hesperian`. A ZIM records the
    shelf and the file name. Both reduce to one path."""
    shelf, f = _norm(a.get("shelf")), _norm(a.get("file"))
    if not f or shelf.rsplit("/", 1)[-1] == f:
        return shelf
    return shelf + "/" + f


def covers(apath, rpath):
    """An artifact covers a row when they are the same path, when the row is a
    file inside the artifact's folder (each Hesperian PDF inside `hesperian/`),
    or when the artifact is inside the row's folder."""
    return (rpath == apath or rpath.startswith(apath + "/")
            or apath.startswith(rpath + "/"))


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _title(row, topic, books):
    stem = re.sub(r"\.zim$", "", _norm(row["file"]).rsplit("/", 1)[-1])
    if books and stem in books and books[stem]["title"]:
        return books[stem]["title"]
    blurb = (topic or {}).get("blurb_en") or ""
    if blurb:
        return blurb.split(": ", 1)[0].rstrip(".")
    return row["id"]


# ---------------------------------------------------------------- status

def _commands(win):
    """The commands the pages show, with FORWARD SLASHES ON EVERY SYSTEM.
    `python bin\\ark.py` is right in cmd and PowerShell and broken in Git Bash,
    which reads the backslash as an escape; `python bin/ark.py` works in all
    three, and Python takes either separator. `win` is kept for callers."""
    return {"ark": "python bin/ark.py", "library": "python bin/kiwix-library.py"}


def _actions(row, state, cause, on_disk, c):
    """What the operator can do about a row, as data the pages can draw."""
    rid, rpath = row["id"], row_path(row)
    manual = (row.get("fetch") or "") == "manual"
    get = ([{"label": "Get it from the publisher", "url": row.get("source_page") or ""},
            {"label": "Place it at", "path": rpath},
            {"label": "Then check it", "command": "%s fetch --verify %s" % (c["ark"], rid)}]
           if manual else
           [{"label": "Download it (needs internet once)",
             "command": "%s fetch %s" % (c["ark"], rid)}])
    if state == "grey":
        return get
    if state == "amber":
        # ONE COMMAND SINCE 2026-10-06: `ark.py index --add` writes the scope
        # line, builds the one collection and runs the steps that make it
        # searchable. It needs the graphics card, hence the stop and start.
        return [{"label": "Stop the node (indexing needs the graphics card)",
                 "command": "%s down" % c["ark"]},
                {"label": "Index it so answers can cite it (can take hours)",
                 "command": "%s index --add %s" % (c["ark"], rid)},
                {"label": "Start the node again",
                 "command": "%s up" % c["ark"]}]
    if state == "red":
        if cause == "not_served":
            return [{"label": "Rebuild the Kiwix library", "command": c["library"]}]
        return get
    return []


def row_state(row, artifacts, books, root, exists, isdir, listdir, getsize):
    """(state, cause, reason, indexed, on_disk) for one catalog row."""
    rpath = row_path(row)
    full = os.path.join(root, *rpath.split("/"))
    kind = row.get("kind")
    if kind == "folder":
        try:
            on_disk = isdir(full) and bool(listdir(full))
        except OSError:
            on_disk = False
    else:
        on_disk = exists(full)
    indexed = any(covers(artifact_path(a), rpath) for a in artifacts)
    stem = re.sub(r"\.zim$", "", rpath.rsplit("/", 1)[-1])

    if indexed and not on_disk:
        return ("red", "gone", "In the index but not on the drive: answers can "
                "cite it and the citations cannot open. Copy it back or fetch it "
                "again.", True, False)
    want = _int(row.get("bytes"))
    if on_disk and kind != "folder" and want:
        try:
            have = getsize(full)
        except OSError:
            have = None
        if have is not None and have != want:
            return ("red", "size", "On the drive at %s bytes; the catalog says %s. "
                    "A partial download or a different edition: fetch it again, or "
                    "check it with ark.py fetch --verify." % (
                        "{:,}".format(have), "{:,}".format(want)), indexed, True)
    if on_disk and kind == "zim" and books is not None and stem not in books:
        return ("red", "not_served", "On the drive, but the Kiwix library does not "
                "list it, so it cannot be browsed%s. Rebuild library.xml." % (
                    " and its citations cannot open" if indexed else ""),
                indexed, True)
    if on_disk and indexed:
        return ("green", "", "Searchable and citable.", True, True)
    if on_disk:
        return ("amber", "", ("Browsable in the library, not indexed: answers "
                              "cannot search or cite it." if kind == "zim" else
                              "On the drive, not indexed: answers cannot search "
                              "or cite it."), False, True)
    return ("grey", "", "Not installed.", False, False)


def status(root, catalog, topics, artifacts, books, win=None,
           exists=os.path.exists, isdir=os.path.isdir, listdir=os.listdir,
           getsize=os.path.getsize):
    """The whole library view: rows, a summary by state, and one line per topic.

    `artifacts` is the index's artifact list (store.Store.artifacts.values(), or
    sources.json's). `books` is library_books(); None means the library is
    unknown, and then no row is red for not being served."""
    win = (os.name == "nt") if win is None else win
    c = _commands(win)
    tmap = {t["id"]: t for t in topics}
    artifacts = list(artifacts)
    rows, matched = [], set()
    for r in catalog:
        if not is_corpus(r):
            continue
        t = tmap.get(r["id"], {})
        state, cause, reason, indexed, on_disk = row_state(
            r, artifacts, books, root, exists, isdir, listdir, getsize)
        rpath = row_path(r)
        searchable = (t.get("suggest") or "no") == "yes"
        actions = _actions(r, state, cause, on_disk, c)
        if state == "amber" and t.get("suggest") == "no":
            # DATA, NOT TEXT: the Koppen climate rasters the map reads, a
            # bibliographic catalogue. Present is all they need to be, and
            # telling the operator to index a GeoTIFF would be wrong advice.
            reason = "On the drive. Data, not text to search: nothing to index."
            actions = []
        for i, a in enumerate(artifacts):
            if covers(artifact_path(a), rpath):
                matched.add(i)
        stem = re.sub(r"\.zim$", "", rpath.rsplit("/", 1)[-1])
        rows.append({
            "id": r["id"], "title": _title(r, t, books),
            "shelf": r["shelf"], "file": r["file"], "kind": r["kind"],
            "profile": r.get("profile") or "", "lang": t.get("lang") or "",
            "topics": [x for x in (t.get("topics") or "").split("|") if x],
            "bytes": _int(r.get("bytes")), "license": r.get("license") or "",
            "redistribute": r.get("redistribute") or "",
            "fetch": r.get("fetch") or "", "source_page": r.get("source_page") or "",
            "blurb_en": t.get("blurb_en") or "", "blurb_es": t.get("blurb_es") or "",
            "suggest": searchable,
            "state": state, "cause": cause, "reason": reason,
            "indexed": indexed, "on_disk": on_disk,
            "browse": stem if (r["kind"] == "zim" and on_disk and state != "red") else None,
            "actions": actions,
            "_kw": _keywords(t)})
    # WHAT THE OPERATOR ADDED IS PART OF THE LIBRARY TOO. An indexed artifact no
    # catalog row covers (libretexts on the reference machine, a notebook
    # later) is listed rather than silently missing from the count.
    for i, a in enumerate(artifacts):
        if i in matched:
            continue
        apath = artifact_path(a)
        full = os.path.join(root, *apath.split("/"))
        here = exists(full)
        rows.append({
            "id": "local:" + apath, "title": apath.rsplit("/", 1)[-1],
            "shelf": _norm(a.get("shelf")), "file": _norm(a.get("file")),
            "kind": "zim" if apath.endswith(".zim") else "folder",
            "profile": "local", "lang": a.get("lang") or "", "topics": [],
            "bytes": None, "license": "", "redistribute": "", "fetch": "",
            "source_page": "", "blurb_en": "Added on this node; not in the catalog.",
            "blurb_es": "Añadido en este nodo; no está en el catálogo.",
            "suggest": False,
            "state": "green" if here else "red", "cause": "" if here else "gone",
            "reason": ("Searchable and citable. Added on this node, not in the "
                       "catalog." if here else "In the index but not on the drive."),
            "indexed": True, "on_disk": here, "browse": None, "actions": [],
            "_kw": {"en": [], "es": []}})
    summary = {s: sum(1 for r in rows if r["state"] == s) for s in STATES}
    summary["total"] = len(rows)
    summary["bytes_installed"] = sum(r["bytes"] or 0 for r in rows if r["on_disk"])
    summary["library_known"] = books is not None
    topics_view = []
    for tid, en, es in TOPICS:
        mine = [r for r in rows if tid in r["topics"]]
        counts = {s: sum(1 for r in mine if r["state"] == s) for s in STATES}
        best = min((r["state"] for r in mine), key=_TOPIC_RANK.get, default="grey")
        topics_view.append({"id": tid, "label_en": en, "label_es": es,
                            "state": best, "counts": counts, "total": len(mine)})
    return {"rows": rows, "summary": summary, "topics": topics_view}


def public(view):
    """The view without the matcher's private fields, for the API."""
    return {"summary": view["summary"], "topics": view["topics"],
            "rows": [{k: v for k, v in r.items() if not k.startswith("_")}
                     for r in view["rows"]]}


# ---------------------------------------------------------------- matching

_STOP_EN = frozenset("""a an the of to in on for with and or is are was were be
been how what why when where which who whom do does did can could should would
will i my me we our you your it its this that these those from by at as about
into than then there their they them if not no yes so get got have has had
any some much many very please tell explain""".split())
_STOP_ES = frozenset("""el la los las un una unos unas de del al a en con por para
y o u e es son fue era ser estar esta estan como que porque cuando donde cual
cuales quien quienes hacer hago hace puedo puede pueden se mi mis me yo tu tus su
sus lo le les este esto ese esa eso nos hay muy mas menos sin sobre entre debo
debe dime explica""".split())


def fold(s):
    """Lower case, accents off, anything not a letter or digit to a space."""
    s = unicodedata.normalize("NFKD", (s or "").lower())
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return " ".join(re.sub(r"[^a-z0-9]+", " ", s).split())


def lang_of(q):
    """'es' or 'en', by stopwords. Crude and enough: it only weights matches."""
    toks = fold(q).split()
    es = sum(1 for t in toks if t in _STOP_ES and t not in _STOP_EN)
    en = sum(1 for t in toks if t in _STOP_EN and t not in _STOP_ES)
    return "es" if es > en else "en"


def _keywords(t):
    """{"en": [...], "es": [...]}, folded. Kept apart because the Spanish list
    is only read for a Spanish question: `red` is a Spanish network and an
    English colour, and a rash question must not be offered a book on routers."""
    out = {}
    for lang, col in (("en", "keywords_en"), ("es", "keywords_es")):
        out[lang] = list(dict.fromkeys(
            fold(k) for k in (t.get(col) or "").split("|") if fold(k)))
    return out


# THE ENDINGS A STEM MAY DIFFER BY. Prefix matching with a length allowance
# matched `treat` to `treaty`, `king` to `kingdom` and `plugh` to `plug` once the
# lists grew to fifty words a corpus; an explicit list of endings does not.
_SUFFIXES = ("s", "es", "ed", "d", "ing", "er", "ers")


def _tok_match(t, k):
    """Same word, or the same word with a common ending: burn/burns,
    quemadura/quemaduras, sharpen/sharpening, leak/leaking. Short words must
    match exactly: `ph` is not `phone`."""
    if t == k:
        return True
    if len(t) < 4 or len(k) < 4:
        return False
    if t.startswith(k):
        return t[len(k):] in _SUFFIXES
    if k.startswith(t):
        return k[len(t):] in _SUFFIXES
    return False


def _content(parts):
    return [p for p in parts if p not in _STOP_EN and p not in _STOP_ES]


def keyword_idf(rows):
    """{token: weight} over the keyword lists of the suggestible rows.

    A WORD MANY CORPORA CLAIM SAYS LITTLE ABOUT WHICH ONE TO OFFER. `diarrhea`
    is in twenty lists and `goat` in three, so "my goat has diarrhea" belongs to
    the veterinary wiki, not to the cholera factsheet that happens to be the
    smallest file. Weight is 1 + ln(N / df), the usual inverse document
    frequency, computed once per status()."""
    import math
    rows = [r for r in rows if r.get("suggest")]
    df = {}
    for r in rows:
        toks = set()
        for lang in ("en", "es"):
            for k in r["_kw"].get(lang, []):
                toks.update(_content(k.split()))
        for t in toks:
            df[t] = df.get(t, 0) + 1
    n = max(len(rows), 1)
    out = {t: 1.0 + math.log(n / c) for t, c in df.items()}
    out[""] = 1.0 + math.log(n)      # a word no list claims: the rarest weight
    return out


def score(question, kw, q_lang=None, idf=None):
    """(score, hits). A phrase found whole counts 2, a phrase whose words are
    all present counts 1.5, a single word 1, each multiplied by the keyword's
    weight in `idf` when one is given. `kw` is _keywords()'s dict, or a plain
    list for a caller that has already chosen."""
    q_lang = q_lang or lang_of(question)
    if isinstance(kw, dict):
        keywords = list(kw.get("en", [])) + (list(kw.get("es", [])) if q_lang == "es" else [])
    else:
        keywords = kw
    qf = fold(question)
    toks = _content(qf.split())
    padded = " %s " % qf

    def weight(parts):
        if not idf:
            return 1.0
        ws = [idf.get(p, idf.get("", 1.0)) for p in parts]
        return sum(ws) / len(ws)
    s, hits = 0.0, []
    for k in keywords:
        parts = k.split()
        # A PHRASE MADE ONLY OF STOPWORDS ("who was") WOULD MATCH ANY QUESTION
        # through all() over nothing, so a phrase needs one real word to count.
        cp = _content(parts)
        if not cp:
            continue
        if len(parts) > 1:
            if (" %s " % k) in padded:
                s += 2.0 * weight(cp)
                hits.append(k)
            elif all(any(_tok_match(t, p) for t in toks) for p in cp):
                s += 1.5 * weight(cp)
                hits.append(k)
        elif any(_tok_match(t, k) for t in toks):
            s += 1.0 * weight(cp)
            hits.append(k)
    return s, hits


def _lang_weight(row_lang, q_lang):
    if row_lang == q_lang:
        return 1.25
    if row_lang in ("mul", ""):
        return 1.1
    return 0.85


# ROUGHLY ONE MATCH ON A WORD A DOZEN CORPORA SHARE. Below it, nothing is
# offered rather than something random.
MIN_SCORE = 2.0
# WHEN A CITABLE CORPUS MATCHES THE QUESTION THIS MUCH BETTER, THE LIBRARY
# ALREADY COVERS THE TOPIC, and an unsourced answer is a retrieval miss, not a
# missing book. On the full archive a 12 V battery-bank question was offered
# Money Stack Exchange on the word `bank` while two citable electronics and
# energy corpora matched it far better. Candidates must reach this share of the
# best citable match.
COVERED_RATIO = 0.5
MAX_SUGGEST = 3


def suggest(question, view, k=MAX_SUGGEST, min_score=MIN_SCORE):
    """Up to k corpora that may cover a question the library could not ground.

    Only rows that are NOT already searchable: a green corpus was searched and
    did not answer, so offering it again would be noise. Rows marked
    suggest=no (a bibliographic index, climate rasters) are never offered.

    When nothing matches, the general encyclopedia in the question's language
    is offered as a fallback, if it is not already installed, and marked as a
    fallback so the page can say so honestly."""
    ql = lang_of(question)
    idf = view.get("_idf")
    if idf is None:
        idf = view["_idf"] = keyword_idf(view["rows"])
    cands, covered = [], 0.0
    for r in view["rows"]:
        if not r["suggest"]:
            continue
        s, hits = score(question, r["_kw"], ql, idf)
        if not hits:
            continue
        s *= _lang_weight(r["lang"], ql)
        if r["state"] == "green":
            covered = max(covered, s)
        elif r["state"] in _SUGGEST_RANK:
            cands.append((r, round(s, 2), hits))
    floor = max(min_score, COVERED_RATIO * covered)
    cands = [c for c in cands if c[1] >= floor]
    # TIES GO TO THE CORPUS THAT MATCHED MORE OF THE QUESTION, then to what is
    # already on the drive, then to the smaller download.
    cands.sort(key=lambda x: (-x[1], -len(x[2]), _SUGGEST_RANK[x[0]["state"]],
                              x[0]["bytes"] or 0))
    out = [_suggestion(r, s, hits, False) for r, s, hits in cands[:k]]
    if not out and covered < min_score:
        # THE ENCYCLOPEDIA ONLY: rows whose one topic is `reference`. The World
        # Factbook and Wikisource also carry `reference`, and on the first run
        # against the full archive a chlorine question fell back to Spanish
        # Wikisource and a leaking tap to the Factbook. A fallback is offered
        # because an encyclopedia covers almost anything; those do not. And not
        # at all when a citable corpus already matches the question.
        for r in view["rows"]:
            if (r["topics"] == ["reference"] and r["lang"] == ql and r["suggest"]
                    and r["state"] in _SUGGEST_RANK):
                out.append(_suggestion(r, 0.0, [], True))
                break
    return {"lang": ql, "items": out}


def _suggestion(r, s, hits, fallback):
    return {"id": r["id"], "title": r["title"], "state": r["state"],
            "reason": r["reason"], "blurb_en": r["blurb_en"],
            "blurb_es": r["blurb_es"], "lang": r["lang"], "bytes": r["bytes"],
            "license": r["license"], "redistribute": r["redistribute"],
            "score": s, "matched": hits, "fallback": fallback,
            "browse": r["browse"], "actions": r["actions"]}


# THE GROUNDING STATES THAT EARN A SUGGESTION (answer.grounding()). A grounded
# answer, with or without an added block, stands on the library; offering a
# download beside it would read as doubt the citations do not deserve.
SUGGEST_WHEN = frozenset({"unsourced", "uncovered", "model_only"})


def wants_suggestion(grounding_state, n_results):
    return n_results == 0 or grounding_state in SUGGEST_WHEN


# ---------------------------------------------------------------- selftest

def selftest():
    """States on a fake drive, the catalog/topics agreement, and the matcher
    against the real topics.csv. Needs no archive and no index, so CI runs it."""
    bad = 0
    ran = [0]

    def check(name, got, want):
        nonlocal bad
        ran[0] += 1
        if got == want:
            print("  ok    corpus %s" % name)
        else:
            bad += 1
            print("  FAIL  corpus %s: got %r, want %r" % (name, got, want))

    cat = [
        {"id": "z-green", "shelf": "07-x", "file": "g.zim", "kind": "zim", "bytes": "10", "fetch": "direct"},
        {"id": "z-amber", "shelf": "07-x", "file": "a.zim", "kind": "zim", "bytes": "10", "fetch": "direct"},
        {"id": "z-grey", "shelf": "07-x", "file": "n.zim", "kind": "zim", "bytes": "10", "fetch": "direct"},
        {"id": "z-gone", "shelf": "07-x", "file": "gone.zim", "kind": "zim", "bytes": "10", "fetch": "direct"},
        {"id": "z-short", "shelf": "07-x", "file": "s.zim", "kind": "zim", "bytes": "10", "fetch": "direct"},
        {"id": "z-unserved", "shelf": "07-x", "file": "u.zim", "kind": "zim", "bytes": "10", "fetch": "direct"},
        {"id": "p-in-folder", "shelf": "07-x", "file": "books/p.pdf", "kind": "pdf", "bytes": "5", "fetch": "manual",
         "source_page": "https://publisher.example/p"},
        {"id": "f-amber", "shelf": "03-y", "file": "texts/", "kind": "folder", "bytes": "99", "fetch": "manual"},
        {"id": "f-empty", "shelf": "03-y", "file": "empty/", "kind": "folder", "bytes": "99", "fetch": "manual"},
        {"id": "f-data", "shelf": "08-m", "file": "rasters/", "kind": "folder", "bytes": "9", "fetch": "manual"},
        {"id": "model", "shelf": "01-models", "file": "m.gguf", "kind": "gguf", "bytes": "1"},
        {"id": "sw", "shelf": "09-software", "file": "tool/", "kind": "folder", "bytes": "1"},
    ]
    top = [{"id": "z-grey", "topics": "water|medicine", "lang": "en", "suggest": "yes",
            "keywords_en": "safe water|chlorine", "keywords_es": "agua potable|cloro",
            "blurb_en": "Water: a test corpus."},
           {"id": "f-data", "topics": "geography", "lang": "mul", "suggest": "no",
            "blurb_en": "Rasters: data."}]
    files = {"R/07-x/g.zim": 10, "R/07-x/a.zim": 10, "R/07-x/s.zim": 4,
             "R/07-x/u.zim": 10, "R/07-x/books/p.pdf": 5}
    dirs = {"R/03-y/texts": ["t.pdf"], "R/03-y/empty": [], "R/07-x/books": ["p.pdf"],
            "R/08-m/rasters": ["a.tif"]}

    def j(p):
        return p.replace("\\", "/")
    arts = [{"shelf": "07-x", "file": "g.zim"}, {"shelf": "07-x", "file": "gone.zim"},
            {"shelf": "07-x/books", "file": "books"}, {"shelf": "07-x", "file": "u.zim"},
            {"shelf": "02-z/extra", "file": "extra"}]
    books = {"g": {"title": "Green Book", "lang": "eng"}, "a": {"title": "", "lang": "eng"},
             "s": {"title": "", "lang": "eng"}, "gone": {"title": "", "lang": "eng"}}
    v = status("R", cat, top, arts, books, win=True,
               exists=lambda p: j(p) in files or j(p) in dirs,
               isdir=lambda p: j(p) in dirs, listdir=lambda p: dirs[j(p)],
               getsize=lambda p: files[j(p)])
    st = {r["id"]: r["state"] for r in v["rows"]}
    check("only corpus rows, plus the local addition",
          sorted(st), sorted(["z-green", "z-amber", "z-grey", "z-gone", "z-short",
                              "z-unserved", "p-in-folder", "f-amber", "f-empty",
                              "f-data", "local:02-z/extra"]))
    check("indexed and on the drive is green", st["z-green"], "green")
    check("on the drive, not indexed, is amber", st["z-amber"], "amber")
    check("not on the drive is grey, not red", st["z-grey"], "grey")
    check("indexed but gone is red", st["z-gone"], "red")
    check("a short file is red", st["z-short"], "red")
    check("indexed but not served is red", st["z-unserved"], "red")
    check("a PDF inside an indexed folder is green", st["p-in-folder"], "green")
    check("a folder with files, not indexed, is amber", st["f-amber"], "amber")
    check("an empty folder is not installed", st["f-empty"], "grey")
    check("an indexed artifact on no catalog row is listed (and here it is gone)",
          st["local:02-z/extra"], "red")
    check("summary counts every state",
          {s: v["summary"][s] for s in STATES}, {"green": 2, "amber": 3, "red": 4, "grey": 2})
    rows = {r["id"]: r for r in v["rows"]}
    check("the title comes from the Kiwix library", rows["z-green"]["title"], "Green Book")
    check("else from the blurb", rows["z-grey"]["title"], "Water")
    check("a grey direct row shows the fetch command, forward slashes everywhere",
          rows["z-grey"]["actions"][0]["command"], "python bin/ark.py fetch z-grey")
    check("a manual row points to the publisher",
          [a.get("url") for a in rows["f-empty"]["actions"]][:1], [""])
    check("an unserved ZIM is repaired by rebuilding the library",
          rows["z-unserved"]["actions"][0]["command"], "python bin/kiwix-library.py")
    check("an amber row is indexed with one command between a stop and a start",
          [x["command"] for x in rows["z-amber"]["actions"]],
          ["python bin/ark.py down", "python bin/ark.py index --add z-amber",
           "python bin/ark.py up"])
    check("a data row is never told to index itself",
          (rows["f-data"]["state"], rows["f-data"]["actions"]), ("amber", []))
    check("a red or amber ZIM offers no browse link unless it is served",
          (rows["z-amber"]["browse"], rows["z-unserved"]["browse"]), ("a", None))
    t = {x["id"]: x for x in v["topics"]}
    check("a topic with only a grey corpus is grey", t["water"]["state"], "grey")
    check("every topic is listed", len(v["topics"]), len(TOPICS))
    v2 = status("R", cat, top, arts, None, win=False,
                exists=lambda p: j(p) in files or j(p) in dirs,
                isdir=lambda p: j(p) in dirs, listdir=lambda p: dirs[j(p)],
                getsize=lambda p: files[j(p)])
    st2 = {r["id"]: r["state"] for r in v2["rows"]}
    check("an unknown library turns nothing red for not being served",
          st2["z-unserved"], "green")
    check("the private matcher fields stay out of the API",
          any(k.startswith("_") for r in public(v)["rows"] for k in r), False)

    check("fold drops accents and punctuation", fold("¿Cómo DESINFECTAR el agua?"),
          "como desinfectar el agua")
    check("Spanish is detected", lang_of("¿Cómo puedo tratar una quemadura en la mano?"), "es")
    check("English is detected", lang_of("How do I treat a burn on the hand?"), "en")
    check("a short keyword does not match a longer word", _tok_match("phone", "ph"), False)
    check("plural matches singular", _tok_match("burns", "burn"), True)
    check("a grounded answer earns no suggestion", wants_suggestion("grounded", 5), False)
    check("an unsourced one does", wants_suggestion("unsourced", 5), True)
    check("no results at all does", wants_suggestion(None, 0), True)

    # THE MATCHER AGAINST THE REAL topics.csv, with every corpus not installed.
    # These are the questions the feature exists for; if one stops matching,
    # a keyword was lost or the scoring changed.
    try:
        real_cat, real_top = load_csv(CATALOG), load_csv(TOPICS_CSV)
    except OSError as e:
        bad += 1
        print("  FAIL  corpus catalog or topics unreadable: %s" % e)
        return bad
    check("every topics row uses known topics",
          sorted({x for t in real_top for x in t["topics"].split("|") if x}
                 - set(TOPIC_IDS)), [])
    corpus_ids = {r["id"] for r in real_cat if is_corpus(r)}
    check("topics.csv and the catalog name the same corpora",
          (sorted(corpus_ids - {t["id"] for t in real_top}),
           sorted({t["id"] for t in real_top} - corpus_ids)), ([], []))
    empty = status("NOWHERE", real_cat, real_top, [], {}, win=False,
                   exists=lambda p: False, isdir=lambda p: False,
                   listdir=lambda p: [], getsize=lambda p: 0)
    check("an empty drive has no red rows", empty["summary"]["red"], 0)

    def first(q):
        items = suggest(q, empty)["items"]
        return [i["id"] for i in items]
    for q, want_any, why in [
            ("How do I make water safe to drink with chlorine?",
             {"zimgit-water_en_2024-08", "ehb_water_en_2016_web"}, "water, English"),
            ("¿Cómo desinfectar el agua para beber con cloro?",
             {"ehb_water_es"}, "water, Spanish"),
            ("¿Cómo tratar una quemadura en un niño?",
             {"es_wtnd_2017_full"}, "burn, Spanish"),
            ("My arduino servo jitters when I use pwm",
             {"arduino.stackexchange.com_en_all_2026-07"}, "Arduino"),
            ("How do I replace a cracked phone screen?",
             {"ifixit_en_all_2025-12"}, "repair"),
            ("What should be in a bug out bag for a power outage?",
             {"trueprepper.com_en_all_2026-05"}, "prepping"),
            ("My goat has a parasite, what vaccination does it need?",
             {"wikivet.net_en_all_maxi_2026-07a"}, "veterinary"),
            ("How do I size a solar panel and charge controller for off grid?",
             {"energypedia_en_all_maxi_2026-06"}, "solar")]:
        got = first(q)
        check("suggests %s" % why, bool(want_any & set(got)) or got, True)
    check("a question about a king goes to the encyclopedia, not the taxonomy",
          [i["id"] for i in suggest("Who was the third king of Bhutan?", empty)["items"]][:1],
          ["wikipedia_en_all_maxi_2026-02"])
    check("a Spanish keyword is not read for an English question (red is a colour)",
          score("I have a red rash on my arm", {"en": [], "es": ["red"]}, "en")[0], 0.0)
    check("a stem needs a real ending: treat is not treaty, sharpen is sharpening",
          (_tok_match("treat", "treaty"), _tok_match("sharpen", "sharpening")), (False, True))
    check("an integral of x squared goes to mathematics, not woodworking",
          [i["id"] for i in suggest("What is the integral of x squared?", empty)["items"]][0]
          in ("math.stackexchange.com_en_all_2026-08", "openstax-04"), True)
    check("the veterinary wiki wins a goat question over the cholera factsheet",
          [i["id"] for i in suggest("My goat has diarrhea", empty)["items"]][:1],
          ["wikivet.net_en_all_maxi_2026-07a"])
    nothing = suggest("Qwrtz vbnmk?", empty)
    check("an unmatched English question falls back to the encyclopedia",
          [(i["id"], i["fallback"]) for i in nothing["items"]],
          [("wikipedia_en_all_maxi_2026-02", True)])
    nothing_es = suggest("¿Qué es el qwrtz de la vbnmk?", empty)
    check("and a Spanish one to the Spanish encyclopedia",
          [i["id"] for i in nothing_es["items"]][:1]
          in (["wikipedia_es_all_maxi_2026-05"], []) and True, True)
    # THE FULL-ARCHIVE CASE (2026-10-06): water and medicine are citable, so a
    # chlorine question that went unsourced must not be handed a book of poems.
    water_green = {"rows": [dict(r, state="green") if r["state"] == "grey"
                            and ("water" in r["topics"] or r["topics"] == ["reference"])
                            else r for r in empty["rows"]]}
    check("with water and the encyclopedias citable, a chlorine question gets nothing",
          suggest("¿Cómo desinfectar el agua con cloro?", water_green)["items"], [])
    check("never more than %d suggestions" % MAX_SUGGEST,
          max(len(suggest(q, empty)["items"]) for q in
              ["fever infection wound burn fracture diarrhea dose antibiotic"]) <= MAX_SUGGEST,
          True)
    print("%d/%d" % (ran[0] - bad, ran[0]))
    return bad


if __name__ == "__main__":
    import sys
    sys.exit(1 if selftest() else 0)
