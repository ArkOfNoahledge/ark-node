#!/usr/bin/env python3
"""
serve.py - the node's HTTP layer. Standard library only.

    python 13-ark-node/ark-api/serve.py                  # 0.0.0.0:8090
    python 13-ark-node/ark-api/serve.py --port 8090 --host 127.0.0.1
    python 13-ark-node/ark-api/serve.py --selftest       # no server, exits non-zero on failure

Routes:
    GET  /                       the three-pane shell (ark-web/index.html)
    GET  /api/health             what is loaded, what is degraded, and says so
    GET  /api/search?q=..&n=..&mode=hybrid|keyword|dense
    GET  /api/passage?cid=..     one passage in full, with its citation
    GET  /api/spatial?q=..       does this question name a place, and which one
    GET  /files/<archive path>   the four PDF artifacts nothing else serves
    GET  /map/                   the map viewer, served from 09-software

WHAT THIS IS AND IS NOT. It is the orchestration layer NODE-ARCHITECTURE §6 asks
for: thin, over services that already work, and disposable. If it dies, kiwix-serve
still serves the archive and bin/index-query.py still answers from the command
line. The node degrades to what it was this morning rather than to nothing. That
property is worth more than any feature in it.

THE ANSWER PANE IS NOT HERE YET, ON PURPOSE. The source pane needs no model, runs
on the index that already exists, and is what makes §9.2 - verify a
safety-consequential answer against the archive - possible at all. Building the
answer first would have meant building it on a citation path that did not exist,
which is precisely the failure mode §11.1 describes: fluent, confident, and
untraceable.
"""

import argparse
import hashlib
import http.server
import json
import mimetypes
import os
import re
import socketserver
import sys
import time
import urllib.parse
# EXPLICIT, THOUGH IT WOULD HAVE WORKED WITHOUT. `import urllib.parse`
# binds only that submodule; `urllib.request` resolves here purely because
# llm.py imports it and Python caches submodules on the package. That is a
# dependency on another module's import list, invisible to pyflakes and
# broken by a refactor nobody would connect to this file.
import urllib.request
import urllib.error

# NO .pyc BESIDE THE SOURCE. This tree is checksummed by rehash.sh per operating
# rule R7, and a __pycache__ directory appearing on first run would change the
# manifest every time the node started - turning the archive's integrity signal
# into noise, which is exactly the reason PUSH.md keeps .git out of here.
sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import store                                                   # noqa: E402
import safety                                                  # noqa: E402
import llm                                                     # noqa: E402
import answer as answerlib                                     # noqa: E402
import spatial                                                 # noqa: E402
import corpus as corpuslib                                     # noqa: E402
import threading                                               # noqa: E402
import uuid                                                    # noqa: E402
import importlib.util                                          # noqa: E402

WEB = os.path.join(os.path.dirname(HERE), "ark-web")

# THE MAP VIEWER IS SERVED FROM HERE SO THE OPERATOR STARTS ONE FEWER SERVER.
# 09-software/map-viewer/ has worked since 2026-08-31 and needed a static server
# of its own on :8080 - which is kiwix-serve's port. An operator following the
# manual and then the map-viewer README would have started kiwix, then hit
# "address already in use" and had no idea which of the two was wrong.
# Serving it from this process removes the fourth server AND the collision.
MAPDIR = None       # resolved at startup, once store.ROOT is known

# .mjs IS NOT OPTIONAL. MapLibre 6 ships ESM only; a module served as
# text/plain or octet-stream fails the import with a MIME error that reads like
# a broken file rather than a wrong header. .pbf carries the glyphs.
mimetypes.add_type("application/javascript", ".mjs")
# .js TOO, SINCE 2026-09-27 (services.js). On Windows `mimetypes` reads the
# registry, and a machine where some installer mapped .js to text/plain would
# serve the Services menu's script as text. Browsers run it today without
# nosniff, but a header that is right costs nothing and one that is wrong
# fails on the machine nobody tested.
mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("application/x-protobuf", ".pbf")
mimetypes.add_type("image/webp", ".webp")
STARTED = time.time()
S = None            # the Store, built once at startup
NODE_PORT = 8090    # set from --port at startup

# WHAT THIS KIT DOES NOT INSTALL, FROM bin/ark.py (2026-10-04). The starter kit
# has no maps and no cross-check model, and on the 2026-10-03 new-account test
# the services page drew both red and Node status counted them as degraded on a
# correct install: a stranger's first screen saying "broken". A part that was
# never installed is not a fault. Only ark.py knows the kit (ark.toml and what
# is on disk), so it says; a node started by hand gets the old, strict report.
NOT_INSTALLED = frozenset(
    x.strip() for x in os.environ.get("ARK_NOT_INSTALLED", "").split(",")
    if x.strip())
# THE MODEL CARDS ARE NOT ALWAYS QWEN AND GEMMA. The 8gb profile answers with
# Phi-4-mini; the page asks the node, and the node asks ark.toml through ark.py.
MODEL_NAMES = {"primary": os.environ.get("ARK_PRIMARY_NAME") or None,
               "crosscheck": os.environ.get("ARK_CROSSCHECK_NAME") or None}


def models_view(status, off=None):
    """llm.status() with each role's display name and whether it is installed.
    Copies: llm caches the dicts it hands out."""
    off = NOT_INSTALLED if off is None else off
    out = {}
    for role, x in status.items():
        x = dict(x)
        x["name"] = MODEL_NAMES.get(role)
        x["installed"] = role not in off
        out[role] = x
    return out


def kit_faults(models, gazetteer_ok, gazetteer_error, off=None):
    """The model and map lines of `degraded`, leaving out what is not installed."""
    off = NOT_INSTALLED if off is None else off
    out = ["%s model not running" % r for r in ("primary", "crosscheck")
           if r not in off and not (models.get(r) or {}).get("ok")]
    if not gazetteer_ok and "map" not in off:
        out.append("no gazetteer: " + (gazetteer_error or "not built"))
    return out


# THE PAGES ASK THE NODE WHICH PORTS TO LINK TO, SINCE 2026-09-28.
# ark.toml can move any server to another port, and until this existed the
# pages would have kept linking the library at :8080 and fetching tiles from
# :8081 whatever the node had been told - a Services card pointing at nothing,
# a map that could not draw, and a diagnosis naming the wrong port. The node
# already knows three of them (the Kiwix base it builds citations with and the
# two model URLs it calls), is told the fourth by ARK_TILES_PORT, and knows its
# own. With nothing set, every value is the old default and the pages behave
# exactly as before.
DEFAULT_PORTS = {"archive": 8080, "tiles": 8081, "node": 8090,
                 "primary": 8091, "crosscheck": 8092}


# THE CORPUS VIEW (2026-10-06, PLAN-corpus-status.md). What the library holds,
# what answers can cite, and what could be added: corpus.py does the work, this
# keeps one copy and refreshes it. Thirty seconds, or at once when library.xml
# changes, because a fetch or a library rebuild while the node runs should show
# within a page refresh, and 107 stat calls plus one XML parse is cheap but not
# free on every health poll from every open tab.
CORPUS_TTL = 30.0
_CORPUS = {"at": 0.0, "key": None, "view": None}
_CORPUS_LOCK = threading.Lock()


def _library_path():
    """The library kiwix-serve was confirmed against, else <archive>/library.xml."""
    lib = getattr(S, "books_library", None) if S is not None else None
    if lib and os.path.exists(lib):
        return lib
    return os.path.join(store.ROOT, "library.xml")


def corpus_view(force=False):
    """corpus.status() for this node, cached. Works with no index at all (a
    clone before setup, or --unit): every row is then grey or amber."""
    lib = _library_path()
    try:
        key = os.path.getmtime(lib)
    except OSError:
        key = None
    with _CORPUS_LOCK:
        c = _CORPUS
        if (not force and c["view"] is not None and c["key"] == key
                and time.time() - c["at"] < CORPUS_TTL):
            return c["view"]
        arts = list(S.artifacts.values()) if S is not None else []
        v = corpuslib.status(store.ROOT, corpuslib.load_csv(corpuslib.CATALOG),
                             corpuslib.load_csv(corpuslib.TOPICS_CSV), arts,
                             corpuslib.library_books(lib))
        c.update(at=time.time(), key=key, view=v)
        return v


def suggestion_for(q, grounding_state, n_results):
    """The "not in your library yet" card for an answer, or None.

    ONLY WHEN THE LIBRARY DID NOT GROUND THE ANSWER (corpus.SUGGEST_WHEN), or
    found nothing at all. Never for a grounded answer: offering a download
    beside cited passages would read as doubt they do not deserve. Shown beside
    the answer and never inside it, so the grounding badge keeps one meaning;
    and it says "may cover", never "contains". A failure here is reported in
    the payload and never breaks the answer."""
    if not corpuslib.wants_suggestion(grounding_state, n_results):
        return None
    try:
        out = corpuslib.suggest(q, corpus_view())
    except Exception as e:                       # noqa: BLE001 - reported
        return {"error": "%s: %s" % (type(e).__name__, e), "items": []}
    if not out["items"]:
        return None
    out["why"] = grounding_state or "no_results"
    return out


def corpus_summary():
    """The four counts for /api/health, or the reason there are none. A broken
    topics file must never take the health report down with it."""
    try:
        return dict(corpus_view()["summary"])
    except Exception as e:                       # noqa: BLE001 - reported
        return {"error": "%s: %s" % (type(e).__name__, e)}


def _port_of(url, default):
    try:
        return urllib.parse.urlparse(url).port or default
    except ValueError:
        return default


def ports():
    try:
        tiles = int(os.environ.get("ARK_TILES_PORT") or 8081)
    except ValueError:
        tiles = 8081
    return {"archive": _port_of(store.KIWIX, 8080),
            "tiles": tiles,
            "node": NODE_PORT,
            "primary": _port_of(llm.ENDPOINTS["primary"], 8091),
            "crosscheck": _port_of(llm.ENDPOINTS["crosscheck"], 8092)}


def ports_js():
    """A script the pages load before anything else, so the ports are known
    synchronously: no request races a render, and a page served by an older
    node simply falls back to its defaults."""
    return "window.ARK_PORTS = %s;\n" % json.dumps(ports(), sort_keys=True)


def retile(body, port):
    """The map viewer and its style name the tile server as localhost:8081.
    Moved in ark.toml, the node rewrites that one string as it serves them, so
    the vendored files in 09-software stay byte for byte what was verified."""
    if port == 8081:
        return body
    # EVERY 8081, NOT ONLY THE URLS. viewer.html also names the port in its
    # status line, in the error it matches and in the command it tells the
    # operator to run; a map that draws from 9181 while telling the reader to
    # start a server on 8081 is the wrong-port diagnosis this exists to prevent.
    return body.replace(b"8081", b"%d" % port)

# HOW MANY CANDIDATES RETRIEVAL ASKS FOR, PER RESULT THE PAGE WANTS.
# Everything removed after retrieval - navigation, duplicates, and the index
# quality filters - eats this pool, so it is the number that decides whether a
# page comes back short. Module constants rather than a literal in `search` so
# `bin/measure-filters.py` can sweep them against the shipped code path instead
# of reimplementing it. See the note in `search` for why they are 8 and 40.
POOL_FACTOR = 8
POOL_MIN = 40


