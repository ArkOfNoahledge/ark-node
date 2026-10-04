#!/usr/bin/env python3
"""
index-bm25.py - the keyword half of retrieval. No GPU, no new dependency.

    python bin/index-bm25.py --build          # ~minutes, from the .jsonl files
    python bin/index-bm25.py --check "flood water"

WHY THIS EXISTS - and it is not a theoretical improvement.

The pass-1 acceptance test asked:

    "how do I make flood water safe to drink"   -> best score 0.522
                                                   watermelons, food microbiology,
                                                   foodborne illness outbreaks

    "how do I purify water after a flood"       -> best score 0.673
                                                   purification tablets, two-stage
                                                   filtering, "Purifying Water
                                                   During an Emergency"

Same index, same corpus, one rewording. Every hit in the failing case was about
FOOD. Dense retrieval matches meaning-shaped neighbourhoods, and it has no notion
that "flood" and "food" are different words - it has no notion of words at all.
BM25 does. It would rank a document containing the literal token "flood" and could
not return a watermelon question.

This is spec 7.5's hybrid retrieval, and the failing query is the argument for it.

WHY SQLITE FTS5 RATHER THAN A LIBRARY. sqlite3 ships with Python. There is no
wheel to vendor, nothing to rebuild on the node from printed instructions, and the
index lives on disk rather than in RAM - which matters when the dense half already
demands its whole HNSW graph be resident. rank_bm25 would hold ~2 million
documents in memory to do the same job.
"""

import os, sys, json, glob, re, time, sqlite3, argparse

# NO .pyc BESIDE THE SOURCE. This module is IMPORTED by index-query.py and by
# the node's ark-api/store.py, not just run, so Python writes __pycache__ into
# bin/ - a directory under a checksum manifest (R7). Three .pyc files had
# accumulated there by 2026-09-04, one of them from a different interpreter
# version, each one a manifest change that reads as a modified tool.
import sys
sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(ROOT, "10-index")
CHUNKS = os.path.join(OUT, "chunks")
DB = os.path.join(OUT, "bm25.sqlite3")

_WORD = re.compile(r"[^\w]+", re.UNICODE)

# FUNCTION WORDS ARE REMOVED FROM THE QUERY, AND THIS IS NOT A REFINEMENT.
#
# The first hybrid run asked "what do I do for someone in shock" and BM25 returned,
# in the top five, a page about female genital cutting and a page about
# cotrimoxazole dosing. Both matched on `what`, `do`, `for`, `someone`, `in`. The
# original filter only dropped single characters, so every function word in a
# conversational question entered the OR query, and BM25's IDF weighting was not
# enough to keep documents that matched ONLY those words out of the result set.
#
# On a medical question that is the least acceptable place for noise, and an
# operator reading a retrieval result has no way to tell that hit three arrived
# because of the word "someone".
_STOP = set("""
a an and are as at be been but by can could do does did for from get got had has
have how i if in into is it its me my no not of on or our so that the their them
then there these they this to us was we were what when where which who why will
with would you your
someone something anyone anything everyone everything nothing should must might
shall being doing done very more most some any each other such only same too just
also still even about after before
al como con de del donde el ella en es esta este hacer hay la las le lo los mas me
mi mucho muy no o para por que se si sin su sus tan te un una uno unos y ya
alguien algo alguno cual cuando donde quien todo todos poco mucha muchos
""".split())

# WORDS DELIBERATELY LEFT IN, because in THIS corpus they are content:
#   well    a water well, not a filler word
#   can     a jerry can, a fuel can
#   may     retained rather than risk a month or a modal reading
#   make    "make water safe" is the instruction being searched for
# A stopword list copied from a general-purpose search engine would strip the
# first two, and a query about drawing water from a well would stop working.


