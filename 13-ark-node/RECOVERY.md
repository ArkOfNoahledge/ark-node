# Recovering the node's surface from printed instructions

**Kind:** RECOVERY PROCEDURE · Implements spec §8.10
**Audience:** a technically capable person who is not the author, holding this
page on paper and a copy of the archive.

> **ONE PROCEDURE, TWO READING LEVELS — AND THIS IS THE DENSE ONE.**
> `00-docs/OPERATOR-MANUAL-EN.md` and `-ES.md` are the procedure the Operator
> actually follows, written to the §3.5 competence floor: every command literal,
> every expected output shown, every failure routed to a symptom. This page is the
> same procedure for a reader who can already read an error, and it exists because
> the surface's half of §8.10 is worth stating compactly for whoever maintains it.
>
> **They must not diverge.** If a step changes, it changes in the manual first and
> here second. Two documents describing one recovery is the defect this build spent
> 2026-09-05 removing everywhere else; it is permitted here only because the two
> audiences are genuinely different and the manual is the authority.

> **What this page is for.** §8.10 requires that the node be rebuildable by a
> second person from printed instructions. This is the surface's half of that.
> It names no machine and no absolute path, because a printed page cannot know
> which drive letter the archive was restored to. Where it says `<ARCHIVE>`,
> substitute the path to the restored archive root — the directory containing
> `10-index`, `07-corpora-supplemental` and `13-ark-node`.

## What you need

1. **The archive.** Specifically `10-index/` for retrieval, and the corpus
   shelves `02-corpora-core` through `07-corpora-supplemental` for the documents
   themselves. The surface is a window onto those; it carries no content of its
   own. Retrieval covers only part of what is on the shelves, and the surface
   says so on screen and offers a Kiwix search over the rest.
2. **Python 3.** Any version from 3.8. **Nothing else.** No pip install, no
   network. BM25 lives in SQLite and `sqlite3` ships with Python.
3. **`kiwix-serve`**, for the links out to archive articles. It is vendored at
   `<ARCHIVE>/09-software/kiwix-tools/` — extract the build for this platform.
   Without it the surface still searches and still shows passages; only the
   links to ZIM articles stop working, and the PDF citations are unaffected.

Optional, for semantic search: a Python environment with `numpy`, `faiss-cpu`
and `sentence-transformers`, built offline from
`<ARCHIVE>/09-software/python-wheels/`. Absent it, the surface runs keyword-only
and says so on screen.

> **This line said `chromadb` until 2026-09-16.** The dense half was rebuilt on
> 2026-09-15 as a flat faiss PQ64 index with an exact rerank - 443 MB resident
> where Chroma needed 29.7 GB for a third as many vectors - and `10-index/chroma/`
> was then deleted. **A recovery page naming a package the system no longer uses
> is worse than one naming none**, because it sends the person rebuilding from
> paper to install something that will not help. `faiss-cpu==1.15.0` is vendored
> and pinned in `<ARCHIVE>/09-software/requirements-lock.txt`.

## Procedure

**1. Rebuild the provenance store.** About ten minutes.

    python <ARCHIVE>/bin/index-provenance.py

Expect `50/50 artifacts` and `39,073,563 chunks` (2026-09-25). If the chunk count
differs from `10-index/sources.json`, the index is incomplete — stop and restore
it first.

> **This step said *about forty seconds* and *29/29 artifacts, 2,315,810 chunks*
> until 2026-09-25** — pass 1's figures, through passes 2 and 3. Read from paper
> against a correct archive, the sentence above would have declared it damaged.
> It had been corrected for exactly that reason once already, on 2026-09-06. The
> ten minutes is derived, not timed: on 2026-09-25 the two Wikipedias alone,
> 32,147,292 chunks, took 439 s to add, about 73,000 chunks a second.

**2. Build the Kiwix library.**

    python <ARCHIVE>/bin/kiwix-library.py

> **ON A RESTORED ARCHIVE, REBUILD THE LIBRARY BEFORE SERVING ANYTHING.** The
> paths inside it are absolute, so they name the drive the archive was built on.
> A restored copy carries a `library.xml` that points at a machine that is not
> this one. It is also excluded from `CHECKSUMS.sha256` on purpose, so
> `verify.sh` will **not** warn you about it. This step is the only thing that
> catches it.

It lands at the **archive root**, one file covering every corpus shelf, and it
writes **absolute** paths. Two assertions must print: every path is absolute and
resolves to a file, and no two paths share a basename.

The absolute path is the load-bearing part. `kiwix-serve` derives each book's URL
key from the path stored in the library, and a relative path is resolved against
the library file's own directory, so a relative multi-segment path produces a key
that cannot be addressed under any spelling — the book appears in the catalogue
and every article returns 404 while the server reports success. An absolute path
is opened directly and the key is the bare filename, which works from anywhere.
Measured against a running server on 2026-09-12; see `RUNBOOK.md`.