def search(q, n, mode, prev_qs=None):
    """One query. Returns the payload the source pane renders.

    A FOLLOW-UP IS RETRIEVED ON MORE THAN ITS OWN WORDS. `prev_qs` is the
    earlier questions in the thread, most recent first, sent by the browser -
    the server keeps no session. When there is one, the RETRIEVAL string becomes
    the follow-up's content words plus the earlier question's, so `And the dose?`
    searches as `dose child fever stiff neck` and finds a dose. The QUESTION
    stays exactly what was asked; only the search string changes, and the
    payload reports both so the operator can see what was searched.

    The pool is deliberately larger than n: navigation pages and near-duplicates
    are removed AFTER retrieval, so asking for exactly n would return fewer than n
    every time something was suppressed.

    IT TIMES ITSELF. The footer read "undefineds" on every query for at least
    two days because it renders d.seconds from THIS object, while seconds was
    being set on the enclosing request payload. Retrieval that reports its own
    cost is also the honest shape: the pane is showing how long the search took,
    not how long the HTTP handler took."""
    t_start = time.time()
    # WHAT IS SEARCHED AND WHAT WAS ASKED ARE NOW TWO DIFFERENT STRINGS.
    # Everything downstream of retrieval - the model, the safety assessment of
    # the query, the spatial card, the printed card - reads `q`, the question.
    # Only the index sees `rq`. Conflating them would put words the operator
    # never typed into the answer's own framing.
    rq = q
    expanded = store.expand_followup(q, prev_qs) if prev_qs else None
    if expanded:
        rq = expanded
    # THE POOL HAS TO SURVIVE THE FILTERS.
    # It was n*4 when the only things removed after retrieval were navigation
    # pages and byte-identical duplicates, which together took 7.8% of slots.
    # The index quality filters remove 37.1% of the index (store.excluded), and
    # they are CONCENTRATED rather than spread: a medical question meets wikem
    # translations and the mdwiki mirror, a repair question meets 297,242 iFixit
    # user pages. A pool of 20 on such a query can be exhausted before five
    # results survive, and the old code would simply have returned three with
    # nothing saying why.
    #
    # DOUBLED, AND THE EXHAUSTION IS REPORTED RATHER THAN INFERRED. Asking BM25
    # or Chroma for 40 instead of 20 is close to free - both are top-k over an
    # index already built - and `candidates_exhausted` in the payload says when even
    # that was not enough, so a page of three results is a stated fact instead of
    # a mystery. Which multiplier is right is a measurement, not a guess, and it
    # was taken before this shipped. `python bin/measure-filters.py`, 24 real
    # queries, keyword, 2026-09-11: **23 of 24 pages returned a full 5, and 13
    # of 24 changed.** The one short page is `spate irrigation`, which returns
    # ONE candidate from the whole index with ZERO exclusions - short before the
    # filters existed and short with them off, and already on the open list.
    # Worst exclusion on a full page was 8 of 40 on `symptoms of tetanus`, so 40
    # has room. They are module constants so the measurement can sweep them by
    # setting `serve.POOL_FACTOR` rather than by carrying its own copy of this
    # line - the mistake measure-retrieval.py records having made once.
    pool = max(n * POOL_FACTOR, POOL_MIN)
    dense_ids, dense_score = ([], {})
    sparse_ids = []
    if mode in ("hybrid", "dense"):
        dense_ids, dense_score = S.dense(rq, pool)
    # THE FALLBACK HAS TO ACTUALLY RUN. The first version reported "ran as
    # keyword" for a dense request with no dense half, and then returned nothing,
    # because the keyword half was never executed. A label that describes work
    # nobody did is worse than no label.
    if mode in ("hybrid", "keyword") or (mode == "dense" and not S.dense_available()):
        sparse_ids = S.keyword(rq, pool)

    # SAY WHEN THE MODE ASKED FOR IS NOT THE MODE THAT RAN.
    # Asking for hybrid on a node where the embedding stack is not importable
    # silently returned keyword results labelled "single" - true, and useless: it
    # reads as a property of the query rather than as "half of what you asked for
    # did not run". A degraded mode the operator cannot see is the failure
    # NODE-ARCHITECTURE.md lists last, and it was reproduced here on 2026-09-04
    # by starting the server with the wrong interpreter.
    degraded = None
    if S.books_stale:
        degraded = S.books_stale
    if mode in ("hybrid", "dense") and not S.dense_available():
        # THE MEASURED CAUSE, NOT AN ASSUMED ONE. Until 2026-09-16 this said the
        # embedding stack was not importable by the interpreter running the
        # server and told the operator to start it with the keeper venv. On
        # 2026-09-15 that sentence was printed by a node ALREADY RUNNING UNDER
        # THE KEEPER VENV, whose real failure - a numpy circular import from a
        # second thread - was sitting unread in `/api/health`. The banner had
        # taken "dense did not load" and printed the one explanation somebody had
        # in mind when it was written. That is this build's most repeated defect,
        # reaching the one surface an operator actually reads.
        _why = S.dense_error()
        degraded = ("Dense retrieval is not loaded, so this ran as KEYWORD only. "
                    + ("The node reports: %s" % _why if _why else
                       "The node did not record a cause, which is itself worth "
                       "reporting - see /api/health.")
                    + " If this node was started without the keeper venv, that is "
                      "the first thing to check; /api/health names the interpreter "
                      "and the error either way.")

    if mode == "hybrid" and dense_ids and sparse_ids:
        order = S.fuse(dense_ids, sparse_ids)
        both = len(set(dense_ids) & set(sparse_ids))
        label, why = S.agreement(both)
    else:
        # a dense-only request with no dense half has nothing of its own to show,
        # and an empty page with no explanation is the worst of the options
        order = dense_ids or sparse_ids
        both = 0
        # HONEST LABEL. With one retriever there IS no agreement to report, and
        # calling that "low agreement" would read as a finding about the corpus
        # rather than a fact about how the query was run.
        label, why = ("single", "one retriever only - no agreement signal is available")

    # THE SAME PASSAGE FROM TWO ARTIFACTS IS ONE PASSAGE.
    # `seen_docs` keys on (src, dnum), which suppresses repeats of ONE document
    # and cannot see the same text arriving from two. This archive holds
    # overlapping mirrors on purpose - mdwiki_en_all_maxi and
    # wikipedia_en_medicine_maxi carry many of the same articles, and
    # zimgit-post-disaster carries copies of the zimgit-water and
    # zimgit-medicine PDFs - so byte-identical passages competed for slots on a
    # page of ten. Measured 2026-09-08 over 24 real queries with
    # `bin/measure-retrieval.py`: 18 of 231 slots, 7.8%, worst case 3 of 10.
    #
    # IT WAS ON SCREEN THE WHOLE TIME. `--selftest` printed `Water (7)` twice
    # with two different artifact URLs, and `First Aid and Medicine (11)` twice.
    # Two adjacent results with the same title is what this looks like, and it
    # read as two sources agreeing rather than as one source counted twice.
    #
    # FIRST BY RANK WINS, and nothing prefers one mirror over another. Ranking
    # the artifacts would be a claim about source quality this build has not
    # measured; rank order is deterministic and needs no such claim.
    results, nav, dupe, same_text = [], 0, 0, 0
    excl = {}
    seen_docs, seen_text = set(), set()
    consumed = 0
    for cid in order:
        if len(results) >= n:
            break
        consumed += 1
        m = S.meta(cid)
        if not m:
            continue
        # BEFORE text(), DELIBERATELY. About one candidate in three is excluded,
        # and each one that reaches text() costs a seek and a read into a
        # half-gigabyte jsonl for a passage nobody will see. The rules are in
        # store.excluded and every one of them is reversible with
        # ARK_NO_FILTERS=1; the counts go into the payload because a node that
        # drops a third of what it retrieved and says nothing is the shape this
        # build spent two days removing.
        why = S.excluded(m)
        if why:
            excl[why] = excl.get(why, 0) + 1
            continue
        txt = " ".join(S.text(m).split())
        if S.is_navigation(m, txt):
            nav += 1
            continue
        key = (m["src"], m["dnum"])
        if key in seen_docs:
            dupe += 1
            continue
        fp = hashlib.sha1(txt.encode("utf-8")).hexdigest()
        if fp in seen_text:
            same_text += 1
            continue
        seen_text.add(fp)
        seen_docs.add(key)
        results.append({
            "cid": cid,
            "score": round(dense_score[cid], 4) if cid in dense_score else None,
            "from": ("both" if cid in dense_score and cid in sparse_ids
                     else "dense" if cid in dense_score else "keyword"),
            "text": txt,
            "citation": S.citation(m),
            # PER RESULT, NOT JUST PER QUERY. The operator is about to read four
            # passages and act on one. Marking which of them carry a dose, a
            # voltage or a pressure is the difference between "here are some
            # sources" and rule 9.2, which is about the specific number acted on.
            "safety": _slim(safety.assess(txt)),
            # WHERE THIS PASSAGE IS ABOUT, WHEN IT IS ABOUT ANYWHERE.
            # Measured on 5,800 chunks sampled from all 29 artifacts: 9.2% carry
            # a place that survives spatial.in_passage, and reading 40 of them by
            # hand put roughly nine in ten right. Empty for the other nine
            # passages in ten, which is the point - the query-side card answers
            # the question, and this only marks a passage that is itself
            # somewhere. No model, no index, about 14 ms per passage.
            "places": spatial.passage_places(txt),
        })
    # THE QUERY IS ASSESSED TOO, AND SEPARATELY.
    # "what dose of amoxicillin for a child" is safety-consequential even when the
    # archive returns nothing, because the operator is about to act on a number
    # either way. An assessment that only looked at what came back would go quiet
    # in exactly the case where the archive failed to answer - which is the case
    # §11.2 says to expect and §9.2 exists for.
    q_safety = safety.assess("", query=q)

    # THE BANNER FOLLOWS THE PASSAGES, NOT ONLY THE QUESTION.
    # "amoxicillin dose for a child" contains no unit, so the question on its own
    # reaches `possible` at best - while all four passages that came back carried
    # doses. The operator is about to act on THOSE numbers, so a firm finding in
    # the results raises the §9.5 line too. The §9.4 line does not move: a
    # retrieved passage IS the archive, and cross-checking it against a model is
    # the wrong direction.
    firm_hits = sum(1 for r in results if r["safety"]["level"] == "present")
    return {
        "query": q,
        # REPORTED, BECAUSE A SEARCH THE OPERATOR CANNOT SEE IS A SEARCH THEY
        # CANNOT CORRECT. Present only when it differs from the question, so an
        # ordinary single-turn query carries neither key and the presence of one
        # means something.
        "retrieval_query": (rq if rq != q else None),
        "followup_from": list(prev_qs) if prev_qs else None,
        "seconds": round(time.time() - t_start, 3),
        "mode": mode,
        "mode_ran": ("keyword" if (mode in ("hybrid", "dense")
                                   and not S.dense_available()) else mode),
        "degraded": degraded,
        "results": results,
        "agreement": {"label": label, "why": why, "in_both": both,
                      "dense": len(dense_ids), "keyword": len(sparse_ids)},
        "suppressed": {"navigation": nav, "same_document": dupe,
                       "duplicate_text": same_text,
                       # per rule, not one total: "we dropped 14 results" is not
                       # a fact anyone can act on, and the three rules answer
                       # three different questions about the corpus
                       "excluded": excl,
                       "excluded_total": sum(excl.values())},
        # WHETHER THE PAGE IS SHORT BECAUSE THE ARCHIVE IS THIN OR BECAUSE THE
        # FILTERS ATE THE POOL. Those look identical to an operator and mean
        # opposite things: the first is the honest §11.2 answer, the second is a
        # node that had the passages and never looked at them.
        #
        # SO THE TWO NUMBERS ARE BOTH REPORTED, AND THE FIRST VERSION OF THIS
        # CONFLATED THEM. `pool_exhausted` was true whenever the candidate list
        # ran out, which is also what happens when BM25 simply has nothing:
        # `spate irrigation` returns ONE candidate and zero exclusions, and the
        # flag fired on it. That is a check true about its own question - *did
        # we run out of candidates* - and misleading about the one being asked,
        # *did the filters cost this page results*. The same shape this build
        # fixed in `ark.py status` on 09-11. `pool` is what retrieval was asked
        # for, `retrieved` is what came back, and a short page with
        # `excluded_total` 0 is a thin corpus and nothing else.
        "pool": pool,
        "retrieved": len(order),
        "candidates_exhausted": bool(consumed >= len(order) and len(results) < n),
        # WHAT IS ON THIS NODE AND UNSEARCHED, WITH A DOOR RATHER THAN AN APOLOGY.
        # The index covers 29 artifacts; kiwix-serve holds 67. Until 2026-09-12
        # the other 42 - Wikipedia EN and ES, Stack Overflow, Gutenberg,
        # Wiktionary, 686 GB - could not be reached by the running node at all,
        # so "NOT IN ARCHIVE" was wrong AND there was nothing better to say.
        # Now there is. Carried on EVERY search rather than only on an empty
        # one, because an operator deciding whether to trust a thin page needs
        # the same fact as one staring at a blank page; the surface decides when
        # to show it.
        "unindexed": S.unindexed(q),
        "dense_available": S.dense_available(),
        # WHICH vector index answered, and over how much. Carried on every
        # search, not only in health, because PQ64 covers all 6,926,271 chunks
        # and Chroma covers only what index-build.py loaded into it - 2,315,810
        # after the 2026-09-14 pass. Falling back from one to the other is a
        # two-thirds loss of dense reach and would otherwise be invisible.
        "dense_backend": S.dense_backend(),
        "dense_coverage": S.dense_coverage(),
        "dense_error": S.dense_error(),
        "safety": {
            "query": _slim(q_safety, "query_findings"),
            # §9.5 and §9.4 read the same assessment and draw different lines.
            # Both flags are carried explicitly rather than left to each consumer
            # to re-derive from the level, so that when the threshold changes it
            # changes in safety.py and not in three places that have drifted.
            "asked_about": q_safety["asked_about"],
            "render_text_only": q_safety["render_text_only"] or bool(firm_hits),
            "cross_check": q_safety["cross_check"],
            "verify_prompt": q_safety["verify_prompt"],
            "hits": sum(1 for r in results if r["safety"]["level"] != "none"),
            "firm_hits": firm_hits,
        },
    }