def query_terms(q, max_terms=24):
    """FTS5 has its own query syntax; a raw question is not valid input to it.
    Content words are extracted, quoted, and joined with OR so BM25 ranks by how
    many of the rarer terms a document contains rather than requiring all of them.

    Falls back to the unfiltered words if a query is nothing but function words,
    because returning nothing at all is worse than returning noise."""
    raw = [w for w in _WORD.split(q.lower()) if len(w) > 1]
    ws = [w for w in raw if w not in _STOP][:max_terms]
    if not ws:
        ws = raw[:max_terms]
    if not ws:
        return None
    return " OR ".join('"%s"' % w.replace('"', '') for w in ws)


def content_words(q, max_terms=24):
    """The words `query_terms` would search for, in order, before they are joined.

    Exposed so a caller can classify them - `store.keyword()` asks the gazetteer
    which of them name places - without a second copy of the tokenizer or the
    stoplist. Two copies of a hand-tuned stoplist is the drift this file's
    coupling note in store.py exists to avoid."""
    raw = [w for w in _WORD.split(q.lower()) if len(w) > 1]
    ws = [w for w in raw if w not in _STOP][:max_terms]
    return ws or raw[:max_terms]


FOLLOWUP_CARRY = 8

# HOW LITTLE A QUESTION MUST SAY BEFORE IT IS RETRIEVED ON THE THREAD.
#
# Measured 2026-09-09 over the archive's own 83,856 Stack Exchange question
# titles, which are all standalone, so any firing there is a false positive:
#
#     content words   share of real standalone questions
#          <= 1                     0.26%   (222)
#          <= 2                     3.85%   (3,231)
#          <= 3                    15.17%   (12,721)
#
# `And the dose?`, `y la dosis?` and `what about children` all carry exactly ONE
# content word. `replacing a laptop screen` carries THREE. So 2 catches the
# follow-up shapes at a 3.85% cost and 3 would break an ordinary short question.
#
# UNGATED EXPANSION IS DESTRUCTIVE, AND WORSE IN THE SEMANTIC HALF. Measured on
# the node, 16 standalone queries with an unrelated thread carried in, share of
# result slots still on the original topic:
#
#                    baseline   no gate   gate <= 2
#      hybrid          100%       45%        91%
#      keyword         100%       72%        94%
#
# **Dense loses more than keyword, not less**, which is the opposite of the
# expectation: appending unrelated words moves the whole embedding, while BM25
# at least keeps the original terms scoring. `replacing a laptop screen` reached
# ZERO of five on topic ungated.
#
# AND THE FAILURE IS MORE DANGEROUS ON THE DENSE SIDE. `And the dose?` with no
# context returns, in keyword, two obvious pieces of junk; in hybrid it returns
# five confident figure-bearing passages - `Dose (biochemistry)` twice and
# `COVID-19 vaccination in the United States`. Plausible, cited, wrong, and
# carrying numbers. That is §11.2's named failure mode, so the gate protects
# more than tidiness.
#
# IT IS NOT A FOLLOW-UP DETECTOR AND THE DIFFERENCE MATTERS. It does not ask
# whether the operator meant a follow-up; it asks whether the question carries
# enough content to be retrieved on its own. That is the actual mechanism - a
# follow-up is unretrievable because it is nearly contentless - and it needs no
# list of discourse markers in two languages. If two-word follow-ups
# (`And the adult dose?`) prove to need covering, a leading-marker rule is the
# next increment and is measurable against the same negative set.
FOLLOWUP_MAX_CONTENT = 2