**3. Start `kiwix-serve`**, pointing at that library with an absolute path in the
platform's own form. Then confirm it opened the books, because "the library was
successfully loaded" is a statement about parsing XML and not about finding ZIMs:

    curl -s "http://localhost:8080/catalog/v2/entries?count=-1" | grep -c "/content/"

**The count must equal the number of `.zim` files on the corpus shelves**, which
the previous step printed. Zero means the library points at files it cannot open,
which on a restored archive is the machine-specific-path failure above.

Note that this number is larger than the number of artifacts retrieval covers,
and that is expected rather than a fault: the library serves everything on the
shelves, the index covers a subset, and the surface names the difference.

**4. Record the book keys from the running server.**

    python <ARCHIVE>/bin/kiwix-library.py --verify-against http://localhost:8080

This reads the keys out of the server's own catalogue rather than deriving them.
Do not skip it and do not hand-write the file it produces. It records a key for
each **indexed** artifact, not for all served books, and it also records which
library file it was confirmed against, which is what lets the surface refuse to
describe a library nobody is serving.

**5. Start the surface.**

    python <ARCHIVE>/13-ark-node/ark-api/serve.py

Then open `http://<node>:8090/`.

> **`bin/ark.py up` does steps 3 and 5 together**, plus the tile server and the
> two models, and prints what each one needs when it cannot start it.
> `bin/ark.py commands` prints every command and starts nothing.
>
> **The steps above stay the procedure.** They are what you follow when the
> launcher will not run, which is the case this page exists for, and they are the
> only ones that show you the library check in step 3 — the check that
> distinguishes a library that parsed from a library that found its ZIMs.
>
> **The interpreter decides whether the surface has a semantic half.** Started
> with a plain Python it runs keyword only, which is correct and is not a
> failure; started with an interpreter that can import `numpy`, `faiss` and
> `sentence-transformers` it also searches by meaning. `ark.py` looks for that
> interpreter and prints which one it chose. Either way every result carries a
> citation, which is what this page is about.
>
> **And the interpreter is not the only thing that can take the semantic half
> away.** On 2026-09-15 a node running under the correct interpreter fell to
> keyword because numpy half-initialized across two threads, while the screen
> blamed the interpreter. The banner now prints the cause the node measured, and
> `/api/health` carries it in full. **Read the cause before acting on it.**

## Rebuilding the Python environment, offline

**Skip this if you only need keyword search.** The surface runs on Python's
standard library alone. This section is for the semantic half.

**Nothing here reaches the network, and that is enforced rather than hoped:
`--no-index` makes pip fail rather than quietly download.** Everything needed is
on the drive.

**1. Python 3.12.** The wheels are `cp312` and will not install on any other
minor version. If `python --version` does not say 3.12, install it from the
archive:

    <ARCHIVE>/09-software/python-3.12.9-amd64.exe

It needs administrator rights, and it changes `PATH` for **new** shells only, so
open a fresh one afterwards. **This is the one step no script can do for you.**

**2. Create the environment.** `bin/ark.py` looks for it at `<ARCHIVE>\.venv`;
anywhere else works if `ark.toml` says where (`paths.venv`). Never on top of a
working one:

    python -m venv <ARCHIVE>\.venv

**3. Install, offline, from the lock file:**

    <ARCHIVE>\.venv\Scripts\python.exe -m pip install ^
        --no-index ^
        --find-links=<ARCHIVE>\09-software\python-wheels ^
        -r <ARCHIVE>\09-software\requirements-lock.txt

221 packages. Several minutes; `torch` alone is 2.75 GB. **Use the lock file, not
individual package names** — the wheel directory carries more than one version of
`filelock`, `fsspec` and `setuptools`, and only the lock knows which ones this
build was proven against.

**4. Check it, and read the two lines separately:**

    <ARCHIVE>\.venv\Scripts\python.exe -c "import numpy, faiss, sentence_transformers, torch; print('stack ok')"
    <ARCHIVE>\.venv\Scripts\python.exe -c "import torch; print('cuda', torch.cuda.is_available())"

**These are two different questions.** The stack importing says nothing about
whether a GPU is present. `torch` installs and runs happily on the CPU, so a
machine with no NVIDIA driver will pass the first line and print `cuda False` on
the second. **Retrieval and answering still work on the CPU. Building an index
does not, in any practical sense.** That is a driver question, not an archive
one: no archive can carry a driver for hardware it has never seen.

**5. Prove it with the thing that matters**, which is not that packages
installed but that a rebuilt environment serves this index:

    <ARCHIVE>\.venv\Scripts\python.exe <ARCHIVE>/13-ark-node/ark-api/serve.py --selftest --mode hybrid

Look for `backend pq over N vectors` on the dense line.

> **`bin/rebuild-venv.sh` does steps 2 to 5** and refuses to target the live
> keeper venv in any of its spellings. **The steps above stay the procedure** —
> the script is a convenience and must never become the only way in, the same
> rule `bin/ark.py` follows by printing every command it runs. A script can be
> lost, or can be the thing that is broken.

