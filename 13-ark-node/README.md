# ark-node — the node's own surface

**Kind:** APPLICATION · **Canonical copy:** `13-ark-node\` in the archive
**Implements:** spec §8.5 (stand up the interfaces), §9.2 (verify against the
archive), §9.3 (prefer the archive directly for factual lookup)
**Reads with:** `00-docs/NODE-ARCHITECTURE.md` (the design this follows and the
two places it departs from it), `00-docs/ROADMAP.md` (§8 sequencing).
**Started:** 2026-09-04.

> **Where this sits.** One of three parallel workstreams. `00-docs/WORKSTREAMS.md`
> is the overview. Figures of record live in `WORKSTREAMS.md` §1–2.

---

## What works today

**You can ask the archive a question and get an answer grounded in it, with every
claim carrying a link to the page it came from - and a second model family
checking the figures.**

Search 1.6 TB, read the passages themselves, or read an answer built from them.
All three at once, side by side, with no network.

    python 13-ark-node/ark-api/serve.py
    #  -> http://<node>:8090/

Keyword search over **39,073,563 chunks** across **12,624,636 documents** in **50
artifacts** after pass 3 (2026-09-25), 38,347,877 of them searchable after the
query-time filters, each hit resolved to a citation: a `kiwix-serve` URL for the 44
ZIM artifacts, and a file URL with a page anchor for the six PDF folders.
**Sub-second at pass 1's 2.3M chunks; not any more.** On 2026-09-25 a warm hybrid
query took 7.9 to 10.6 s and dense alone 2.3 s, so the keyword half and fusion
account for roughly 5.6 to 8.3 s — derived by subtraction, not timed on its own.
*(This paragraph carried pass 1's 1,590,124 / 942,032 / 29 and the word
sub-second until 2026-09-25.)*

**The source pane needs no model, no vector store, and nothing installed.** BM25 lives in SQLite
FTS5 and `sqlite3` ships with Python. That is the point rather than a limitation —
see *Why the standard library* below.

**Every result is also scanned for the figures rule 9.2 says to verify** — doses,
voltages, pressures, temperatures, tolerances, structural loads, toxicity limits
and part numbers, in English and Spanish. Passages carrying one are marked, the
figures themselves are highlighted in the text, and a question that carries one
raises the verify banner before the results. `safety.py`, standard library, no
model. See *The safety-term detector* below for what it does not catch.

## The safety-term detector

`ark-api/safety.py`. One component, three consumers, which is the whole reason it
is a file rather than three conditions in three places:

| Rule | Consumer | Threshold |
|---|---|---|
| §9.5 | render as text, never audio alone | any finding at all — suppressing audio is free |
| §9.4 | trigger the model cross-check | a unit that means what it says — a model swap is not free |
| §9.2 | the "verify this against the archive" prompt | any finding, and it names the figures |

It returns a level (`none`, `possible`, `present`) and the spans, not a boolean,
because those three lines are in different places and a boolean would force one of
them to be wrong.

**It reads intent, not only figures.** *What dose of amoxicillin for a child*
contains no unit at all, and span matching found nothing in it — while every
passage that came back carried one. A question that ASKS for a dose reaches
`possible` on its vocabulary alone, and a firm finding in the results raises the
banner regardless of the question. The cross-check line does not move: a retrieved
passage is already the archive.

**What it does not do.** It does not know whether a figure is correct, safe, or
real. It answers one question — *is this the kind of statement rule 9.2 says to
check before acting* — and a green result is not a clearance.

**Measured on the corpus, not on examples.** Over 200 random passages from each of
29 artifacts: WikEM 18%, ham radio 15.5%, `zimgit-medicine` 12.5%, Hesperian 10%,
against cooking 1%, pathology 1%, gardening 1.5%. A detector that fired evenly
everywhere would be measuring nothing.

**Three false-positive classes were found by that measurement and are fixed**, all
the same shape: a context word that is also an ordinary word somewhere else in a
1.6 TB archive. `"dar"` matched inside *standard*; `"nut"` lifted `CD34` out of
*NUT carcinoma*; `"load"` lifted every weight in the USDA canning guide, because a
canner load is a batch of jars. The bare-number rule was deleted outright — it was
three quarters of all findings and inspected, it was chemistry infoboxes and figure
numbers.

## The answer pane, and the cross-check

**Built 2026-09-05.** **Since 2026-09-27 the model is a source of knowledge that
checks itself against the archive** (Juan's decision; `00-docs/DESIGN-knowledge-first.md`).
Until then it was *"a reader of the archive, not a source"* and was told to be
brief. It still receives the retrieved passages numbered, still cites them
inline, and **an answer it produces without a citation is still labelled rather
than shown plainly**. What changed is the voice and the length: both the cited
part and its own block are asked to be complete. `ARK_ANSWER_PROMPT=reader`
restores the old prompt, for measuring one against the other.

    grounded     cited at least one of the passages it was given
    uncovered    said NOT IN ARCHIVE. A GOOD outcome - §11.2 says retrieval
                 quality bounds the answer, and admitting a gap beats filling it
    unsourced    said NOT IN ARCHIVE and THEN offered its own knowledge in a
                 marked block. The operator is shown "Not in the passages
                 found" and NOT "not in the archive" - the model reported on
                 the n passages it was given, and the index holds 2,315,810
                 it did not see. On a keyword-only search the block also says
                 which half ran, because that is the half most likely to miss
                 a passage the archive holds. The admission always comes first, so the operator
                 reads "the archive does not cover this" before reading anything
                 unverified. Cross-check is mandatory, the safety prompt names
                 the absence instead of telling anyone to check a page that does
                 not exist, and a [n] inside the block is a rule 3b violation
    grounded_plus  cited the passages AND added a block of its own knowledge.
                 Since 2026-09-11 rule 3b asks for that block on EVERY answer, so
                 this is the ordinary state for a grounded answer and bare
                 `grounded` is the exception. The block is plain and open, not
                 folded: it appears every time, so its presence carries no
                 information, and the red is spent on the model declaring it
                 CONTRADICTS the archive
    model_only   answered with no citation AND no admission. Shown with a
                 warning, never plainly. Still a fault - the test was never
                 "was there an admission", it is whether the block stands alone

**Invented citations are dropped and counted**, on screen and on the printed card.
A model citing [7] when five passages were supplied has manufactured a source, and
rendering that chip would be manufacturing provenance.

**`[3]` is a control.** It scrolls the source pane to that passage and flashes it,
which is §9.2 as one click rather than as a discipline. The source pane is
re-rendered from the ANSWER's own payload so chip *n* and passage *n* are the same
object by construction - an earlier version asked for 8 passages while answering
from 5, and a chip could point at a passage the answer never cited.

### Two models, two memories, one code path

    primary      Qwen3.8-27B IQ4_XS   GPU, ngl 64    ~25 t/s   127.0.0.1:8091
    crosscheck   Gemma 4 31B IQ4_XS   CPU, 64GB RAM  ~3 t/s    127.0.0.1:8092

30.5 GiB of 64 GB, both resident, **no model swap, and both at the same
quantization**. §9.4 warns that where the two families run at different
quantization levels some disagreement is compression noise; that caveat is removed
here rather than managed. Gemma 4 31B is 14.82 GiB at its smallest quantization
against 14.69 GiB of free VRAM, so it could never have shared the GPU - being
forced into system RAM is what let it keep its quantization.

### The cross-check is a job, not a request

An answer lands in about 18 seconds; the second opinion needs about **four
minutes** - 600 tokens in 241.61s measured 2026-09-11, where the ~3 t/s above is
generation alone and prompt evaluation of five passages on a CPU is the rest. If
the answer waited for it, every question would take four minutes and §9.4 would
be switched off by the first impatient operator. It runs while the operator reads
and arrives as a state change.

**It runs on every answer** (2026-09-11). The threshold that rationed it was
removed once cancellation made an abandoned check free: asking a new question
aborts the one in flight by closing the socket, which frees Gemma's single slot
within a second or two. And the second model's **answer** is shown, not only the
verdict - it spends four minutes writing one.

**It compares figures, not prose**, using the same detector that triggered it.
Comparing two free-text answers for semantic agreement needs a third model and a
judgement; comparing the doses, voltages and pressures each one states needs
neither, and is the class §9.2 cares about. **What it misses is subtle semantic
disagreement with no numbers in it**, and that limit is stated in `answer.py`
rather than discovered later.

**Its output is never a verdict.** Four states - agree, disagree, both
unsupported, no basis - and the pane says under every one of them that neither
model is authoritative. *Both unsupported* is the most reassuring result available:
two families independently declining to invent an answer, which is the opposite of
§11.1's two fluent models confidently wrong together.

### The printable card

§9.5 and `NODE-ARCHITECTURE.md` §4: *paper survives the node.* Question, answer,
sources with their URLs, local timestamp, and **the cross-check state named
explicitly in all four cases** - including *STILL RUNNING when this was printed*.
A card printed before the second opinion finished must not look like a card where
none was needed, because paper cannot update itself.

## The map pane

A question that names a place gets its coordinates beside the answer, and the
offer is fetched separately from the answer on purpose. `/api/answer` needs the
primary model and takes about twenty seconds; `spatial.classify` needs no model
and returns in milliseconds. Carried on the answer payload the offer arrived
last and disappeared entirely when no model was running, which is the moment an
operator most needs a coordinate they can act on. §3.4 says nothing that gates
retrieval may depend on a model, so the surface calls `/api/spatial` itself,
in parallel with the search.

**Two shapes, because it is two different things.** `spatial` means the question
asked about the place and gets a card: label, coordinates, kind, the great-circle
distance when two places are named, the `koppen-lookup.py` command with a copy
button, and a **Show it here** button. `place` means the question was merely
anchored on a place and gets one quiet line. Those fire on 0.042% and 0.783% of
83,856 real questions respectively, so the second is 86 times more common, and
giving it a card would put a map beside every passing mention of a country.
`bin/measure-spatial-cues.py` is where both numbers come from.

**The map opens on demand, never by itself.** The button builds an iframe of
`/map/#lat,lon,zoom` inside the pane. Nothing loads until it is pressed, which
is the difference between a link and a MapLibre instance plus terrain tiles on
every question. The zoom follows what the place is: country 5, region 7,
locality 11. The viewer reads `location.hash` once at load and has no
`hashchange` listener, so moving the map rebuilds the frame rather than
rewriting the fragment.