def expand_followup(q, prev_qs, carry=FOLLOWUP_CARRY):
    """The retrieval string for a follow-up: its own words, then the earlier
    question's, most recent first. Returns None when there is nothing to add.

    WHY THE RETRIEVAL QUERY AND THE QUESTION MUST DIVERGE. `And the dose?` has
    one content word. Keyword search over it returns nothing useful and bge-m3
    embeds it as a near-contentless vector, so the surface answers a follow-up
    as though it were a new question - which is what the public site's own Uses
    panel depicts a nurse doing, and what the node could not do until this
    existed. Searching `dose child fever stiff neck` finds the dose; the
    QUESTION put to the model stays `And the dose?`, because that is what was
    asked and the answer has to read as a reply to it.

    IT NEEDS NO MODEL, AND THAT IS THE POINT. §3.4 says nothing gating retrieval
    may require a component that can be absent. A model rewriting the follow-up
    into a standalone question would be better and would also mean follow-up
    retrieval fails on a node whose primary is down, while single-turn retrieval
    keeps working. So the deterministic form is the floor and any model rewrite
    is an improvement layered over it.

    DUPLICATES ARE DROPPED AND ORDER IS KEPT. FTS5 scores a repeated term twice,
    so `dose dose child` would weight `dose` against itself; and the follow-up's
    own words come first because they are what was actually asked.

    IT DECLINES WHEN THE QUESTION CAN STAND ALONE. See `FOLLOWUP_MAX_CONTENT`
    above for the measurement; a question with more than that many content words
    is retrieved on its own and the thread is ignored, so an operator who forgets
    to clear the thread before changing subject is not punished for it.

    `carry` bounds how many earlier content words are added. Unbounded, a long
    triage thread would eventually search for everything anyone had mentioned,
    which is the topic-drift failure the surface's visible clear control exists
    to let the operator break."""
    own = content_words(q)
    if len(own) > FOLLOWUP_MAX_CONTENT:
        return None                  # it can be retrieved on its own words
    seen = set(own)
    extra = []
    for pq in prev_qs or ():
        for w in content_words(pq):
            if w not in seen:
                seen.add(w)
                extra.append(w)
                if len(extra) >= carry:
                    break
        if len(extra) >= carry:
            break
    if not extra:
        return None
    return " ".join(own + extra)


def query_scoped(topic, place):
    """`(topic OR topic) AND (place OR place)` - the topic qualified by where.

    WHY THIS EXISTS, MEASURED 2026-09-08. `query_terms` joins with OR so BM25
    ranks by how many of the rarer terms a chunk carries, and that is exactly
    what it does: for *groundwater in Nevada and Utah* the top TWELVE results
    contained `groundwater` ZERO times, because in a medical corpus `nevada`
    (1,665 chunks) and `utah` (2,313) are RARER than `groundwater` (3,510). The
    ranking was right and the premise was wrong. **Rarity is not relevance**,
    and IDF cannot know which term is the subject and which are qualifiers.

    COUNTING TERMS DOES NOT FIX IT. Every one of those forty results matched
    exactly two of the three terms, so a coverage-first re-rank - the first fix
    proposed, and wrong - would have reordered nothing. `nevada AND utah` and
    `groundwater AND nevada` are both two of three. The roles differ, not the
    counts.

    PLAIN `AND` IS NOT AVAILABLE EITHER: zero chunks match all three terms.
    Hence OR within each group and AND between them - which yields 15 candidates
    for that query, every one containing the topic.

    The caller decides which words are places, and only calls this when the
    question has BOTH kinds. A question that is all topic keeps `query_terms`
    unchanged, because this rule was measured on the case where a place
    qualifies a subject and nowhere else."""
    if not topic or not place:
        return None
    def grp(ws):
        return "(%s)" % " OR ".join('"%s"' % w.replace('"', '') for w in ws)
    return "%s AND %s" % (grp(topic), grp(place))