def _json_ok(d):
    try:
        json.dumps(d)
        return True
    except Exception:
        return False


def _slim(a, key="findings"):
    """What the wire carries. Spans stay - the pane highlights them."""
    return {"level": a["level"], "categories": a["categories"],
            "findings": [{"text": f["text"], "start": f["start"],
                          "end": f["end"], "category": f["category"],
                          "tier": f["tier"]} for f in a[key]]}


# --------------------------------------------------------------------------
# THE CROSS-CHECK IS A JOB, NOT A REQUEST.
#
# Measured 2026-09-04: the primary writes an answer in about 18 seconds and the
# cross-check needs about FOUR minutes - measured 2026-09-10 at 2.48 t/s, 600
# tokens in 241.61s, not the 3 t/s this file used to assume. It is a 31B model
# on the CPU. If
# the answer waited for it, every safety-relevant question would take two and a
# half minutes and §9.4 would be switched off by the first impatient operator.
#
# So the answer returns immediately and the second opinion arrives later as a
# state change. The operator reads while it runs, which is the only arrangement
# where a slow check gets used at all. NODE-ARCHITECTURE §2 described this shape
# before there were numbers; the numbers agree with it.
# --------------------------------------------------------------------------

JOBS = {}
JOBS_LOCK = threading.Lock()
JOB_TTL = 1800          # seconds. Long enough to read an answer and come back.
JOB_MAX = 32            # a node with one operator does not need a queue


def _reap():
    now = time.time()
    for k in [k for k, v in JOBS.items() if now - v["started"] > JOB_TTL]:
        JOBS.pop(k, None)
    while len(JOBS) > JOB_MAX:
        JOBS.pop(min(JOBS, key=lambda k: JOBS[k]["started"]), None)


def cancel_crosschecks(why="a new question was asked"):
    """Stop every running cross-check. Returns how many were actually aborted.

    WHY THIS EXISTS AT ALL. Gemma runs `-ngl 0 -t 16` on the CPU with no `-np`,
    so llama-server gives it ONE slot: an abandoned check does not merely waste
    four minutes of 16 threads, it makes the NEXT check queue behind it. Three
    safety-relevant questions in a row put the third one five minutes out while
    the surface kept saying 2.5 - a confident, specific, wrong number in the
    status line, which is §11.2's failure shape somewhere nobody looks for it.

    JUAN'S DECISION 2026-09-10: any new question cancels the running check,
    taking speed over a check the operator has demonstrably stopped waiting for.
    THE CONDITION THAT RIDES WITH IT is that a cancelled check must never render
    as one that passed or was not needed - hence a state of its own, carried to
    the screen and to the printed card. An operator reading an empty cross-check
    area as a clean one is the failure this is designed against, and it is the
    same mistake `unavailable` got its own wording to prevent on 09-04.

    The token is closed while holding JOBS_LOCK only long enough to collect it;
    the socket shutdown happens outside, because a cancel must never be able to
    block the request thread that asked for it.

    IT CANCELS EVERY RUNNING CHECK, NOT ONLY THE CALLER'S, and on a LAN with two
    people that means one operator's question stops another's check. Recorded
    rather than hidden - but it costs nothing that was available anyway: Gemma
    has ONE slot, so two checks could never have run concurrently, and the
    second was already going to wait for the first. What changes is which one
    survives, not how many run. `JOB_MAX`'s own comment has assumed a single
    operator since the job registry was written."""
    toks = []
    with JOBS_LOCK:
        for k, v in JOBS.items():
            if v.get("state") == "running" and v.get("_cancel") is not None:
                v["state"] = "cancelled"
                v["why"] = why
                toks.append(v.pop("_cancel"))
    n = 0
    for t in toks:
        if t.cancel():
            n += 1
    return n


def start_crosscheck(question, results, primary_text):
    job = uuid.uuid4().hex[:12]
    tok = llm.CancelToken()
    with JOBS_LOCK:
        _reap()
        JOBS[job] = {"state": "running", "started": time.time(),
                     "model": "gemma-crosscheck", "_cancel": tok}

    def run():
        try:
            out = answerlib.crosscheck(question, results, primary_text,
                                       cancel=tok)
            with JOBS_LOCK:
                if job in JOBS:
                    # AND NOT IF IT WAS CANCELLED WHILE THE REPLY WAS IN FLIGHT.
                    # cancel_crosschecks sets the state before closing the
                    # socket, so a reply that landed in that window would
                    # otherwise overwrite `cancelled` with `done` and present a
                    # result the operator was told had been abandoned.
                    if JOBS[job].get("state") != "cancelled":
                        JOBS[job].update(out)
                        JOBS[job]["state"] = "done"
        except llm.Cancelled:
            with JOBS_LOCK:
                if job in JOBS:
                    JOBS[job]["state"] = "cancelled"
                    JOBS[job].setdefault("why", "a new question was asked")
        except llm.LLMUnavailable as e:
            # NOT "FAILED". The cross-check being unavailable is a fact about the
            # node, not about the answer, and the surface must not let the two
            # look alike: an operator who reads "cross-check failed" beside a dose
            # may believe something was checked and found wanting.
            with JOBS_LOCK:
                if job in JOBS:
                    JOBS[job].update({"state": "unavailable", "detail": e.detail})
        except Exception as e:                                # pragma: no cover
            with JOBS_LOCK:
                if job in JOBS:
                    JOBS[job].update({"state": "error",
                                      "detail": "%s: %s" % (type(e).__name__, e)})
        finally:
            with JOBS_LOCK:
                if job in JOBS:
                    JOBS[job].pop("_cancel", None)

    t = threading.Thread(target=run, name="crosscheck-" + job, daemon=True)
    t.start()
    return job


# ---------------------------------------------------------------------------
# A PASSAGE THAT NAMES A TABLE IT DOES NOT CONTAIN
# ---------------------------------------------------------------------------
#
# Chunk 18:1:46 of the USDA home canning guide says "the example for peaches is
# given in Table for Example C below" and then carries the table for Example B.
# Table C is in chunk 45. Both halves are correct and they are not in the same
# chunk, because chunk boundaries follow the text and a printed page does not.
# A model given only that passage sees a stated figure beside a table that
# disagrees with it, and nothing marks the passage as partial.
#
# WIDENING THE CHUNK OVERLAP WOULD FIX IT AND COST FOUR TIMES THE INDEX.
# 512-token chunks with 77 of overlap advance 435 tokens at a time. Carrying a
# printed page would need about 400 of overlap, a stride of 112, and 2.27M
# chunks would become 8.8M. Raising the model's context is also out: -c 8192
# already, and Qwen holds 14.5 GiB against 14.69 free.
#
# Provenance stores every chunk's src, dnum, i and n, so the chunk before or
# after a hit is a string away and one seek. This fetches it ONLY when a passage
# names a table it does not carry, so the usual query pays nothing.
#
# THE LABEL DECIDES THE DIRECTION, NOT THE WORD "BELOW". The USDA passage says
# "below" and Table C is on the PREVIOUS page, because a PDF's tables float and
# its prose does not. So the reference's own label - "for Example C" - is
# searched for in both neighbours, and the direction word is only a fallback
# when there is no label to match.
_REF = re.compile(r"(?i)\b(?:table|figure|fig\.|chart)\b[ \t]*([^.,;:\n]{0,28}?)"
                  r"[ \t]*\b(below|above|following|preceding|opposite)\b")

MAX_ADJACENT = 2


def _expand_references(results, limit=MAX_ADJACENT):
    """Insert the adjacent chunk after any passage that names a table it lacks.

    Mutates the list in place so the answer, the source pane and the [n] chips
    all read from one list. Two lists was defect 17 of 2026-09-05: the pane
    showed eight, the answer was given five, the chips indexed by position, and a
    chip could point at a passage nobody cited.
    """
    added = 0
    i = 0
    while i < len(results) and added < limit:
        r = results[i]
        txt = r.get("text") or ""
        if r.get("adjacent"):
            i += 1; continue
        hit = None
        for m in _REF.finditer(txt):
            label = " ".join(m.group(1).split())
            # A LABEL HAS TO IDENTIFY SOMETHING. Scanning 30,000 chunks found
            # exactly one match for this rule and its label was "and the", from
            # "the table and the figures below" - two stopwords that appear in
            # any neighbour, so it would have attached an unrelated passage and
            # explained itself convincingly. A real label carries a digit or a
            # capital: "3.2", "for Example C", "A". Function words do not.
            if label and not re.search(r"[0-9]|[A-Z]", label):
                label = ""
            # NO LABEL, NO EXPANSION. Measured on 25,000 chunks: 37 passages
            # carry a reference, and only 2 of them name something specific. The
            # other 35 say "the table below" and nothing more, and the fallback
            # that used to serve them - attach the neighbour if it mentions a
            # table - fired ten times more often on a heuristic whose precision
            # was never established. In a reference document the neighbour
            # almost always mentions a table. Inserting a passage the operator
            # did not search for, with a line explaining why, is a claim; it
            # needs a label to be true.
            # TRAILING FUNCTION WORDS ARE NOT PART OF A LABEL. One real case
            # extracted "7-30 and by the" from "Figure 7-30 and by the ...
            # below"; the identifier is "7-30".
            label = re.sub(r"(?i)\s+(?:and|or|by|of|in|to|for|with|the|a|an)"
                           r"(?:\s+\w+)*$", "", label).strip()
            if label and not re.search(r"[0-9]|[A-Z]", label):
                label = ""
            if not label:
                continue
            # THE TABLE IS ALREADY HERE. A reference plus its caption is two
            # occurrences of the same label; one occurrence is a reference alone.
            if label and txt.lower().count(label.lower()) >= 2:
                continue
            hit = (label, m.group(2).lower())
            break
        if not hit:
            i += 1; continue
        label, direction = hit
        meta = S.meta(r["cid"])
        if not meta:
            i += 1; continue
        order = [meta["i"] + 1, meta["i"] - 1]
        if direction in ("above", "preceding"):
            order.reverse()
        chosen = None
        for j in order:
            if not (0 <= j < meta["n"]):
                continue
            cid = "%s:%s:%d" % (meta["src"], meta["dnum"], j)
            m2 = S.meta(cid)
            if not m2:
                continue
            t2 = " ".join(S.text(m2).split())
            # THE NEIGHBOUR MUST CONTAIN THE CAPTION, NOT THE LABEL LOOSE IN
            # THE TEXT. A substring test accepted "13" inside "2013", which is
            # the same defect as a one-word context term matching inside another
            # word - the mistake this build has now made four times. Requiring
            # the keyword AND the label, adjacent and on word boundaries, is
            # what makes the match mean "the caption is here".
            if not re.search(r"(?i)\b(?:table|figure|fig\.|chart)\s+"
                             + re.escape(label) + r"\b", t2):
                continue
            chosen = (cid, m2, t2)
            break
        if not chosen:
            i += 1; continue
        cid, m2, t2 = chosen
        if any(x.get("cid") == cid for x in results):
            i += 1; continue
        results.insert(i + 1, {
            "cid": cid,
            "score": None,
            "from": "adjacent",
            "text": t2,
            "citation": S.citation(m2),
            "safety": _slim(safety.assess(t2)),
            # SAID OUT LOUD, IN THE PAYLOAD. A passage the operator did not
            # search for, sitting among ones they did, has to explain itself.
            "adjacent": {
                "of": r["cid"],
                "why": ("that passage refers to \u201c%s\u201d and does not "
                        "contain it" % label),
            },
        })
        added += 1
        i += 2
    return added


def do_spatial(q):
    """Whether this question is about a place, and what to show if it is.

    NO MODEL ON THIS PATH, deliberately: section 3.4 says retrieval must work
    with nothing loaded, so anything that gates it must too. The rules are
    measured rather than guessed - bin/measure-spatial-cues.py imports
    spatial.py and reports what they cost on 83,856 real questions."""
    c = spatial.classify(q)
    return {
        "query": q,
        "intent": c["intent"],
        "cue": c["cue"],
        "why": c["why"],
        "degraded": c["degraded"],
        "distance_km": c["distance_km"],
        "places": [spatial.card(p) for p in c["places"]],
    }


PREV_Q_MAX = 2
PREV_Q_CHARS = 400


def prev_questions(qs):
    """The thread, out of the query string. `prev_q` repeated, most recent first.

    THE BROWSER HOLDS THE THREAD AND THE SERVER HOLDS NOTHING. `serve.py` is
    http.server with no sessions, argued in README.md, and a session here would
    bring identity, eviction and unbounded growth to the one layer that has
    stayed free of all three. The client sends what it wants carried; two people
    on the same LAN cannot collide because there is no shared state to collide
    in, and a restart loses no thread that mattered.

    BOUNDED ON ARRIVAL, not deeper in. Two turns and 400 characters each is what
    the 8,192-token budget affords once passages are in the prompt, and an
    unbounded thread would eventually search for everything anyone mentioned."""
    out = []
    for t in (qs or [])[:PREV_Q_MAX]:
        t = (t or "").strip()[:PREV_Q_CHARS]
        if t:
            out.append(t)
    return out