**Ambiguity is shown rather than resolved quietly.** Cairo answers to two places
here and Franklin to six. The tiles carry no state or country, so there is no
honest way to write *Franklin, Wisconsin* from what is stored; the alternates
are offered as coordinates that move the map, and the operator recognises the
place by seeing it. `cairo` resolving to Georgia is a defect this build actually
shipped, and the count alone gave nobody a way to correct it.

**Every state says what to do.** No gazetteer names the error and the rebuild
command. No tile server disables the button with the reason attached rather than
handing over a grey rectangle. No rasterio prints the note `spatial.card` already
carries. No model changes nothing here at all.

The printable card gains the label, the coordinates and the climate command, and
deliberately not the map URL: §8.10 is about what survives with no screen, and a
coordinate can be read off paper into a GPS while a `/map/` link cannot be
followed at all.

### Places in the passages

A retrieved passage that is itself about somewhere carries a dashed chip in its
row of facts, beside the artifact and chunk numbers. Clicking it moves the map.
It fires on **8.5%** of chunks, so roughly one passage in twelve has one, which
is why it is a chip and not a card.

**It is a different problem from the question side and the numbers say so.**
`mentions()` rests on capitalisation, and that holds for questions because they
are sentence-cased. Run unchanged over passage text it fires on **49.8%** of the
archive, led by `Phillips` the screwdriver, `Salt` and `Since` and `Page` at the
starts of sentences, `Para` and `Una` in Spanish, and `FAO` in a corpus published
by the FAO. `spatial.in_passage()` is what closes that gap, and every condition
in it cost something measurable:

| gate | fires on |
|---|---|
| `mentions()` as shipped | 49.8% |
| `+ is_place_reference()` | 15.6% |
| `+ the capital was not free` | 14.4% |
| `+ not a 3-capital acronym` | 13.8% |
| `+ poprank >= 10` | 8.9% |
| `in_passage()` | **8.5%** |

`bin/measure-spatial-cues.py --passages` reproduces the whole cascade offline
from `10-index/chunks`, sampling uniform by line against a fixed seed.
`--derive` re-counts the `AMBIGUOUS` exclusion list from the archive: names the
corpus mostly writes in lower case, so a capital is an accident of position.
`colon`, `mobile`, `springs`, `turkey`, `lima`, `guinea` and four others, each
with its measured share beside it.

Known and asserted: `Bulletin No. 138, Rome` is a publisher's address that
carries exactly the punctuation `Kathmandu, Nepal` does. `--selftest` expects it,
with the reason, so a better rule makes the check fail rather than quietly
inheriting the compromise.

## The dense half, what it needs, and what it cost

The **dense half** of retrieval works, and loads on first use. **As of 2026-09-15
it is a flat faiss PQ64 index with an exact rerank over the float16 `.npy`
vectors** - `10-index/pq/`, **2,501,756,694 bytes at 64.0 measured bytes per
vector, covering all 39,073,563 chunks** after pass 3 (rebuilt 2026-09-25; it was
443.5 MB over 6,926,271 from 2026-09-15). Chroma held only pass 1's 2,315,810 at
4,287 B/vector and was deleted on 2026-09-16; `store.py` keeps the named fallback path, and
`dense_backend` says which one answered.