def load_exclusions():
    f = os.path.join(OUT, "excluded-ids.txt")
    if not os.path.exists(f):
        return set()
    out = set()
    for line in open(f, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#"):
            out.add(line)
    return out


def stamp():
    """What the keyword store was built from: each chunk file's name and size,
    and the exclusion list's size. No mtime, for the reason index-pq-build.py
    gives: a cold-copy restore rewrites every mtime and changes no byte, and
    this store takes two hours to rebuild (7,089 s at 39,073,563 rows).
    `ark.py index` computes the same thing to decide whether a rebuild is due;
    the two must stay in step, and its selftest runs this file to check."""
    ef = os.path.join(OUT, "excluded-ids.txt")
    return {"chunks": [[os.path.basename(f), os.path.getsize(f)] for f in
                       sorted(glob.glob(os.path.join(CHUNKS, "*.jsonl")))],
            "excluded": os.path.getsize(ef) if os.path.exists(ef) else None}


def build():
    """BUILT BESIDE THE OLD STORE AND MOVED INTO PLACE, 2026-09-30. It used to
    drop the table inside the live 109 GB file with the journal off, so an
    interrupted build left no keyword index at all. Now it writes
    bm25.sqlite3.building and replaces the old file only when it is complete,
    with its stamp inside. The cost is room for both, about 110 GB at 39M
    chunks; `ark.py index` checks for it first."""
    if not sqlite3.sqlite_version_info >= (3, 9):
        sys.exit("sqlite too old for FTS5")
    work = DB + ".building"
    if os.path.exists(work):
        os.remove(work)
    con = sqlite3.connect(work)
    try:
        con.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
        con.execute("DROP TABLE t")
    except sqlite3.OperationalError:
        sys.exit("this Python's sqlite3 was built without FTS5 - "
                 "report this, it changes the retrieval plan")

    excl = load_exclusions()
    if excl:
        print("exclusion list: %s ids skipped" % "{:,}".format(len(excl)))

    con.execute("PRAGMA journal_mode=OFF")
    con.execute("PRAGMA synchronous=OFF")
    con.execute("DROP TABLE IF EXISTS chunks")
    # cid is UNINDEXED: it is a key to look up in Chroma, not something to search.
    con.execute("CREATE VIRTUAL TABLE chunks USING fts5(cid UNINDEXED, text, tokenize='unicode61')")

    files = sorted(glob.glob(os.path.join(CHUNKS, "*.jsonl")))
    if not files:
        sys.exit("no chunks found - run the build first")
    t0 = time.time()
    n = 0

    def rows(f):
        nonlocal n
        with open(f, encoding="utf-8") as fh:
            for line in fh:
                m = json.loads(line)
                if m["id"] in excl:
                    continue
                n += 1
                yield (m["id"], m["text"])

    for f in files:
        before = n
        con.executemany("INSERT INTO chunks(cid, text) VALUES (?,?)", rows(f))
        con.commit()
        print("  %-52s %s rows" % (os.path.basename(f)[:-6][:52], "{:,}".format(n - before)))

    con.execute("INSERT INTO chunks(chunks) VALUES('optimize')")
    st = stamp()
    st["rows"] = n
    st["built"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    con.execute("CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT)")
    con.execute("INSERT INTO meta VALUES ('stamp', ?)", (json.dumps(st),))
    con.commit()
    con.close()
    os.replace(work, DB)
    print("\n%s rows in %.0f s -> %s (%.1f GB)"
          % ("{:,}".format(n), time.time() - t0, DB, os.path.getsize(DB) / 1024.0 ** 3))


def check(q, k=5):
    con = sqlite3.connect(DB)
    m = query_terms(q)
    rows = con.execute(
        "SELECT cid, bm25(chunks), snippet(chunks, 1, '[', ']', '...', 18) "
        "FROM chunks WHERE chunks MATCH ? ORDER BY bm25(chunks) LIMIT ?",
        (m, k)).fetchall()
    print("BM25: %s\n" % q)
    for cid, score, snip in rows:
        print(" %8.3f  %-16s %s" % (score, cid, re.sub(r"\s+", " ", snip)[:150]))
    if not rows:
        print("  no matches")
    con.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", action="store_true")
    ap.add_argument("--check", nargs="*")
    a = ap.parse_args()
    if a.build:
        build()
    elif a.check:
        check(" ".join(a.check))
    else:
        sys.exit("--build or --check QUERY")