def do_answer(q, n, mode, prev_qs=None):
    """Retrieve, answer, and start the second opinion if it is worth its cost."""
    found = search(q, n, mode, prev_qs=prev_qs)
    results = found["results"]
    # Fetch the table a passage names but does not hold, before the model reads it.
    found["adjacent_added"] = _expand_references(results)
    out = {"query": q, "retrieval": found}
    # A PLACE THE QUESTION NAMES IS CONTEXT THE PASSAGES DO NOT CARRY. Attached
    # when there is one and omitted when there is not, rather than always
    # present and usually empty.
    sp = do_spatial(q)
    if sp["intent"]:
        out["spatial"] = sp

    # WITH NO MODEL THERE IS NO GROUNDING TO JUDGE, so a suggestion is made
    # only when retrieval itself found nothing: the one case where "the library
    # does not hold this" is known without a model.
    if not llm.available("primary"):
        # THE SOURCE PANE STILL WORKS AND THE PAYLOAD SAYS SO EXPLICITLY.
        # Silence here would teach the operator that no answer means the archive
        # holds nothing, which is a different and much worse statement.
        out["answer"] = None
        out["model_down"] = ("The primary model is not running. Retrieval is "
                             "unaffected - the passages below were found without "
                             "any model.")
        sg = suggestion_for(q, None, len(results))
        if sg:
            out["suggest"] = sg
        return out

    try:
        a = answerlib.answer(q, results)
    except llm.LLMUnavailable as e:
        out["answer"] = None
        out["model_down"] = e.detail
        sg = suggestion_for(q, None, len(results))
        if sg:
            out["suggest"] = sg
        return out

    out["answer"] = a
    sg = suggestion_for(q, a.get("grounding"), len(results))
    if sg:
        out["suggest"] = sg
    sf = a["safety"]
    unsourced = a["grounding"] in ("unsourced", "grounded_plus")
    # THE TWO ARE NOT THE SAME SITUATION even though both force the check.
    # `unsourced` means the passages failed; `grounded_plus` means they did not
    # and the model added something anyway. Telling an operator "nothing in the
    # archive supports this answer" about a cited answer would be false, and it
    # is the sentence they would act on.
    plus = (a["grounding"] == "grounded_plus")
    # AN UNSOURCED ANSWER HAS NO PASSAGE TO READ, so the second model family is
    # not a second opinion - it is the only opinion that can be checked against
    # anything. `safety.assess` therefore sets cross_check for it at any level.
    # It is ATTEMPTED AND REPORTED, NEVER GATING: the 2026-09-09 decision is
    # that an operator who is told is better served than one who is blocked, so
    # the figure is on screen at once and the check lands behind it.
    if sf["cross_check"] and llm.available("crosscheck"):
        out["crosscheck_job"] = start_crosscheck(q, results, a["text"])
        out["crosscheck_why"] = (
            ("The cited answer stands on the passages, but the model added "
             "something from its own training - so the second model family is "
             "reading the same passages to check the cited part. Nothing can "
             "check the added block. About four minutes.")
            if plus else
            ("Nothing in the archive supports this answer, so the second model "
             "family is being asked the same question independently. Two models "
             "disagreeing is the only check available here. About four "
             "minutes - THE FIGURE ABOVE IS UNVERIFIED UNTIL IT LANDS.")
            if unsourced else
            "A figure in this answer is one rule 9.2 says to verify, so the second "
            "model family is reading the same passages. About four "
            "minutes.")
    elif sf["cross_check"]:
        out["crosscheck_why"] = (
            ("The model added a block from its own training and the second model "
             "is not running, so the added part is unverified and the cited part "
             "is unchecked. Read the passages themselves.")
            if plus else
            ("Nothing in the archive supports this answer AND the second model is "
             "not running, so nothing has checked it at all. It is one model's "
             "recall, unverified by anything. Start the crosscheck model and ask "
             "again before acting on it.")
            if unsourced else
            "This answer warrants a cross-check and the "
            "second model is not running.")
    else:
        out["crosscheck_why"] = ("No cross-check: nothing in this answer reaches "
                                 "the threshold that justifies a second model.")
    return out


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "ark-node/0.1"

    def log_message(self, fmt, *a):
        sys.stderr.write("  %s %s\n" % (self.address_string(), fmt % a))

    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        ctype = _header_value(ctype)
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False, indent=1).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # No cache on API answers: a node whose index was just rebuilt must not
        # keep answering from the old one.
        if ctype.startswith("application/json"):
            self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(u.query)
        try:
            if u.path in ("/", "/index.html"):
                return self._file(os.path.join(WEB, "index.html"))
            # THE SERVICES PAGE (2026-09-27). `/` stays the search page, because
            # the manual, DRILL-01 and RECOVERY all send the operator there to
            # type a question. This is one click away from it, in the header.
            if u.path in ("/home", "/home/"):
                return self._file(os.path.join(WEB, "home.html"))
            # WHAT THE LIBRARY HOLDS (2026-10-06): every corpus, whether answers
            # can cite it, and how to add or repair the rest.
            if u.path in ("/library", "/library/"):
                return self._file(os.path.join(WEB, "library.html"))
            if u.path == "/api/corpus":
                out = corpuslib.public(corpus_view(
                    force=(qs.get("refresh") or [""])[0] == "1"))
                out["topic_list"] = [{"id": t[0], "label_en": t[1], "label_es": t[2]}
                                     for t in corpuslib.TOPICS]
                out["computed_seconds_ago"] = round(time.time() - _CORPUS["at"], 1)
                return self._send(200, out)
            if u.path == "/api/ports.js":
                return self._send(200, ports_js(),
                                  ctype="application/javascript; charset=utf-8",
                                  extra={"Cache-Control": "no-store"})
            if u.path == "/api/health":
                st = S.stats()
                # COMPUTED WHETHER OR NOT DENSE HAS EVER LOADED. It reads two
                # files and imports nothing, so it answers on a node that has
                # never been asked a hybrid question - which is exactly when an
                # operator checking health would want it.
                pqgap = S.pq_coverage_gap()
                return self._send(200, {
                    "ok": True,
                    "uptime_seconds": round(time.time() - STARTED, 1),
                    "archive_root": store.ROOT,
                    "kiwix_base": store.KIWIX,
                    "ports": ports(),
                    "index": st,
                    "keyword_search": True,
                    "dense_search": S.dense_available(),
                    "dense_attempted": S.dense_attempted(),
                    "dense_backend": S.dense_backend(),
                    "dense_coverage": S.dense_coverage(),
                    # `state` is about the index that exists; `covers` is
                    # about the corpus. An index can be perfectly `ok` and
                    # hold a fifth of the archive, which is what pass 3 made
                    # true on 2026-09-24, so both are reported.
                    #
                    # `registry_chunks` is from sources.json and will NOT
                    # match `index.chunks` above, which is COUNT(*) over
                    # provenance.sqlite3 - a separate build step that lags.
                    # Two different questions, two different numbers.
                    "pq": {"state": S.pq_state(), "detail": S.pq_detail(),
                           "covers": pqgap[0] if pqgap else None,
                           "registry_chunks": pqgap[1] if pqgap else None,
                           "not_indexed": pqgap[2] if pqgap else None},
                    # A check whose result is invisible is a check that has
                    # stopped running (NODE-ARCHITECTURE §7 item 6). The detector
                    # needs no model and no index, so if it is ever NOT ok the
                    # cause is a broken file, and the operator should be told
                    # before they trust a quiet search result.
                    "safety_detector": safety.selfcheck(),
                    "spatial": spatial.status(),
                    "models": models_view(llm.status()),
                    "not_installed": sorted(NOT_INSTALLED),
                    # Counts only; the rows are /api/corpus.
                    "corpus": corpus_summary(),
                    "dense_error": S.dense_error(),
                    # NOT LOADED YET IS NOT DEGRADED. Dense retrieval is lazy, so
                    # every node reported it for the first minute of its life and
                    # the services page counted it as a fault. It is still said,
                    # under `pending`; FAILED to load stays in `degraded`. Two
                    # different facts (DECISIONS 2026-09-06), two different lists.
                    "pending": ([] if S.dense_available() or S.dense_error() else
                                ["dense retrieval not loaded yet - it is lazy, and "
                                 "no hybrid query has been run since this server "
                                 "started"]),
                    "degraded": ([] if S.dense_available() or not S.dense_error()
                                 else ["dense retrieval failed to load - keyword only"])
                                # RUNNING ON CHROMA IS NOT HEALTHY, IT IS REDUCED.
                                # Chroma holds only what was loaded into it, and
                                # since the 09-14 pass that is pass 1 alone. A
                                # node answering from it looks identical to one
                                # answering from the full index unless this says
                                # otherwise.
                                + ([("dense retrieval is running on CHROMA, which "
                                     "covers %s of %s chunks - the PQ index is %s%s"
                                     % ("{:,}".format(S.dense_coverage() or 0),
                                        "{:,}".format(st.get("chunks", 0)),
                                        S.pq_state() or "not loaded",
                                        (": " + S.pq_detail()) if S.pq_detail() else ""))]
                                   if S.dense_backend() == "chroma" else [])
                                # AN INDEX BUILT BEFORE THE LAST PASS IS NOT
                                # STALE, IT IS INCOMPLETE, and nothing else on
                                # this page says so: `pq.state` reads `ok`,
                                # `dense_error` is null, `degraded` is empty and
                                # `dense_coverage` disagrees with a chunk count
                                # in a payload that does not invite the
                                # comparison. Measured on 2026-09-24: 6,926,271
                                # of 39,073,563, so 82.3% of the archive was
                                # dense-invisible on a node reporting nothing
                                # wrong.
                                #
                                # NAMED, NOT COUNTED. "2 artifacts missing" is
                                # not a fact an operator can act on; the
                                # filenames are, and they say which half of the
                                # corpus the answers are not coming from.
                                + ([("the PQ index covers %s of the %s chunks the "
                                     "registry holds - it was built before %s, and "
                                     "dense retrieval cannot reach them. Rebuild: "
                                     "python bin/index-pq-build.py"
                                     % ("{:,}".format(pqgap[0]),
                                        "{:,}".format(pqgap[1]),
                                        ", ".join(pqgap[2][:3])
                                        + (", and %d more" % (len(pqgap[2]) - 3)
                                           if len(pqgap[2]) > 3 else "")))]
                                   if pqgap and pqgap[2] else [])
                                + ([S.books_stale] if S.books_stale else [])
                                # A MIRROR SET THAT IS NOT THERE OR NOT TRUSTED
                                # IS A DEGRADED NODE, not a detail in a count.
                                # The rule is off, so every shared medical
                                # article comes back twice and competes for the
                                # same five slots. Same class as books_stale,
                                # and reported the same way: an operator reading
                                # `degraded` should not have to infer it from a
                                # zero in `index.excluded.by_rule`.
                                + ([st["excluded"]["mirror_detail"]]
                                   if st["excluded"]["mirror_state"] not in
                                   ("ok", "not_needed", None) else [])
                                + kit_faults(llm.status(), spatial.available(),
                                             spatial.status()["error"]),
                })
            if u.path == "/api/answer":
                qs = urllib.parse.parse_qs(u.query)
                q = (qs.get("q") or [""])[0].strip()
                if not q:
                    return self._send(400, {"error": "q is required"})
                nn = max(1, min(10, int((qs.get("n") or ["5"])[0])))
                md = (qs.get("mode") or [DEFAULT_MODE])[0]
                # BEFORE THE MODEL IS TOUCHED, NOT AFTER. The point is to free
                # Gemma's single slot and its 16 CPU threads while this answer is
                # being written, rather than after it has competed with them.
                out_cancelled = cancel_crosschecks()
                t = time.time()
                out = do_answer(q, nn, md, prev_qs=prev_questions(qs.get("prev_q")))
                if out_cancelled:
                    out["cancelled_crosschecks"] = out_cancelled
                out["seconds"] = round(time.time() - t, 2)
                return self._send(200, out)

            if u.path == "/api/crosscheck/cancel":
                # A GET THAT CHANGES SOMETHING, AND THE REASON IS WORTH STATING.
                # This handler has no do_POST and adding one for fifteen lines
                # would be the larger change. Cancelling is idempotent - twice is
                # the same as once - and nothing a browser does on its own
                # reaches a fetch() URL, so the usual objection to a mutating GET
                # does not apply on a local single-operator node. Recorded rather
                # than left as a silent exception to a rule everybody knows.
                #
                # WHY IT EXISTS: `Clear thread` on the surface flushes the
                # conversation, and without this the browser would simply stop
                # watching while Gemma finished four minutes of work nobody would
                # read. That is the exact waste cancellation was built to end on
                # 2026-09-10; an operator ending a conversation is at least as
                # clear a signal as asking a new question.
                n = cancel_crosschecks("the operator cleared the thread")
                return self._send(200, {"cancelled": n})

            if u.path == "/api/crosscheck":
                job = (urllib.parse.parse_qs(u.query).get("job") or [""])[0]
                with JOBS_LOCK:
                    j = JOBS.get(job)
                    # Private keys hold live objects (the cancel token owns a
                    # socket). They are stripped rather than skipped at write
                    # time, so adding another one cannot silently break the
                    # endpoint with a serialisation error mid-response.
                    j = {k: v for k, v in j.items()
                         if not k.startswith("_")} if j else None
                if not j:
                    return self._send(404, {"error": "no such job", "job": job})
                if j["state"] == "running":
                    j["elapsed"] = round(time.time() - j["started"], 1)
                return self._send(200, j)

            if u.path == "/api/search":
                q = (qs.get("q") or [""])[0].strip()
                if not q:
                    return self._send(400, {"error": "q is required"})
                n = max(1, min(int((qs.get("n") or ["8"])[0]), 50))
                mode = (qs.get("mode") or [DEFAULT_MODE])[0]
                if mode not in ("hybrid", "keyword", "dense"):
                    return self._send(400, {"error": "mode must be hybrid, keyword or dense"})
                t = time.time()
                out = search(q, n, mode,
                             prev_qs=prev_questions(qs.get("prev_q")))
                out["seconds"] = round(time.time() - t, 3)
                return self._send(200, out)
            if u.path == "/api/passage":
                cid = (qs.get("cid") or [""])[0]
                m = S.meta(cid)
                if not m:
                    return self._send(404, {"error": "no such chunk", "cid": cid})
                return self._send(200, {"cid": cid, "text": S.text(m),
                                        "citation": S.citation(m)})
            if u.path == "/api/spatial":
                qs = urllib.parse.parse_qs(u.query)
                q = (qs.get("q") or [""])[0].strip()
                if not q:
                    return self._send(400, {"error": "q is required"})
                return self._send(200, do_spatial(q))
            if u.path.startswith("/files/"):
                rel = urllib.parse.unquote(u.path[len("/files/"):])
                full = S.file_path(rel)
                if not full:
                    return self._send(404, {"error": "not in the archive", "path": rel})
                return self._file(full)
            if u.path.startswith("/web/"):
                name = os.path.basename(u.path)
                return self._file(os.path.join(WEB, name))
            if u.path in ("/map", "/map/"):
                return self._file(os.path.join(MAPDIR, "viewer.html"),
                                  retile_port=ports()["tiles"])
            if u.path.startswith("/map/"):
                # UNQUOTE: a fontstack is "Noto Sans Regular", which arrives as
                # /map/fonts/Noto%20Sans%20Regular/0-255.pbf. Without this the
                # glyphs 404 and the map renders with no labels at all, which
                # looks like a styling bug and not a missing file.
                rel = urllib.parse.unquote(u.path[len("/map/"):])
                full = os.path.normpath(os.path.join(MAPDIR, *rel.split("/")))
                root = os.path.normpath(MAPDIR)
                if not (full == root or full.startswith(root + os.sep)):
                    return self._send(403, {"error": "outside the map viewer",
                                            "path": u.path})
                return self._file(full, retile_port=(
                    ports()["tiles"] if full.endswith((".html", ".json"))
                    else None))
            return self._send(404, {"error": "no such route", "path": u.path})
        except BrokenPipeError:
            return
        except Exception as e:                                 # noqa: BLE001
            # SAY WHAT BROKE. A node that fails silently is the failure mode
            # NODE-ARCHITECTURE lists last: degraded mode, stale index and missing
            # services are all currently invisible. Not here.
            import traceback
            traceback.print_exc()
            return self._send(500, {"error": "%s: %s" % (type(e).__name__, e)})

    def _file(self, full, retile_port=None):
        # ONE CONTAINMENT CHECK FOR EVERY FILE THIS SERVER SENDS (2026-10-04).
        # Each route already confined its own path (/files/ by store.file_path,
        # /web/ by basename, /map/ by normpath against MAPDIR), but three checks
        # in three places is three chances to forget one; CodeQL's first scan of
        # the public repository could not see them from here, and neither could a
        # reader. Resolved, then required to sit inside a root this node serves.
        full = _contained(full)
        if full is None or not os.path.isfile(full):
            return self._send(404, {"error": "missing file"})
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript",):
            ctype += "; charset=utf-8"
        if retile_port not in (None, 8081):
            with open(full, "rb") as fh:
                return self._send(200, retile(fh.read(), retile_port), ctype=ctype)
        size = os.path.getsize(full)
        self.send_response(200)
        self.send_header("Content-Type", _header_value(ctype))
        self.send_header("Content-Length", str(size))
        self.end_headers()
        if self.command == "HEAD":
            return
        with open(full, "rb") as fh:
            while True:
                b = fh.read(1 << 16)
                if not b:
                    break
                self.wfile.write(b)