It needs `numpy`, `faiss-cpu` and `sentence-transformers`, which live in the
keeper venv and not in the archive's plain Python, so **the interpreter the node
is started with decides whether it has a semantic half at all**:

    <ARCHIVE>\.venv\Scripts\python.exe 13-ark-node\ark-api\serve.py
    # then choose "hybrid" in the mode selector

`bin/ark.py` finds the keeper venv, prefers it, and prints which interpreter it
chose. If the imports are not available the layer reports that on `/api/health`
and in the footer and keeps working on keyword alone, which is §3.4 rather than
an apology.

**The load is lazy and its outcome is sticky.** One attempt is made on the first
hybrid or dense query and the result is remembered, so a failed load leaves the
process keyword only until it is restarted. That is deliberate: re-importing a
missing stack on every query is worse on the machine that genuinely lacks one.

It is also why the load is taken **under a lock**. Until 2026-09-08 it was not,
and `serve.py`'s Server is `ThreadingMixIn`. chromadb registers a `System` in a
class level dict keyed by the persist directory *before* it starts it and does not
lock that dict, so two handler threads in one cold start could collide — measured
here at 2 failures of 480 unlocked, 0 of 480 locked. Combined with the sticky
flag, one unlucky start meant **days of `0 dense` with an intact 27 GB store on
the drive**, reported honestly in every footer and read by nobody, because a
report that never changes stops being read. `--selftest` now fires four concurrent
first queries and asserts the vector half returns hits. Nothing asserted it
before.

---

## What the node does not index, and what it does about it

Retrieval covers **25 of the 67 ZIM artifacts** on the corpus shelves, plus four
PDF folders. The other 42 are 737 GB the node could not see, and until
2026-09-12 a question they would have answered returned *NOT IN ARCHIVE* with no
hint that the material was on the drive. Wikipedia EN and ES, Stack Overflow,
Gutenberg, Wiktionary and Khan Academy are all in that set.

`store.unindexed()` reads the library `kiwix-serve` was actually started with,
subtracts the artifacts the index covers, and the surface prints one quiet line
under a short result page naming what is left and linking into Kiwix full-text
search for the same question.

**A link, not a merge, and the reason is §9.2.** A Kiwix full-text hit has no
chunk, no provenance row and no byte offset, so it cannot carry a citation the
way every result in the sources pane does. Folding those hits in beside cited
ones would make the GROUNDED badge mean two different things on one screen, which
is the defect this build spent 09-09 to 09-11 removing from the answer pane. The
operator is handed a real search over the rest of the archive and the citation
contract is left intact.

**One link per language, and that is not tidiness.** kiwix-serve 3.8.1 requires a
book filter on `/search` and accepts exactly one language:

    /search?pattern=X                          400
    /search?books.filter.lang=&pattern=X       400
    /search?books.filter.lang=eng,spa&...      400
    /search?books.filter.lang=mul&...          400
    /search?books.filter.lang=eng&pattern=X    200

Repeating the parameter answers **200** and silently discards the second value,
which is how it nearly shipped. Mapping results back to their books is what
caught it: `eng` repeated with `spa` reached the same three English books as
`eng` alone, while `spa` alone reached five including `wikipedia_es_all_maxi`.
A 200 is not coverage. Nine of the 67 books are Spanish, so a single `lang=eng`
link would have hidden every one of them from a Spanish-speaking operator, which
is the wikem translation defect arriving from the other direction.

**It fails closed.** A zero here has four causes and they are not the same fact:
no library recorded, a stale library stamp, a library path that is not on this
drive, and a library that will not parse. Each is named rather than collapsed
into one number, and the line is suppressed rather than guessed at. The first
version returned 0 for all four and a real bug hid behind it, because
`kiwix-books.json` records the server URL as well as the library path and the
wrong one was being tested for existence, so 67 books read as none.

**The line only appears when it is useful**, which is when the page came back
short. A full page of five cited results does not need to be told what else
exists.

---