> **WHY THE VENV IS NOT ON THE COLD COPY, since someone will ask during a
> recovery and the answer is not "we forgot".** It is derived, and its
> ingredients are here: the exact Python, 221 wheels, and the lock file. Copying
> a venv would be worse than rebuilding one — `pyvenv.cfg` hardcodes an absolute
> path to a Python install inside one user's profile, and every launcher in
> `Scripts/` has it baked in, so a restored copy would be the most dangerous kind
> of artifact: one that appears to work.

## Confirming it works

    python <ARCHIVE>/13-ark-node/ark-api/serve.py --selftest

Runs five real questions and **fails if any result cannot be turned into a
citation**, which is the one property this layer exists to provide. Then, by hand:
search for something, and click a citation. If it opens the source document, the
chain is whole.

`http://<node>:8090/api/health` reports what is loaded and what is degraded.
Read it first when something seems wrong.

## The maps, if they were restored

`08-maps` is **843 GB and about 38% of the archive**, and until 2026-09-06 it
appeared in no procedure at all — the largest capability on the drive, reachable
only by someone who already knew it was there.

    <ARCHIVE>/09-software/pmtiles-cli/pmtiles serve <ARCHIVE>/08-maps --port 8081 --cors="*"

Then `/map/` on the surface. The node serves the viewer itself, so there is no
static server to start; `09-software/map-viewer/` holds MapLibre, the style, the
glyphs and the sprites, all local.

Three things fail silently here and each looks like something else:

1. **`--cors="*"` omitted.** Tiles are on :8081, the page on :8090, so every tile
   request is cross-origin. Without it the map draws nothing and the browser
   reports a security error, not a missing file.
2. **Glyphs served from a different origin than the page.** The map draws with **no
   labels at all** and reports nothing useful. Use the surface's `/map/`; the style
   asks for glyphs relative to itself precisely so this cannot happen.
3. **`pmtiles serve` pointed at the wrong directory.** Every tile 404s while the
   server reports success. The archive names in a URL are the file names in
   `08-maps` without `.pmtiles`.

One command settles which of the three it is:

    curl -s -o /dev/null -w '%{http_code} %{size_download}\n' \
        http://localhost:8081/20260825/5/9/14.mvt

**200 and roughly 28,000 bytes.** Anything else is item 3.

---

## Finding a place by name

The map draws the planet; it has no search box. Turning a NAME into coordinates
is a separate command, and it needs no server at all, not even the tile server:

    python <ARCHIVE>/bin/index-gazetteer.py --find maracaibo

It holds 46,861 places under 271,848 names in 42 languages, read out of the
basemap itself, and it answers in any of them. **Many places share a name**:
`--find franklin` lists thirteen, largest first. Read the coordinates, not just
the first line.

Those two numbers are what the climate lookup wants:

    python <ARCHIVE>/bin/koppen-lookup.py 10.6498 -71.6418 Maracaibo

**`rasterio is not installed` means the right command and the wrong `python`.**
Map rasters need the geospatial environment, which is the keeper environment,
`<ARCHIVE>/.venv/Scripts/python.exe` unless `ark.toml` names another. `/api/spatial` checks which interpreter
can import rasterio and prints the working form rather than guessing.

If a place is not there, the reason is usually the layer rather than the zoom:
the basemap's `places` layer labels localities, regions and countries, so lakes,
mountains and ruins are drawn on the map and have no searchable name. Find those
by eye and read the coordinates off the viewer's panel.

---

## What this surface does not do

**No voice.** Voice (§8.6) is not started. That is what remains of
`ROADMAP.md` §8.5 through §8.8.

**The map pane was built 2026-09-07**, and so was the passage matcher. A question
that names a place shows its coordinates beside the answer with no model running,
and a retrieved passage that is itself about somewhere carries a chip that opens
the map there. **This paragraph said there was no map pane for one day after there
was one** - the second time in three days that this page described less than the
system does, and the paragraph below was already about exactly that.

**This paragraph used to say there was no model and no cross-check either.** Both
were built on 2026-09-05, and this page - the one an operator holds on paper -
still said otherwise for two days. A page describing less than the system does is
the same defect as one describing more: it teaches the reader not to look.

**The semantic half of retrieval first ran on 2026-09-08.** Until then every
hybrid query returned keyword results and said so, honestly, in a footer that
never changed — against a 27 GB vector store on the drive that was intact the
whole time. The cause was a lazy load with no lock meeting a race inside
chromadb, and a sticky error flag that made one unlucky start permanent. It is
recorded here because the failure was invisible to every check the node had, and
because **a page that describes less than the system does teaches the reader not
to look** — which is what the two paragraphs above are also about.

What the surface is, on its own: a searchable index of **1.6 TB** of reference
material with a citation for every answer, a second model checking the first when
a figure warrants it, and a whole-planet map with a gazetteer behind it.