def _contained(path):
    """The resolved path if it lies inside a root this node serves, else None.
    The roots: the pages (ark-web), the map viewer, and the archive itself."""
    real = os.path.realpath(path)
    for root in (WEB, MAPDIR, store.ROOT):
        if not root:
            continue
        base = os.path.realpath(root)
        if real.startswith(base + os.sep):
            return real
    return None


def _header_value(v):
    """A header value with no line breaks. The values sent are fixed strings and
    mimetypes' own table, so this never changes one; it makes that a property of
    the code rather than of every caller."""
    return str(v).replace("\r", "").replace("\n", "")


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


# HYBRID, DECIDED BY JUAN 2026-09-15, AND THE REASON IS A MEASURED REGRESSION.
#
# This was "keyword" from the first day, on a good argument: keyword needs no
# model and no vector store, so it is the mode that works on a node with nothing
# loaded, and graceful degradation is priority four in the spec.
#
# Pass 2 broke the tie. "what do I do for someone in shock" returns, in keyword,
# `How is the second shock in a lambda shock able to form?` from
# physics.stackexchange at slot 3 of 5 - an aerodynamics answer to a medical
# question, on a safety-relevant query, costing a fifth of the result page. BM25
# cannot tell medical shock from a shock wave. In hybrid the same query returns
# five medical results and the physics answer is DECLINED ON MERIT: the PQ index
# covers all 1,071,104 physics chunks and ranks them below first aid anyway.
#
# NOTHING IS LOST ON A NODE WITH NO MODEL. serve.py already runs hybrid as
# keyword when the vector half will not load (see the mode checks in search()),
# so this changes what a working node does and not what a bare one can do.
#
# THE COST IS REAL AND IS PAID ONCE PER SERVER LIFETIME: the first hybrid query
# loads bge-m3 and the 443 MB index, measured at 26 to 36 seconds, and every
# query after it at 0.2 to 2.7. Thirty seconds of blank screen is a bad thirty
# seconds in an emergency, and the fix for that - keyword results first, upgraded
# in place when the vector half lands - is a separate pass, not a default.
#
# The HTTP default matches, so a curl and the surface cannot disagree about what
# this node does. A caller who wants the cheap path passes mode=keyword.
DEFAULT_MODE = "hybrid"

SELFTEST = [
    "how do I make flood water safe to drink",
    "what do I do for someone in shock",
    "como purificar agua despues de una inundacion",
    "how much water does a person need per day in an emergency",
    "signs of dehydration in a child",
]

# ADDED 2026-09-15, AND THE REASON IS THE ENTRY. The five questions above are the
# acceptance set from pass 1 and every one of them is medical or water, which is
# exactly what pass 1 holds. So the suite ran green on 2026-09-14 over an index
# that had just TRIPLED without touching one chunk of what was added - none of
# the five reaches unix (986,093 chunks), electronics (890,967), physics
# (1,071,104) or retrocomputing.
#
# A SUITE THAT EXERCISES NONE OF THE MATERIAL A PASS ADDED IS NOT AN ACCEPTANCE
# TEST FOR THAT PASS. Each of these is answerable ONLY from pass 2, so a green
# line here means the new corpus is genuinely reachable rather than merely
# counted in sources.json.
SELFTEST_PASS2 = [
    # the only material on the drive that answers "the node will not boot"
    ("no space left on device but df shows free space", "unix.stackexchange"),
    ("choosing a pull-up resistor value for an I2C bus", "electronics.stackexchange"),
    ("difference between phase velocity and group velocity", "physics.stackexchange"),
]


def ports_selftest():
    """The ports the pages are handed. No server needed."""
    bad = 0
    js = ports_js()
    try:
        handed = json.loads(js.split("=", 1)[1].strip().rstrip(";"))
    except ValueError:
        handed = None
    for name, ok, detail in [
            ("ports.js is one assignment", js.startswith("window.ARK_PORTS = "), js.strip()[:60]),
            ("ports.js parses as the ports", handed == ports(), ""),
            ("every port is a number", isinstance(handed, dict) and
             all(isinstance(v, int) for v in handed.values()), ""),
            ("the map is untouched at 8081",
             retile(b"http://localhost:8081/x", 8081) == b"http://localhost:8081/x", ""),
            ("a moved tile port is rewritten",
             retile(b"http://localhost:8081/a --port 8081", 9181)
             == b"http://localhost:9181/a --port 9181", "")]:
        if ok:
            print("  ok    ports  %-30s %s" % (name, detail))
        else:
            bad += 1
            print("  FAIL  ports  %-30s %s" % (name, detail))
    return bad


def corpus_route_selftest():
    """The node's corpus view with no index loaded: what a clone shows before
    setup. Nothing may be red on an empty drive, and the health summary must
    carry the four counts."""
    bad = 0
    v = corpus_view(force=True)
    summ = corpus_summary()
    for name, got, want in [
            ("the view is built with no index", isinstance(v.get("rows"), list), True),
            ("the health summary has the four states",
             sorted(k for k in summ if k in corpuslib.STATES), sorted(corpuslib.STATES)),
            ("the library page is a file in ark-web",
             os.path.isfile(os.path.join(WEB, "library.html")), True),
            ("the public view carries no matcher internals",
             any(k.startswith("_") for r in corpuslib.public(v)["rows"] for k in r), False),
            ("a grounded answer gets no suggestion",
             suggestion_for("How do I make water safe with chlorine?", "grounded", 5), None),
            # ON ANY NODE: either water is already citable here (no card), or
            # the card offers a water collection. Never something unrelated.
            ("an unsourced water question is offered water, or nothing",
             (lambda sg: sg is None or any("water" in i["id"] for i in sg["items"]))(
                 suggestion_for("How do I make water safe with chlorine?", "unsourced", 5)),
             True),
            ("a suggestion says why it was made",
             (suggestion_for("Qwrtz vbnmk?", "unsourced", 5) or {"why": "unsourced"}).get("why"),
             "unsourced")]:
        if got == want:
            print("  ok    route  %s" % name)
        else:
            bad += 1
            print("  FAIL  route  %s: got %r" % (name, got))
    return bad


def kit_selftest():
    """What a kit leaves out is not a fault; what it installs still is."""
    bad = 0
    up = {"primary": {"ok": True}, "crosscheck": {"ok": True}}
    starter = {"primary": {"ok": True}, "crosscheck": {"ok": False}}
    gaz = "10-index/gazetteer.sqlite3 is not there"
    for name, got, want in [
            ("full kit, all up: nothing", kit_faults(up, True, None, frozenset()), []),
            ("starter, judged strictly: both",
             kit_faults(starter, False, gaz, frozenset()),
             ["crosscheck model not running", "no gazetteer: " + gaz]),
            ("starter, judged as a starter: none",
             kit_faults(starter, False, gaz, frozenset({"crosscheck", "map"})), []),
            ("an installed model that is down still counts",
             kit_faults({"primary": {"ok": False}, "crosscheck": {"ok": False}},
                        False, gaz, frozenset({"crosscheck", "map"})),
             ["primary model not running"]),
            ("maps present, gazetteer missing still counts",
             kit_faults(up, False, None, frozenset({"crosscheck"})),
             ["no gazetteer: not built"]),
            ("models carry name and installed",
             sorted((r, x["installed"]) for r, x in
                    models_view(starter, frozenset({"crosscheck"})).items()),
             [("crosscheck", False), ("primary", True)]),
            ("models_view copies, not mutates",
             (models_view(starter, frozenset()) and "installed" in starter["primary"]),
             False),
            # The one containment check every served file passes (2026-10-04).
            ("a page inside ark-web is served",
             _contained(os.path.join(WEB, "index.html"))
             == os.path.realpath(os.path.join(WEB, "index.html")), True),
            ("a path that climbs out of the archive is refused",
             _contained(os.path.join(store.ROOT, "..", "outside.txt")), None),
            ("the archive folder itself is not a file to serve",
             _contained(store.ROOT), None)]:
        if got == want:
            print("  ok    kit    %s" % name)
        else:
            bad += 1
            print("  FAIL  kit    %s: got %r" % (name, got))
    return bad