## Layout

    13-ark-node/
    |-- README.md          this file
    |-- RECOVERY.md        the printed procedure (8.10). Generic; names no machine
    |-- RUNBOOK.md         how to run it HERE. Real paths, NOT in the repository
    |-- .gitignore
    |-- ark-api/
    |   |-- store.py       retrieval, provenance, citation resolution
    |   |-- safety.py      the safety-term detector (§9.2, §9.4, §9.5)
    |   |-- llm.py         transport to the two llama-server instances
    |   |-- answer.py      the grounded prompt, citations, the cross-check
    |   |-- spatial.py     does the question name a place, and is it asking about it
    |   `-- serve.py       the HTTP layer and the selftest
    `-- ark-web/
        |-- index.html     the three-pane shell, one file, no build step
        |-- home.html      every service on the node, and whether it is up (2026-09-27)
        `-- services.js    the one list of services, read by both pages

`bin/index-provenance.py` builds the store this reads. It lives in `bin/` because
it is an index-build tool alongside `index-build.py` and `index-bm25.py`, not part
of the running application.

## The two models, and what runs without them

The source pane needs **no model at all**. The answer pane needs the primary; the
cross-check needs the second. With neither running, search still works and the
surface says so rather than going quiet - a node whose answer pane falls silent
while the sources keep working teaches the operator that silence means the archive
has nothing, which is a different and much worse statement.

    python 13-ark-node/ark-api/llm.py --probe      # are both answering?

**You cannot re-index while the primary is loaded.** Qwen holds 14.5 GiB and
BGE-M3 needs another 2.3, and 16 GiB does not hold both. Stop the primary before
any `bin/index-build.py` run and start it again afterwards. The cross-check is CPU
only and can stay up throughout.

The exact commands, with this machine's paths, are in `RUNBOOK.md`, which is
deliberately outside the repository.

## Routes

| | |
|---|---|
| `GET /` | the shell |
| `GET /api/health` | what is loaded, what is degraded, and it says which. Includes the gazetteer under `spatial` |
| `GET /api/search?q=&n=&mode=keyword\|hybrid\|dense` | results with citations |
| `GET /api/passage?cid=` | one passage in full |
| `GET /api/answer?q=&n=&mode=` | retrieve, answer, start the cross-check if it is worth its cost |
| `GET /api/crosscheck?job=` | poll the second opinion. `running` / `done` / `unavailable` / `error` |
| `GET /api/spatial?q=` | does this question name a place, and which one. No model, no index |
| | `/api/search` and `/api/answer` results each carry `places`, the passage's own |
| `GET /files/<archive path>` | the four PDF artifacts nothing else serves |
| `GET /map/` | the map viewer, served out of `09-software/map-viewer` so there is no fourth server |
| `GET /home/` | every service on the node, one card each, with whether **this browser** can reach it. Also the *Services* menu in the search page's header (2026-09-27) |

## Starting it

    python bin/ark.py status        what is up, what is not, and why
    python bin/ark.py up            start what is missing
    python bin/ark.py commands      print every command, start nothing

Five servers on five ports: kiwix on 8080, tiles on 8081, this on 8090, and the
two models on 8091 and 8092. `bin/ark.py` starts whichever are not already
answering, refuses the models when the machine cannot hold them and says the
numbers, and names the missing binary and its fix when something has not been
extracted. `/home/` shows all five from the reader's side, and draws the two
model chat pages apart from the rest: they answer from memory with nothing looked
up, and they accept connections from the node itself only, so from a phone they
read *this computer only* rather than offering a link that cannot work. Since the
same change, citation links opened from a phone name the node's address instead
of `localhost`, which on a phone is the phone. **It is a convenience, not the source of truth** - `RUNBOOK.md` is,
and `ark.py commands` prints every line so the launcher is never the only way
in. Standard library, one file, no build step, for the same reason this surface
has none.

## Verifying it

    python 13-ark-node/ark-api/serve.py --selftest

Runs the same five acceptance questions `bin/index-query.py` uses, through the
API's own code path, and **fails if any hit cannot be turned into a citation** —
which is the one property this layer exists to provide. Passed 5/5 on 2026-09-04,
keyword mode, on a machine with no embedding model loaded.

---

## Two departures from `NODE-ARCHITECTURE.md`, both deliberate

That document is explicit that its own proposals stand only until §7 is amended.
These are the two amendments this application asks for.

### 1. The provenance store was not missing. The way to read it was.

§3 and §5 say a provenance store is missing and that §9.2 depends on it. That was
written 2026-08-30, **before the index existed**. Since 2026-09-02
`10-index/sources.json` has carried a `kiwix_book` for all 25 ZIM artifacts, every
chunk row has carried `path`, `title` and `i`/`n`, and PDF chunks have carried
`page`. Chunk to artifact to a resolvable URL was already derivable.

What was actually missing is that **`index-query.py` reads its metadata from
Chroma**, so a keyword lookup — which needs no embedding model at all — still paid
for a 35 GB store and the `chromadb` package. That coupling is wrong three ways:
power (constraint 3.1 is priority one), graceful degradation (§3.4: if Chroma is
absent or being rebuilt the archive should still be searchable *and citable*), and
dependencies.

`bin/index-provenance.py` fixes it with a 148 MB sidecar. It stores identifiers and
**a byte offset into `10-index/chunks/*.jsonl`**, so a passage is one seek away and
the text is never copied. 148 MB rather than 3.4 GB, because it is a pointer.

### 2. No FastAPI. The standard library only.

§6 proposes FastAPI. FastAPI pulls starlette, pydantic, **pydantic-core** (a
compiled Rust extension), anyio, sniffio, typing-extensions and a server — a wheel
set to acquire, pin, verify and re-acquire for macOS/arm64, which is the work the
geospatial toolchain took two days over. Against that:

* **§5.5.4** requires everything vendored, with no install step that needs a
  network. `http.server` is already vendored, in every Python that exists.
* **§3.3** ranks repairability with commodity parts and **§8.10** requires a second
  person to rebuild this from printed instructions.
* It buys nothing here. Nine routes, no auth, no validation beyond integers, one
  user.

If the node ever needs concurrency this cannot give, that is the moment to
revisit — and by then it will be replacing something that works.

---

## Running kiwix-serve, which the ZIM citations need

A citation to one of the 25 indexed ZIM artifacts is a link to `kiwix-serve`.
Without it running, the passage still shows and the link goes nowhere. The PDF
citations need nothing: `ark-api` serves those itself.

**The library holds 67 books, not 25.** Since 2026-09-12 it covers every corpus
shelf, `02-corpora-core` through `07-corpora-supplemental`, so the 42 artifacts
retrieval does not index are still reachable. See *What the node does not index*
below.

    # once, to build the library - it lands at the ARCHIVE ROOT
    python bin/kiwix-library.py

    # then serve it, with an absolute path in the platform's own form
    kiwix-serve --port 8080 --library "$(cygpath -w "$ARK/library.xml")"

    # then record the keys the server actually answers to
    python bin/kiwix-library.py --verify-against http://localhost:8080

**What is stored inside the library is load-bearing, and the rule changed.**
`kiwix-serve` derives each book's URL key from the path stored in the library,
and a **relative** path is resolved against the library file's own directory. The
rule recorded here on 2026-09-04 concluded from that the library must live beside
the ZIMs, because a relative multi-segment path such as
`07-corpora-supplemental/mdwiki_….zim` yields a key no spelling can address: the
book appears in the catalogue and every article 404s while the server reports
success.

That half is still true. What was never actually tested was the **absolute**
path, because `kiwix-manage` relativises whatever it is given, `--zimPathToSave`
included, so the tool could not produce one. `bin/kiwix-library.py` now writes
the library itself and rewrites every path to absolute:

| stored path | book | article |
|---|---|---|
| bare filename, library beside the ZIMs | opens | opens |
| `<shelf>/<file>.zim`, library at the root | opens | **404** |
| absolute, library anywhere | opens | opens |

An absolute path is opened directly and the key is the bare **basename**, which
is why one library at the root can serve six shelves at once. `_assert_absolute_paths`
and `_assert_unique_keys` replaced the two guards that enforced the old rule and
would now reject a correct library. Measured against a running kiwix-serve 3.8.1
by `bin/kiwix-probe-shelves.py`; full account in `RUNBOOK.md` and `DECISIONS.md`
2026-09-12.

**The cost of absolute paths is that the library is machine-specific**, and it is
excluded from `CHECKSUMS.sha256` so nothing warns you. On a restored archive,
rebuild it before serving anything. `RECOVERY.md` step 2 says so in the place
somebody rebuilding this will actually be reading.

`kiwix-serve` is vendored, not installed: extract the archive for this platform
from `09-software/kiwix-tools/` first. `bin/kiwix-library.py` finds it there
without being told where.

If `ARK_KIWIX` is set, `ark-api` uses it; otherwise it assumes
`http://localhost:8080`. On a node where kiwix runs on the secondary machine, set
it: `ARK_KIWIX=http://secondary:8080`.

## Three things confirmed rather than assumed

**The kiwix URL shapes were read out of the vendored binary**, not from
documentation for some other version. `09-software/kiwix-tools`, kiwix-serve - 3.8.1 for Windows, which is the build
this machine runs, and 3.8.2 for the Linux tarballs beside it:

    raw content     <root>/content/<book>/<path>
    viewer chrome   <root>/viewer#<book>/<path>

**The book key is the ZIM's filename stem, and this was got wrong once.**
`kiwix-manage` reports a book `name` from ZIM metadata — `appropedia_en_all` —
which disagrees with the filename stem for all 25 artifacts here. That looked like
proof every citation was broken, and the "fix" was applied. A live server then
said the opposite:

    /content/appropedia_en_all              404   <- the "corrected" name
    /content/appropedia_en_all_maxi_2026-02 200   <- the original

`kiwix-serve` builds a book's URL from its file path. Its own catalogue prints both
fields next to each other — `href="/content/mdwiki_en_all_maxi_2025-11"` against
`<name>mdwiki_en_all</name>` — and only one of them is a URL. **The check that
settled it was fetching the URL from the server that serves it**, and
`bin/kiwix-library.py --verify-against` is now that check, run against a live
server rather than against `library.xml`, which is an input to the server and
cannot say what the server will do with it.

**Sixteen distinct ZIM citations were fetched from a running kiwix-serve on
2026-09-04: 16 of 16 returned 200**, including `Are_You_Ready?/Floods` and the
in-ZIM PDFs whose paths carry spaces and parentheses. Confirmed again on the
Windows node the same day, from the surface itself: *Headache Disorders* opened
from a citation in the source pane.

**And the count of wrong answers before that, because it is the more useful
figure: four.** The book `name` from `kiwix-manage` (ZIM metadata, never in a
URL); a 3.8.1 versus 3.8.2 difference (asserted, then withdrawn as unsupported);
the path separator; relative versus absolute `--zimPathToSave`. The last two are
real and decide whether the ZIMs open at all, but on 2026-09-04 only the
library's location decided whether articles resolve. Every wrong answer was
reached by asking something adjacent to the program that actually serves the
citation.

**And the fourth of those was withdrawn too early, which is the 2026-09-12
correction.** Relative versus absolute was dismissed as untestable because
`kiwix-manage` relativises every path it is given. That was a fact about the
tool, not about the server, and it was recorded as though it were about the
server. Writing the library directly instead of through `kiwix-manage` made the
absolute form available, and the absolute form resolves articles from any
location. **The location only mattered because the paths were relative.** Five
wrong answers, then, and the fifth was a true statement about a measurement that
had never been taken.

**Paths must be percent-encoded, and two real entries prove it.** Appropedia has
`Are_You_Ready?/Floods` — a literal `?`, which starts a query string the moment it
lands in a path, so the unencoded URL 404s. FAO has
`Irrigation/Respuesta del rendimiento de los cultivos al agua.pdf` — spaces. Both
were found in the first four results of the first three test questions, which is
the argument for testing against the real corpus rather than a fixture. Separators
are left alone, matching what kiwix-serve's own viewer calls a
`quasiUriEncodedPath`.

---

## Known couplings and open items

1. **The stoplist is imported from `bin/index-bm25.py`, not copied.** A second copy
   would drift, and that is the file where drift is least acceptable — it is
   hand-tuned for this corpus, where `well` is a water well and `can` is a jerry
   can. When this becomes its own repository, `bin/` moves in as `ops/` per §6 and
   the import becomes local.
2. **Cross-artifact duplicates are not suppressed.** *Where There Is No Doctor*
   appears from both `zimgit-medicine` and `zimgit-post-disaster` and both are
   shown. Deliberate: `index-filter.py` already recorded that cross-artifact dedupe
   reduced `zimgit-water` from 355 chunks to 1. Two copies shown is better than one
   corpus silently emptied.
3. **Keyword-only results are keyword-only results.** "How do I treat a burn"
   returns *Hydrofluoric acid* at rank 2. That is what BM25 does without a semantic
   half, the mode selector says which mode produced it, and the agreement line says
   `single` rather than pretending to a confidence it cannot have.
3a. ~~**Retrieval covers 18.2 GB of the 755 GB the library serves.**~~
   **SUPERSEDED 2026-09-15, and the whole of this item is now wrong, which is why
   it is struck rather than edited.** It said retrieval covered 18.2 GB, projected
   the two Wikipedias at ~22M chunks from one artifact's density, and called the
   quantized index unbuilt. What actually happened:

   - **Coverage:** 48 artifacts, **6,926,271 chunks**, 42 of the 67 served ZIMs
     indexed and 25 searchable only in Kiwix. **After pass 3 (2026-09-25): 50
     artifacts, 39,073,563 chunks, 44 of 67 indexed and 23 Kiwix-only** — the two
     Wikipedias are in; Stack Overflow is the largest thing still out. The inversion of the figure this
     item carried.
   - **The projection was 5x low.** Measured, `02-corpora-core` is **112,347,518
     chunks**, because `docs per GB` does not transfer between a medical corpus
     and a general one even at the same publisher and packaging.
   - **The escape hatch was built.** Flat faiss PQ64 plus an exact rerank,
     `10-index/pq/`, **443.5 MB at 64.2 measured B/vector**, `ARK_RERANK_DEPTH`
     400 measured at r@5 0.995 against exhaustive search. **After pass 3:
     2,501,756,694 bytes at 64.0 B/vector over 39,073,563, and the depth not yet
     re-measured at that size.** Chroma's 29.7 GB was
     deleted on 2026-09-16.
   - **The caution about Stack Exchange density was right.** Those artifacts do
     span 72,113 to 642,649 chunks per GB, and refusing to derive a Stack Overflow
     figure from that spread is the same judgement `index-measure.py` later
     encoded as three separate terms rather than one blended rate.

   See `00-docs/DESIGN-vector-index-at-scale.md` and its two postscripts.

4. ~~**No answer pane, no map pane, no voice, no cross-check.**~~ **The answer
   pane and the §9.4 cross-check were built 2026-09-05** and verified in a browser
   against a live index: a dose answered, grounded, cited, and the two model
   families compared at `dose (mg/kg): 45 / 45`. **The map pane was wired to the
   gazetteer 2026-09-07.** Voice (§8.6) and the secondary node (§8.7) remain
   open.

   The pane's first real query also produced this build's most serious finding: a
   grounded, correctly cited, DANGEROUS answer, because PDF table extraction had
   destroyed the row pairing in a bleach dosing table. Provenance was perfect and
   the content would have hurt someone. **Grounding is not correctness**, and the
   two are easy to confuse precisely because the interface shows the first. See
   BUILD-LOG, *The first dangerous answer, and it was grounded*.
5. **Version control, decided 2026-09-04.** This directory is the authored copy;
   the git working copy lives outside the archive, mirroring the website split,
   because the synced mount cannot unlink git's lock files (`PUSH.md`).
   **Scope: the surface only.** `ark-api/`, `ark-web/`, `README.md`, `RECOVERY.md`,
   `.gitignore`. `bin/` stays archive-only and joins later as `ops/` per
   `NODE-ARCHITECTURE.md` §6, once the stoplist duplication is settled — a design
   decision that should not block history from starting.

   **Private first.** The local-path review is its own step before it is ever made
   public; `PUSH.md` records why that order matters, since private to public is one
   click and public to private is not.

   **`RUNBOOK.md` is deliberately untracked**, exactly as `PUSH.md` is in the
   website repo. It names one machine's own paths and a username inside a
   documented error message, and its value is partly that those are the paths that
   actually failed. `RECOVERY.md` is the generic, publishable procedure and is what
   §8.10 requires.

   **A clone cannot run on its own**, and that is not a defect: this is a window
   onto a 1.6 TB archive that is not in the repository. `store.py` locates the
   archive by `ARK_ROOT` or by its own position on disk, and imports the stoplist
   from `bin/index-bm25.py` rather than copying it.

---

## A degraded mode you could not see, fixed 2026-09-04

Asking for **hybrid** on a server started with the wrong interpreter returned
keyword results, labelled every one of them `keyword`, and said
`retriever agreement: single`. Every word of that was true and it read as a fact
about the query rather than about the server: half of what was asked for had not
run, and nothing said so.

The search payload now carries `mode_ran` and `degraded`, and the page prints a
warning above the results whenever the mode that ran is not the mode that was
asked for. `mode=dense` with no dense half also now actually runs the keyword
fallback it claims to, which it previously did not — it reported the fallback and
returned nothing.

This is the sixth item in `NODE-ARCHITECTURE.md`'s *architecturally missing* list:
*no failure visibility — degraded mode, stale index and missing services are all
currently silent*. It was reproduced by accident, inside the very surface written
to fix it.