def selftest(mode):
    """The same five acceptance questions bin/index-query.py uses, run through the
    API's own code path. Fails if any hit cannot be turned into a citation, which
    is the property this whole layer exists to provide."""
    bad = 0

    # ---------------------------------------------------------------- cancel
    # RUN FIRST AND WITHOUT A MODEL. The transport half lives in llm.py and is
    # exercised against a throwaway socket; this half is the bookkeeping, which
    # is where the 2026-09-09 lesson applies - `compare()` shipped four state
    # names the surface had never heard of because the FUNCTION was tested and
    # the PATH was not. So this asserts the record an operator's browser will
    # actually be handed, including that it can be serialised at all: the job
    # holds a live CancelToken under a private key, and a job that cannot be
    # turned into JSON breaks /api/crosscheck mid-response with the answer
    # already on screen.
    bad += llm.selftest()
    bad += ports_selftest()
    bad += kit_selftest()

    tok = llm.CancelToken()
    with JOBS_LOCK:
        JOBS["_probe"] = {"state": "running", "started": time.time(),
                          "model": "gemma-crosscheck", "_cancel": tok}
    n = cancel_crosschecks("selftest")
    with JOBS_LOCK:
        rec = dict(JOBS.get("_probe") or {})
        wire = {k: v for k, v in rec.items() if not k.startswith("_")}
        JOBS.pop("_probe", None)

    for name, ok, detail in [
            ("state becomes cancelled", rec.get("state") == "cancelled",
             repr(rec.get("state"))),
            ("token is released", "_cancel" not in rec, ""),
            ("token was marked", tok.cancelled, ""),
            ("reason is carried", bool(rec.get("why")), rec.get("why") or ""),
            ("no live connection to close", n == 0, "cancel() reported %d" % n),
            ("record is JSON-serialisable", _json_ok(wire), "")]:
        if ok:
            print("  ok    job    %-30s %s" % (name, detail))
        else:
            bad += 1
            print("  FAIL  job    %-30s %s" % (name, detail))
    print()

    for q in SELFTEST:
        t = time.time()
        out = search(q, 5, mode)
        a = out["agreement"]
        print("\n%s\n  %d result(s) in %.2fs   [%s: %d dense, %d keyword, %d in both]"
              % (q, len(out["results"]), time.time() - t,
                 a["label"], a["dense"], a["keyword"], a["in_both"]))
        if not out["results"]:
            print("  !! nothing returned")
            bad += 1
        for r in out["results"]:
            c = r["citation"]
            if not c.get("url"):
                print("  !! no citation url for %s" % r["cid"])
                bad += 1
            print("   - %-8s %-58s" % (r["from"], c["title"][:58]))
            print("     %s" % c["url"])
        # NO TWO PASSAGES ON A PAGE MAY BE THE SAME PASSAGE.
        # Checked by hashing what came back, not by reading the suppression
        # counter, because a filter that counts correctly and lets the text
        # through would pass a check that trusted its own number. Before
        # 2026-09-08 this fired on the water and medicine questions: `Water (7)`
        # appeared twice with two different artifact URLs, printed on this very
        # screen for days.
        _fp = {}
        for r in out["results"]:
            k = hashlib.sha1(r["text"].encode("utf-8")).hexdigest()
            if k in _fp:
                print("  !! the same passage twice: %s and %s"
                      % (_fp[k], r["cid"]))
                bad += 1
            _fp[k] = r["cid"]

        sf = out["safety"]
        if sf["query"]["level"] != "none" or sf["hits"]:
            print("  safety: query=%s%s | %d of %d passages carry figures"
                  % (sf["query"]["level"],
                     (" [" + ",".join(sf["query"]["categories"]) + "]")
                     if sf["query"]["categories"] else "",
                     sf["hits"], len(out["results"])))
        s = out["suppressed"]
        if s["navigation"] or s["same_document"] or s["duplicate_text"]:
            print("  (suppressed %d navigation, %d repeats of one document, "
                  "%d the same passage from another artifact)"
                  % (s["navigation"], s["same_document"], s["duplicate_text"]))
        if s["excluded"]:
            print("  (excluded %s of %d retrieved)"
                  % (", ".join("%d %s" % (v, k) for k, v in sorted(s["excluded"].items())),
                     out["retrieved"]))
        # A SHORT PAGE IS A FINDING, NOT A DETAIL - but only when the filters
        # are why. A question the archive barely covers runs out of candidates
        # too, and failing the build on that would be a check that fires on the
        # honest §11.2 answer. `spate irrigation` is the live example: one
        # candidate, zero exclusions, and nothing wrong with the node.
        if out["candidates_exhausted"]:
            if s["excluded_total"]:
                bad += 1
                print("  !! ran out of candidates with %d/5 results after "
                      "excluding %d of %d retrieved - raise POOL_FACTOR or "
                      "narrow a filter"
                      % (len(out["results"]), s["excluded_total"], out["retrieved"]))
            else:
                print("  (only %d candidate(s) in the whole index, nothing "
                      "excluded - this page is short because the archive is "
                      "thin here)" % out["retrieved"])

    # THE DETECTOR IS PART OF THE BUILD CHECK, NOT AN OPTIONAL EXTRA.
    # A dosing question that comes back "none" means the detector silently
    # stopped working, and every consumer downstream of it would then be quietly
    # wrong in the safe-looking direction - no banner, no cross-check, no
    # text-only rule. That is the failure this file's own header calls out, so it
    # fails the build rather than printing a warning nobody reads.
    probe = "amoxicillin dose for a 20 kg child with pneumonia"
    out = search(probe, 3, mode)
    if not out["safety"]["render_text_only"]:
        print("\n  !! the safety detector did not fire on %r" % probe)
        bad += 1
    else:
        print("\n  safety detector: fired on the dosing probe [%s]"
              % ",".join(out["safety"]["query"]["categories"]))
    if not safety.selfcheck()["ok"]:
        print("  !! safety.selfcheck() failed")
        bad += 1

    # ------------------------------------------------------- index quality
    # THE RULES ARE CHECKED AGAINST SYNTHETIC PATHS IN store.py AND AGAINST THE
    # REAL INDEX HERE, because those are two different claims. store.selftest()
    # proves the predicate says the right thing about a path; only this proves
    # the predicate is reached, that the counts it produces match the index the
    # node is serving, and that the pages the defect was found on have changed.
    print("\n  index quality:")
    ex = S.exclusion_stats()
    if ex["off"]:
        # NOT A PASS AND NOT A FAILURE - a selftest run with the filters
        # disabled proves nothing about them, and must say so rather than
        # printing green lines about checks that did not happen. Same shape as
        # the unsourced probe's NOT EXERCISED branch.
        print("    NOT EXERCISED - ARK_NO_FILTERS=1 is set, the filters are off")
    else:
        tot = S.stats()["chunks"]
        print("    %s of %s chunks excluded (%.1f%%): %s"
              % ("{:,}".format(ex["chunks"]), "{:,}".format(tot),
                 100.0 * ex["chunks"] / max(tot, 1),
                 ", ".join("%s %s" % (k, "{:,}".format(v))
                           for k, v in sorted(ex["by_rule"].items()))))
        if not ex["chunks"]:
            print("    !! the filters removed nothing - either the artifacts are "
                  "absent or a rule stopped matching")
            bad += 1
        # THE MIRROR SET IS A FILE AND FILES GO STALE. Its state is asserted
        # rather than printed, because a node running with the rule off shows
        # two copies of every shared medical article and nothing on the page
        # says so.
        ms = ex.get("mirror_state")
        if ms == "ok":
            print("    ok  mirror set                       %s" % ex["mirror_detail"])
        elif ms == "absent":
            print("    -- mirror set NOT BUILT - %s" % ex["mirror_detail"])
        else:
            print("    !! mirror set %s: %s" % (ms, ex["mirror_detail"]))
            bad += 1

        # THE THREE QUESTIONS THE DEFECTS WERE FOUND ON, asked again.
        # A count proves a rule fired; only a question proves the page changed.
        probes = [
            ("who is Albert Einstein", "User/",
             "an iFixit contributor profile"),
            ("dose of ibuprofen for a child", "/vi",
             "a wikem page in a language the operator does not read"),
        ]
        for q, needle, what in probes:
            page = search(q, 5, mode)
            hit = [r for r in page["results"]
                   if needle in (r["citation"].get("path") or "")]
            if hit:
                print("    !! %r still returns %s: %s"
                      % (q, what, hit[0]["citation"]["path"][:60]))
                bad += 1
            else:
                # A PAGE OF NOTHING IS THE RIGHT ANSWER HERE AND IT IS SAID OUT
                # LOUD. `who is Albert Einstein` returns zero results now, and
                # that is the §11.2 answer this corpus should give: it is a
                # medical and practical archive with no biography of him. What
                # it returned before was three iFixit accounts with 1
                # Reputation, labelled GROUNDED IN THE ARCHIVE. Nothing beats
                # a confident irrelevant answer, but the operator has to see
                # which of the two they got.
                n_res = len(page["results"])
                print("    ok  %-30s %d result(s), none is %s%s"
                      % (q[:30], n_res, what,
                         "  <- nothing at all, which is the honest answer here"
                         if n_res == 0 else ""))

        # THE FALLBACK IS A URL AND A URL IS A GUESS UNTIL A SERVER ANSWERS IT.
        # This is the same class of claim as the book key, which was wrong for
        # eight days because it was constructed rather than fetched. So it is
        # fetched - and when kiwix-serve is not running the check says NOT
        # EXERCISED instead of printing a green line about a request nobody made.
        page = search("how do I make flood water safe to drink", 5, mode)
        ui = page.get("unindexed") or {}
        if not ui.get("extra"):
            # NAME THE CAUSE. This line read "nothing served beyond the index, or
            # the library stamp is stale" and a real bug lived behind the "or".
            print("    !! unindexed fallback is OFF: %s" % ui.get("state", "?"))
            bad += 1
        else:
            print("    ok  %-30s %d served, %d indexed, %d searchable only in Kiwix"
                  % ("served but unindexed", ui["served"], ui["indexed"],
                     ui["extra"]))
            # EVERY LINK IS FETCHED, NOT JUST THE FIRST. There is one per
            # language now, and a Spanish operator handed a dead link is the
            # failure this whole design exists to avoid - the wikem translation
            # defect arriving from the other side.
            urls = ui.get("search_urls") or []
            if not urls:
                print("    !! no fallback URL was built for %d unindexed books"
                      % ui["extra"])
                bad += 1
            for u in urls:
                try:
                    code = urllib.request.urlopen(u["url"], timeout=8).getcode()
                except urllib.error.HTTPError as e:
                    code = e.code
                except Exception as e:           # noqa: BLE001 - reported
                    code = "%s" % type(e).__name__
                if code == 200:
                    print("    ok  %-30s HTTP 200, %d unindexed book(s)"
                          % ("fallback: " + u["label"], u["books"]))
                elif isinstance(code, int):
                    print("    !! the %s fallback URL returned %s: %s"
                          % (u["label"], code, u["url"]))
                    bad += 1
                else:
                    print("    -- NOT EXERCISED - kiwix-serve unreachable (%s), so "
                          "the %s fallback URL was not fetched. It is the shape "
                          "most likely to be wrong and it is unchecked this run."
                          % (code, u["label"]))

        # AND THE MIRROR, WHICH IS THE ONE A COUNT CANNOT SHOW.
        # Two artifacts carrying the same article at two revisions produce two
        # results that are NOT byte-identical, so `duplicate_text` never sees
        # them. `Oral_rehydration_therapy` comes back from wikipedia_en_medicine
        # AND mdwiki on the dehydration question, and what that means changed on
        # 2026-09-12.
        #
        # THE CONTRACT IS NOT "NO ARTICLE TWICE" AND THIS ASSERTION SAID IT WAS.
        # It was written against the one-day-old rule that excluded every shared
        # path, under which a repeat was impossible by construction. The rule
        # now excludes a shared path only when the mdwiki copy adds nothing, so
        # a repeat is the EXPECTED outcome for the 22,972 articles where it adds
        # something - Juan's decision, 2026-09-12, on eight articles where the
        # mdwiki copy carried a dose the newer one had dropped.
        #
        # So the check became the one the contract actually makes: a repeat is a
        # failure only when the mdwiki side IS in the mirror set, which would
        # mean the filter did not fire. A repeat from outside the set is the
        # design working and is named rather than counted, because the cost is
        # real and visible here - two of five slots on one article.
        page = search("signs of dehydration in a child", 5, mode)
        mirror = S._mirrored_dnums()
        mdw_src = S._src_of("mdwiki_")
        by_path = {}
        for r in page["results"]:
            c = r["citation"]
            # cid is "src:dnum:i"; see provenance.sqlite3's schema comment
            try:
                src, dnum = int(r["cid"].split(":")[0]), int(r["cid"].split(":")[1])
            except (IndexError, ValueError):
                src = dnum = None
            by_path.setdefault(c.get("path"), []).append(
                (c["artifact"], src, dnum))
        leaked, expected = [], []
        for p, hits in by_path.items():
            if len(hits) < 2:
                continue
            if any(s == mdw_src and d in mirror for _, s, d in hits):
                leaked.append(p)
            else:
                expected.append(p)
        for p in leaked:
            print("    !! %s is in the mirror set and came back anyway - the "
                  "filter did not fire" % p)
            bad += 1
        if expected:
            for p in expected:
                print("    ok  %-30s both copies shown: not in the mirror set, "
                      "so mdwiki adds something" % p[:30])
        if not leaked and not expected:
            print("    ok  %-30s no article returned by two artifacts"
                  % "dehydration in a child")

    if not store.selftest():
        print("  !! store.selftest() failed")
        bad += 1

    # THE ANSWER PATH IS CHECKED WITH OR WITHOUT A MODEL, AND THE DEGRADED BRANCH
    # IS THE ONE WORTH CHECKING. Most of the time this node will be asked a
    # question while llama-server is stopped - during an index build it MUST be,
    # because BGE-M3 and a 14.5 GiB primary do not both fit in 16 GiB of VRAM. The
    # requirement is not that an answer appears; it is that its absence is stated
    # and that retrieval still works and says so.
    # THE SPATIAL ROUTE IS CHECKED HERE FOR THE SAME REASON THE SAFETY DETECTOR
    # IS: it needs no model and no index, so if it stops working the cause is a
    # broken file or a missing gazetteer, and both are things the operator should
    # be told before they trust a quiet answer.
    if not spatial.available():
        print("\n  spatial: no gazetteer (%s)" % spatial.status()["error"])
    else:
        probes = [("what is the climate in Kathmandu", "spatial"),
                  ("¿qué clima hace en Katmandú?", "spatial"),
                  ("what is the weather like in Franklin", "place"),
                  ("how do I sharpen a chisel", None)]
        for q, want in probes:
            got = do_spatial(q)["intent"]
            if got != want:
                print("\n  !! spatial said %r for %r, expected %r" % (got, q, want))
                bad += 1
        d = do_spatial("how far is Bogota from Cali")["distance_km"]
        if not (d and 250 <= d <= 350):
            print("\n  !! Bogota to Cali came back as %r km" % d)
            bad += 1

        # THE SURFACE READS THESE FIELDS BY NAME AND FAILS SILENTLY WITHOUT THEM.
        # ark-web/index.html renders the map offer straight from this payload,
        # and a missing key there is not an error in a browser - it is the string
        # "undefined" in a coordinate, or a button whose link goes nowhere. The
        # pane cannot assert its own inputs, so the contract is asserted here,
        # on the side that produces them.
        need = ("label", "kind", "poprank", "lat", "lon", "map", "zoom",
                "climate_command", "climate_note", "others", "others_brief")
        sp = do_spatial("what is the climate in Cairo")
        for k in ("query", "intent", "cue", "why", "degraded",
                  "distance_km", "places"):
            if k not in sp:
                print("\n  !! /api/spatial payload has no %r" % k)
                bad += 1
        p0 = (sp["places"] or [{}])[0]
        for k in need:
            if k not in p0:
                print("\n  !! a spatial place card has no %r" % k)
                bad += 1
        # AMBIGUITY MUST BE ACTIONABLE, NOT JUST COUNTED. Cairo answers to two
        # places in this gazetteer; a count with no coordinates is what the
        # surface cannot do anything with, and `cairo` resolving to Georgia is a
        # defect this build actually shipped once.
        if p0.get("others") and not p0.get("others_brief"):
            print("\n  !! Cairo reports %s other places and offers none of them"
                  % p0["others"])
            bad += 1
        for o in p0.get("others_brief", []):
            if not all(k in o for k in ("label", "lat", "lon", "map")):
                print("\n  !! an alternate is missing a field: %r" % (sorted(o),))
                bad += 1
        # THE ZOOM MUST FOLLOW WHAT THE PLACE IS. A country at a locality's zoom
        # shows one suburb of its capital, which is worse than no zoom at all.
        cty = do_spatial("what is the climate in Colombia")["places"]
        if cty and cty[0]["kind"] == "country" and cty[0]["zoom"] >= 9:
            print("\n  !! a country came back at zoom %s" % cty[0]["zoom"])
            bad += 1
        # THE TWO INTENTS MUST STAY DISTINGUISHABLE, because the surface draws a
        # card for one and a single line for the other, and the whole reason for
        # that split is that `place` fires 86 times more often.
        pl = do_spatial("what is the weather like in Franklin")
        if pl["cue"] is not None:
            print("\n  !! a place result carried a cue: %r" % pl["cue"])
            bad += 1
        if sp["cue"] is None:
            print("\n  !! a spatial result carried no cue")
            bad += 1

        # THE PASSAGE SIDE. Different rules from the question side and the
        # reason is recorded in spatial.py: questions are sentence-cased, so
        # their one free capital is nearly always a function word, and passages
        # have dozens whose first words are ordinary nouns.
        pp = spatial.passage_places(
            "Sigmoid Colon, Sigmoidectomy: diverticular disease was found. "
            "The patient lives in Phoenix, Arizona. FAO Bulletin No. 138, "
            "Rome. George Washington University reviewed the case.")
        got = sorted(p["label"].split(" (")[0] for p in pp)
        # ROME IS EXPECTED HERE AND IT IS A FALSE POSITIVE. `Bulletin No. 138,
        # Rome` is a publisher's address, and it carries exactly the punctuation
        # that `Kathmandu, Nepal` does. No signal available separates them, and
        # citation-shaped rules were measured and rejected: requiring the next
        # token not to be an initial or a number removed eleven Washingtons and
        # four Californias to catch one Laval. It is asserted rather than hidden
        # so that anyone who finds a better rule sees this line fail.
        if got != ["Arizona", "Phoenix", "Rome"]:
            print("\n  !! passage_places returned %r, expected Arizona, Phoenix "
                  "and the known Rome, with Colon and George Washington "
                  "rejected" % (got,))
            bad += 1
        for k in ("label", "lat", "lon", "map", "zoom", "climate_command"):
            if pp and k not in pp[0]:
                print("\n  !! a passage place card has no %r" % k)
                bad += 1
        # A PASSAGE THAT IS ABOUT NOWHERE MUST RETURN NOTHING. 91.5% of the
        # archive is that case, and a rule that always finds a place is the
        # 50.7% one this replaced.
        if spatial.passage_places("Sharpen the chisel at twenty-five degrees, "
                                  "then hone the back flat on a fine stone."):
            print("\n  !! passage_places found a place in a chisel passage")
            bad += 1
        # AND THE SEARCH PAYLOAD MUST CARRY IT, because the source pane renders
        # `places` by name and a missing key is not an error in a browser.
        probe = search("groundwater in Nevada and Utah", 3, "keyword")
        if probe["results"] and "places" not in probe["results"][0]:
            print("\n  !! a search result has no 'places' key")
            bad += 1

        # UNDER LOAD, BECAUSE THAT IS WHERE IT BROKE AND NOTHING SAID SO.
        # 2026-09-07: three connections were shared across request threads with
        # `check_same_thread=False` and no lock. A connection has ONE prepared
        # statement cache, so two threads running the same SQL got the same
        # statement - and it did NOT merely raise. On one passage the surface
        # showed Missouri, Kansas City and Nebraska where the correct answer,
        # verified deterministic over five runs, is Maryland, Missouri and Maine.
        # A wrong boolean from `_is_place` drops a place or accepts a non-place
        # and says nothing. Errors are the loud half of that defect.
        #
        # THIS ASSERTION CANNOT FAIL ON EVERY BUILD. It failed reliably on
        # Windows/Python 3.12.9/SQLite 3.45.3 and not at all on
        # Linux/3.10.12/3.37.2 under 48,000 iterations. Run it where the node
        # runs; a pass on a tolerant build proves nothing about a strict one.
        import threading as _th
        _errs, _got = [], []

        def _hammer():
            try:
                for _ in range(12):
                    r = do_spatial("what is the climate in Cairo")
                    _got.append(tuple(p["label"] for p in r["places"]))
                    search("fallout shelters", 3, "keyword")
            except Exception as e:                          # noqa: BLE001
                _errs.append("%s: %s" % (type(e).__name__, e))

        _ts = [_th.Thread(target=_hammer) for _ in range(6)]
        for _t in _ts:
            _t.start()
        for _t in _ts:
            _t.join()
        if _errs:
            print("\n  !! concurrent access raised: %s" % _errs[0])
            bad += 1
        if len(set(_got)) != 1:
            print("\n  !! the same query gave %d different answers under load: %s"
                  % (len(set(_got)), sorted(set(_got))[:3]))
            bad += 1

        print("\n  spatial: %s names loaded; four probes, the distance, the "
              "payload contract, the two intents, the passage rule and %d "
              "concurrent queries agree"
              % ("{:,}".format(spatial.status()["names"]), len(_got)))

    # THE VECTOR HALF, AND ITS COLD START, WHICH IS THE PART THAT BROKE.
    # This node reported `0 dense` on every query for days with a 27 GB store on
    # the drive in perfect condition, and nothing failed the build, because
    # nothing ever asserted that the dense half returns anything. The footer said
    # `dense retrieval not loaded` truthfully every time, and a report that never
    # changes stops being read.
    #
    # The load is lazy and its outcome is sticky, so a process gets exactly ONE
    # cold start - which is why this fires it from four threads at once rather
    # than politely one at a time. That is the exact condition that failed:
    # chromadb registers a System in a class-level dict before starting it, and a
    # second thread inside that window takes an unstarted one. Measured 2026-09-08
    # at 2 failures of 480 unlocked and 0 of 480 with store.py's lock.
    #
    # SKIPPED, LOUDLY, WITHOUT THE KEEPER VENV. A node started with plain python
    # has no embedding stack, and keyword-only is a correct configuration rather
    # than a fault - so this says which of the two it is instead of failing a
    # build that is fine. It costs one bge-m3 load, which the first real hybrid
    # query would pay anyway.
    if (importlib.util.find_spec("chromadb")
            and importlib.util.find_spec("sentence_transformers")):
        import threading as _th
        _derr, _dn = [], []

        def _cold():
            try:
                o = search("groundwater in Nevada and Utah", 5, "hybrid")
                _dn.append(o["agreement"]["dense"])
            except Exception as e:                          # noqa: BLE001
                _derr.append("%s: %s" % (type(e).__name__, e))

        _dt = [_th.Thread(target=_cold) for _ in range(4)]
        for _t in _dt:
            _t.start()
        for _t in _dt:
            _t.join()
        # A FAILURE HERE HAS TWO CAUSES AND THEY NEED DIFFERENT ANSWERS.
        # bge-m3 takes CUDA when CUDA is there, and both models resident leave
        # 472 MiB free of 16,050. So this can fail because the race came back,
        # or because the selftest was run on a fully loaded card and there is no
        # room for the embedding model. Naming only the first would send the next
        # person hunting a concurrency defect that is not there.
        _hint = ("\n     Two causes look alike here: the cold-start race, or no "
                 "VRAM left for bge-m3. Both models resident leave ~472 MiB of "
                 "16,050. Run `python bin/ark.py status`, and if the models are "
                 "up, stop them and run this again before suspecting the lock.")
        if _derr:
            print("\n  !! concurrent cold start of the vector half raised: %s%s"
                  % (_derr[0], _hint))
            bad += 1
        if not S.dense_available():
            print("\n  !! the embedding stack is importable and the vector half "
                  "did not load: %s%s" % (S.dense_error(), _hint))
            bad += 1
        elif S.dense_backend() == "chroma" and S.pq_state() in ("stale", "error"):
            print("\n  !! the PQ index is %s and the node fell back to Chroma: %s"
                  % (S.pq_state(), S.pq_detail()))
            bad += 1
        elif not _dn or max(_dn) == 0:
            print("\n  !! the vector half loaded and returned no hits - the "
                  "collection or the chunk ids may not match the index")
            bad += 1
        else:
            # WHICH BACKEND ANSWERED, PRINTED EVERY RUN. It was in /api/health
            # and in the search payload from the start and NOT here, which is
            # the one place an operator actually looks - so the first run over a
            # PQ index could not be told from a run that had silently fallen
            # back to Chroma. PQ covers all 6,926,271 chunks and Chroma covers
            # what index-build.py loaded into it, 2,315,810 since the 09-14 pass.
            print("\n  dense: loaded under 4 concurrent cold starts, %d hits"
                  % max(_dn))
            print("         backend %s over %s vectors%s"
                  % (S.dense_backend() or "?",
                     "{:,}".format(S.dense_coverage() or 0),
                     "" if S.dense_backend() == "pq" else
                     "   <- NOT the PQ index; pq is %s%s"
                     % (S.pq_state() or "not loaded",
                        (": " + S.pq_detail()) if S.pq_detail() else "")))

            # AND WHETHER THE MATERIAL PASS 2 ADDED IS REACHABLE AT ALL.
            # See SELFTEST_PASS2 for why the five questions above cannot say.
            for _q, _want in SELFTEST_PASS2:
                try:
                    _p2 = search(_q, 5, "hybrid")
                except Exception as e:                      # noqa: BLE001
                    print("    !! pass 2 probe raised: %s: %s" % (type(e).__name__, e))
                    bad += 1
                    continue
                _hits = _p2.get("results") or []
                _from = [h for h in _hits
                         if _want in ((h.get("citation") or {}).get("artifact") or "")]
                _dense = [h for h in _from if h.get("from") in ("dense", "both")]
                if not _from:
                    print("    !! %-28s nothing from %s - the 4.6M chunks pass 2 "
                          "added may be counted but not reachable" % (_q[:28], _want))
                    bad += 1
                else:
                    print("    ok  %-28s %d from %s, %d of them dense"
                          % (_q[:28], len(_from), _want, len(_dense)))
    else:
        print("\n  dense: SKIPPED - no embedding stack in this interpreter, so "
              "this node is keyword only. That is a configuration, not a fault; "
              "start it from the keeper venv to check the semantic half.")

    up = llm.available("primary")
    out = do_answer("how much chlorine to disinfect drinking water", 3, mode)
    n_pass = len(out["retrieval"]["results"])
    if n_pass == 0:
        print("\n  !! the answer path retrieved nothing")
        bad += 1
    if up and not out.get("answer"):
        print("\n  !! primary is up and no answer came back: %s"
              % out.get("model_down"))
        bad += 1
    if not up and not out.get("model_down"):
        print("\n  !! primary is down and the payload does not say so")
        bad += 1
    # THE FIVE GROUNDING STATES ARE A CONTRACT WITH THE SURFACE AND THE PAPER.
    # `badge()` maps them to words, the card prints one, and the manual's label
    # table names them. A fifth state, or a rename, would render as a raw
    # identifier on screen and as an unexplained word on a printed card that
    # someone reads days later with no way to ask.
    if out.get("answer"):
        g = out["answer"]["grounding"]
        if g not in ("grounded", "uncovered", "unsourced", "grounded_plus",
                     "model_only"):
            print("\n  !! unknown grounding state %r - the surface and the "
                  "manual only know five" % g)
            bad += 1
        if "unsourced_citations" not in out["answer"]:
            print("\n  !! the answer payload lost unsourced_citations, which is "
                  "how a rule 3b violation reaches the operator")
            bad += 1

        # THE STATE ALONE IS NOT A CHECK, AND THIS PROBE PROVED IT.
        # The four-state contract above accepts `unsourced` and `grounded`
        # equally, so this question - a rule 9.2 toxicity dose - can flip
        # between them run to run and print as normal either way. On
        # 2026-09-09 it did: hybrid grounds on the Hesperian table (2 drops
        # per litre) and keyword at n=3 goes unsourced, offering 2-8 mg/L
        # free chlorine from the model's own recall.
        #
        # THAT WAS CORRECT, and the reason it was correct is the thing worth
        # asserting: none of the three passages keyword found carried a dose.
        # The failure this guards against is the opposite - the model
        # declaring NOT IN ARCHIVE and reaching for its own training WITH a
        # passage in front of it that carries the figure. That is the door
        # the 2026-09-09 prompt change opened, it would ship on a dosing
        # question, and nothing else here would notice.
        #
        # Categories, not just figures: a passage may carry a temperature or
        # a page number and still say nothing about the dose being asked for.
        # Only a finding in a category THE QUESTION raised counts as a
        # passage the model should have used.
        if g == "unsourced":
            q_cats = set(out["retrieval"]["safety"]["query"].get("categories")
                         or [])
            covered = []
            for i, r in enumerate(out["retrieval"]["results"], 1):
                pc = set(safety.assess(r.get("text") or "")["categories"])
                hit = pc & q_cats if q_cats else set()
                if hit:
                    covered.append((i, sorted(hit),
                                    (r.get("citation") or {}).get("title")))
            if covered:
                print("\n  !! the answer is `unsourced` and %d passage(s) in "
                      "front of the model carried a figure in a category the "
                      "question raised (%s). The model declared NOT IN ARCHIVE "
                      "with the answer on screen:" % (len(covered),
                                                      ", ".join(sorted(q_cats))))
                for i, hit, title in covered:
                    print("       [%d] %s  %s" % (i, ",".join(hit), title))
                bad += 1
            else:
                print("\n  unsourced probe: honest - none of the %d passages "
                      "carried a %s figure" % (n_pass, "/".join(sorted(q_cats))
                                               or "flagged"))
        else:
            # AND THIS CHECK HAS A HOLE, WHICH IS STATED WHERE IT IS READ.
            # It only looks when the run came out `unsourced`, and whether it
            # does is SAMPLED: llm.chat sends temperature 0.2, top_p 0.9 and no
            # seed, so this probe returns `unsourced` on some runs and
            # `grounded` on others from identical retrieval. Measured
            # 2026-09-09 over five runs of the same URL: 3 unsourced, 2
            # grounded, with keyword n=3 handing the model the same three
            # chunks (26:3:146, 29:74:35, 26:1:42) every time - verified
            # model-free, so retrieval is not the variable.
            #
            # The two answers are both defensible: the passages support a
            # qualitative answer (enough that free chlorine remains) and not a
            # number, so grounding the first and declining on the second are
            # both readings of the same evidence. THE DEFECT IS THAT AN
            # OPERATOR GETS ONE OR THE OTHER BY SAMPLING, on a rule 9.2
            # question, and llm.chat's own docstring says sampling variance in
            # a dose is a defect and not a feature.
            #
            # So this line is not decoration: a green selftest does NOT mean
            # the unsourced path was exercised. Fixing the sampling is its own
            # pass - a seed rather than temperature 0, measured across more
            # than one query - and until it lands, read this.
            print("\n  unsourced probe: NOT EXERCISED this run - grounding came "
                  "back %r. The state is sampled (temperature 0.2, no seed), so "
                  "the check above looked at nothing. Run again." % g)

    print("\n  answer path: %d passages, model %s%s"
          % (n_pass, "up" if up else "DOWN (retrieval unaffected, and said so)",
             (", grounding=%s, cited=%s" % (out["answer"]["grounding"],
                                            out["answer"]["cited"]))
             if out.get("answer") else ""))
    return bad


def main():
    global S
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8090)
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--mode", default="keyword",
                    choices=["keyword", "hybrid", "dense"],
                    help="selftest only; keyword needs no model and no Chroma")
    ap.add_argument("--unit", action="store_true",
                    help="only the checks that need no index and no model "
                         "(cancellation, ports, kit); what bin/ci.py runs")
    args = ap.parse_args()
    global NODE_PORT
    NODE_PORT = args.port

    # A CLONE HAS NO INDEX, AND CI IS A CLONE. --selftest needs the real
    # provenance and keyword stores below; these three do not, so they can run
    # on every commit (2026-10-04).
    if args.unit:
        bad = (llm.selftest() + ports_selftest() + kit_selftest()
               + corpuslib.selftest() + corpus_route_selftest())
        print("\n%s" % ("UNIT CHECKS FAILED: %d problem(s)" % bad if bad
                        else "unit checks passed"))
        sys.exit(1 if bad else 0)

    for p in (store.PROV_DB, store.BM25_DB):
        if not os.path.exists(p):
            sys.exit("missing %s\n  build it: python bin/index-provenance.py" % p)
    S = store.Store()

    # NUMPY WHOLE, ON THIS THREAD, BEFORE ANY REQUEST EXISTS. Added 2026-09-16.
    # store.preimport() is idempotent and the loaders call it too; doing it here
    # is what makes the race impossible rather than merely narrow, because after
    # this line no thread can find numpy half-initialized. Failure is REPORTED
    # AND NOT FATAL - a node with no numpy still serves keyword search, which is
    # the whole point of the lazy design - and it is reported at startup rather
    # than at the first dense query, which is the difference between a line in
    # the log everyone sees and a banner one operator sees hours later.
    try:
        store.preimport()
        _np_note = None
    except Exception as e:                                     # noqa: BLE001
        _np_note = "%s: %s" % (type(e).__name__, e)

    st = S.stats()

    # THE MAP VIEWER, IF IT IS ON THE DRIVE. Reported either way at startup,
    # because 843 GB of tiles - 38% of this archive - appeared in no procedure at
    # all until 2026-09-06, and a capability nobody is told about is one the node
    # does not have.
    global MAPDIR
    MAPDIR = os.path.join(store.ROOT, "09-software", "map-viewer")
    map_ok = os.path.isfile(os.path.join(MAPDIR, "viewer.html"))

    print("ark-node  archive=%s" % store.ROOT)
    print("          index: %s artifacts, %s documents, %s chunks"
          % ("{:,}".format(st["artifacts"]), "{:,}".format(st["documents"]),
             "{:,}".format(st["chunks"])))
    # THE NUMBER THE OPERATOR CAN ACTUALLY BE ANSWERED FROM.
    # Printing 2,315,810 while searching 1,456,944 would be accurate about the
    # index and misleading about the node - the exact shape `ark.py status` was
    # fixed for on 09-11, where "VRAM free 16,050 MiB" was true and meant the
    # opposite of what it read as. Both numbers, and what was removed, or one
    # line saying the filters are off.
    # PRINTED EITHER WAY, ADDED 2026-09-17. Until now this said something only
    # when the warm FAILED, so a clean log could not show that it had happened -
    # the wrong shape for the one thing in this file that failed invisibly for
    # three hours. A check whose success is silent is indistinguishable from a
    # check that did not run, which is NODE-ARCHITECTURE section 7 item 6 and
    # the reason safety_detector reports itself on every health call.
    #
    # THE POSITIVE LINE CLAIMS ONLY WHAT IT KNOWS. numpy imported is not dense
    # retrieval working: faiss, the 443 MB index and bge-m3 are all still ahead
    # and all still lazy. Saying "dense retrieval available" here would be the
    # `ark.py status` note this build just finished removing, rebuilt in a new
    # place on the same day.
    if _np_note:
        print("          NUMPY DID NOT IMPORT: %s" % _np_note)
        print("          the node serves KEYWORD ONLY until that is fixed")
    else:
        print("          numpy %s imported on the main thread; the dense half "
              "is lazy" % (store.numpy_version() or "(version unknown)"))

    ex = st["excluded"]
    if ex["off"]:
        print("          filters: OFF (ARK_NO_FILTERS=1) - all %s chunks searchable"
              % "{:,}".format(st["chunks"]))
    else:
        print("          search: %s chunks after filters (-%s, %.1f%%): %s"
              % ("{:,}".format(st["chunks_searchable"]),
                 "{:,}".format(ex["chunks"]),
                 100.0 * ex["chunks"] / max(st["chunks"], 1),
                 ", ".join("%s %s" % (k, "{:,}".format(v))
                           for k, v in sorted(ex["by_rule"].items()))))
        # A MIRROR COUNT OF ZERO MEANS TWO OPPOSITE THINGS, so it is never left
        # to be read off the number. Absent or stale is a degraded node: two
        # copies of every shared medical article compete for the same five
        # slots, which is what this filter exists to stop.
        if ex.get("mirror_state") not in ("ok", None):
            print("          mirror: %s" % ex["mirror_detail"])
    print("          map:   %s"
          % ("/map/  (needs `pmtiles serve` on :%d for tiles)" % ports()["tiles"]
             if map_ok else
             "NOT PRESENT - 09-software/map-viewer/viewer.html is missing"))
    print("          home:  %s"
          % ("/home/  (every service, checked from the reader's browser)"
             if os.path.isfile(os.path.join(WEB, "home.html")) else
             "NOT PRESENT - 13-ark-node/ark-web/home.html is missing"))

    if args.selftest:
        bad = selftest(args.mode)
        print("\n%s" % ("SELFTEST FAILED: %d problem(s)" % bad if bad
                        else "selftest passed - every hit resolved to a citation"))
        sys.exit(1 if bad else 0)

    print("          kiwix expected at %s" % store.KIWIX)
    print("          serving on http://%s:%d/  (Ctrl-C to stop)\n"
          % (args.host, args.port))
    Server((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
