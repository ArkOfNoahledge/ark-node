#!/usr/bin/env python3
"""
index-build.py - build the pass 1 retrieval index. Spec 8.4, 7.5.

    python bin/index-build.py --pilot                 # one artifact, 300 docs, ~minutes
    python bin/index-build.py --artifact zimgit-post-disaster_en_2024-05.zim
    python bin/index-build.py --all                   # everything in index-scope-pass1.txt
    python bin/index-build.py --all --resume          # skip artifacts already finished
    python bin/index-build.py --all --scope index-scope-pass2.txt
    python bin/index-build.py --all --scope index-scope-pass2.txt --dry-run

CHROMA IS OFF BY DEFAULT, SINCE 2026-09-29. The build writes the .jsonl and .npy
files and stops; the index the node queries is built from them afterwards, by
the steps printed at the end of every run. Chroma was the dense index until
2026-09-15, was deleted on 2026-09-16, and until this change the build still
LOADED IT BY DEFAULT at the end of every run: pass 3 needed `--no-chroma` on its
command line to avoid finishing three days of correct embedding by trying to put
33.6M vectors into Chroma at 4,287 bytes each, about 144 GB on a 64 GB machine.
A default a careful operator must remember to override is a trap for everyone
else, and the public release makes "everyone else" the audience. `--chroma` asks
for it; `--no-chroma` is still accepted, and now changes nothing.

--scope AND --dry-run EXIST BECAUSE A SCOPE FILE CARRIES A COMMAND LINE IN ITS
OWN HEADER, AND THAT COMMAND HAD NEVER BEEN RUN. index-scope-pass2.txt was
written, checksummed and committed on 2026-09-13 with a --scope flag this file
did not have: the scope was a MODULE CONSTANT and the only way to build a second
corpus was to edit line 52. argparse would have rejected the documented command
at the start of an eight-hour run. Same defect as index-measure.py four days
earlier, one tool over - a script that has never been executed is not a
procedure - so --dry-run resolves the scope, checks every path against the disk
and names what would be re-indexed, in about a second, writing nothing.

WHAT IS THE ARCHIVE OF RECORD, AND WHAT IS DISPOSABLE

    10-index/chunks/<artifact>.jsonl     text + provenance      <- SOURCE OF TRUTH
    10-index/vectors/<artifact>.f16.npy  embeddings, float16    <- SOURCE OF TRUTH
    10-index/sources.json                registry + build config <- SOURCE OF TRUTH
    10-index/chroma/                     Chroma HNSW            <- DERIVED, OPTIONAL (--chroma)

The dense index the node queries is 10-index/pq/, a flat faiss PQ64 index built
by bin/index-pq-build.py from the .npy files. The paragraph below is from before
that, and its argument is the one that made the switch take minutes.

Chroma is deliberately last on that list. Its HNSW files are fragile, must be
fully RAM-resident to query, and are the one component whose size decides whether
the retrieval layer fits a solar node at all (23.2 GB at pass 1 scope, measured
2026-09-01). Keeping the vectors in plain .npy beside plain .jsonl means the index
TYPE stays re-decidable per machine - Chroma here, a quantized faiss index on a
smaller node - without re-reading a single ZIM or recomputing a single embedding.
Delete 10-index/chroma/ and it rebuilds in minutes. Lose the .npy files and it is
days of GPU time.

PROVENANCE IS EMITTED FROM THE FIRST CHUNK, NOT ADDED LATER

Every chunk carries the artifact it came from, the entry path inside that artifact,
and the page number when the source is a PDF - enough for an operator with no
network to open the exact page in Kiwix or a PDF reader and read the original.
An answer this system cannot trace back to a document on the drive is exactly the
failure mode spec 2 exists to prevent.

Per-chunk metadata is kept SMALL and the artifact-level facts (filename, sha256,
licence, Kiwix book name) live once in sources.json. At 5.7 million chunks, a
120-byte metadata record is 680 MB; putting the sha256 in every chunk would add
another 380 MB and make correcting a licence field a rewrite of the whole store.
"""

import os, sys, json, time, hashlib, argparse, re, html

# NO BYTECODE. This module loads bin/pdf-tables.py through importlib, and Python
# writes a .pyc beside any module it IMPORTS rather than runs. That is how three
# .pyc files reached bin/'s checksum manifest on 2026-09-04, and how one reached
# the cold copy's mirror list on 2026-09-05 - caught by a --dry-run, not by any
# check that exists to catch it. ark-api/serve.py has carried this line since the
# first time; these did not.
sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCOPE = os.path.join(HERE, "index-scope-pass1.txt")   # default; --scope overrides
OUT = os.path.join(ROOT, "10-index")
MODEL = os.path.join(ROOT, "01-models", "tier4-embedding", "bge-m3")

# The network is forbidden before anything is imported, not after. A library that
# reaches out during import would otherwise succeed here and fail on the node.
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"
os.environ["HF_DATASETS_OFFLINE"] = "1"

# --- build configuration. Every value here is recorded into sources.json, so an
# --- index can always answer "what produced you?" without reading this file.
CHUNK_TOKENS = 512          # BGE-M3 tokens, NOT tiktoken tokens - see index-preflight.py
CHUNK_OVERLAP = 77          # ~15%
EMBED_BATCH = 32          # measured 2026-09-01: 162 chunks/s, 1.5 GB VRAM.
                          # Throughput is FLAT from 32 to 256 (162 -> 144) and VRAM
                          # never exceeds 4 GB, so the GPU is saturated at 32 and a
                          # bigger batch buys nothing. Worth knowing for the node:
                          # this index can be rebuilt on a 4 GB card.
DIM = 1024
MIN_DOC_CHARS = 50          # below this a "document" is a stub, not content
# BUMPED TO /2 ON 2026-09-05, because html_to_text and the PDF path both changed
# that day: _WS was extended to non-\s separators, and pdf_tables.dehyphenate was
# added to rejoin words a PDF broke across a line. Either alone shifts the text
# and therefore every byte offset derived from it.
#
# The bump is late. The changes landed at 14:22 and this line was still /1 at
# 20:30, so any build report written in between claims an extractor it did not
# use. The comment on the previous line asked for exactly this and was not read,
# which is worth leaving visible: a note that instructs is only as good as the
# habit of re-reading the file you are editing.
EXTRACTOR = "ark-html-strip/3"   # /3: HTML tables rendered rather than flattened   # bump if html_to_text changes; offsets/text shift with it

_SCRIPT = re.compile(r"(?is)<(script|style|noscript)\b.*?</\1>")
_TAG = re.compile(r"(?s)<[^>]+>")
# SEPARATORS THAT PYTHON'S \s DOES NOT MATCH, AND WHY THIS LIST EXISTS.
# 2026-09-05: the USDA canning guide's dial-gauge pressure table was invisible to
# keyword search. Its text reads "Recommended\x18process\x18time\x18for\x18Snap" -
# the words ARE separated, by U+0018, because that PDF's font maps its space glyph
# to a control character. `\s` does not match it, so BM25 saw one enormous token
# and a query containing "dial gauge pressure canner" could not reach the page
# that answers it. The answer was in the archive and unreachable.
#
# Measured across the whole index: 7,218 chunks (0.32%) carry one of these, led by
# U+200B and the C0 controls. It is NOT a PDF-only fault - appropedia, a ZIM, has
# the most of any artifact, so this is applied on both the HTML and the PDF path.
# U+200C is deliberately absent: the zero-width NON-joiner is meaningful in Arabic
# and Persian script, which this archive contains.
_WS = re.compile("[\\s\\x00-\\x08\\x0b\\x0c\\x0e-\\x1f\\x7f\\u00a0\\u200b\\u2000-\\u200a\\u202f\\u205f\\u3000]+")
_TITLE = re.compile(r"(?is)<title[^>]*>(.*?)</title>")
_CITEMARK = re.compile(r"\[\s*\d{1,3}\s*\]")

# THE ZIM MARKS ITS OWN BOILERPLATE, AND NOBODY LOOKED.
#
# Every MediaWiki-derived ZIM in this corpus wraps its generated footer in an
# htdig_noindex comment pair - literally "do not index this". Verified 2026-09-02
# on mdwiki, wikipedia_en_medicine, wikem and energypedia: all four, identical.
# It is a machine-readable instruction from the people who built the archive, and
# the pass-1 build ignored it and embedded 11,000 copies of a licence notice.
_NOINDEX = re.compile(r"(?is)<!--\s*htdig_noindex\s*-->.*?<!--\s*/htdig_noindex\s*-->")

# Containers whose text is furniture. Class names taken from the probe output,
# not from memory of what MediaWiki usually emits.
#   navbox / navbox-styles / nowraplinks  the drug and disease link farms that
#                                          produced 1,550-copy chunks
#   reflist / references / mw-references-wrap   citation lists: real information,
#                                          but never the answer to a question, and
#                                          the source of most "dup" chunks
#   machine-translation-banner            WikEM's translated-page notice
#   zim-footer                            belt and braces; also inside _NOINDEX
#   footer / footer-*  / banner-*         iFixit is NOT MediaWiki and needs its own
#   mobile-wiki-toc / mobile-skeleton-toc iFixit navigation scaffolding
_STRIP = [
    ("div", re.compile(r'(?i)(class|id)\s*=\s*["\'][^"\']*'
                       r'\b(navbox|navbox-styles|reflist|mw-references-wrap|catlinks|'
                       r'printfooter|machine-translation-banner|zim-footer|'
                       r'mobile-wiki-toc|mobile-skeleton-toc|footer-container|'
                       r'footer-row|footer-stats|banner-wrap|banner-bucket)\b')),
    ("ol", re.compile(r'(?i)class\s*=\s*["\'][^"\']*\b(references|mw-references)\b')),
    ("table", re.compile(r'(?i)class\s*=\s*["\'][^"\']*\b(navbox-inner|nowraplinks)\b')),
    ("footer", re.compile(r"")),
]


def _matching_end(s, tag, start):
    """Index just past the close tag matching the one opening at `start`.

    Nesting-aware on purpose: a navbox contains tables and divs, and a non-greedy
    `.*?</div>` would cut at the first inner close and leave the rest of the
    furniture in the text - which looks like it worked."""
    opener = re.compile(r"<%s\b" % tag, re.I)
    closer = re.compile(r"</%s\s*>" % tag, re.I)
    depth, i, n = 0, start, len(s)
    while i < n:
        mo = opener.search(s, i)
        mc = closer.search(s, i)
        if mc is None:
            return n
        if mo is not None and mo.start() < mc.start():
            depth += 1
            i = mo.end()
        else:
            depth -= 1
            i = mc.end()
            if depth <= 0:
                return i
    return n


def strip_boilerplate(t):
    t = _NOINDEX.sub(" ", t)
    for tag, attr in _STRIP:
        open_re = re.compile(r"<%s\b[^>]*>" % tag, re.I)
        pos = 0
        while True:
            m = open_re.search(t, pos)
            if not m:
                break
            if attr.pattern and not attr.search(m.group(0)):
                pos = m.end()
                continue
            end = _matching_end(t, tag, m.start())
            t = t[:m.start()] + " " + t[end:]
            pos = m.start() + 1
    return t


# ---------------------------------------------------------------------------
# HTML TABLES
# ---------------------------------------------------------------------------
#
# Until 2026-09-05 html_to_text stripped every tag with one regex, so a table
# lost not only its column names but its ROW BOUNDARIES: a four-column table
# became one unbroken run of cell text with nothing marking where a row ended.
# The note on record - that HTML serialises row-major and therefore preserves
# pairing - was half true. Adjacent cells stay adjacent; nothing says which cells
# were a row.
#
# Measured with bin/index-table-audit.py, after boilerplate removal so navboxes
# are not counted: 112,094 chunks carry table text, concentrated in the medical
# and technical shelves. The open item on record was 3,942, which is a different
# population - PDF chunks inside HTML-dominated ZIMs. The larger job was the one
# nobody had sized.
#
# THIS IS EASIER THAN THE PDF CASE AND SHOULD BE MORE RELIABLE. A PDF has no
# header markup, so bin/pdf-tables.py must infer a grid and gate what it infers,
# and that gate has been wrong twice. HTML says so explicitly: <tr>, <td> and
# <th> are the structure, and <th> is present in 90 to 99% of tables in every
# medical artifact. Nothing is inferred here, so nothing needs a quality gate -
# only guards against markup that is a table by tag and a layout by intent.
#
# Rendering policy is the one already decided for PDFs: two columns become prose
# rows, three or more become markdown. Rowspan and colspan are NOT honoured; a
# spanned cell appears once, in its first row, which is wrong for a minority of
# tables and much less wrong than no rows at all.

_TABLE_OPEN = re.compile(r"(?i)<table\b[^>]*>")
_TR_SPLIT = re.compile(r"(?i)<tr\b[^>]*>")
_CELL = re.compile(r"(?i)<(t[hd])\b[^>]*>(.*?)</\1\s*>", re.S)
_CAPTION = re.compile(r"(?i)<caption\b[^>]*>(.*?)</caption\s*>", re.S)
_TH = re.compile(r"(?i)<th\b")

MAX_TABLE_COLS = 12         # beyond this it is page layout, not data
MAX_CELL_CHARS = 400        # a cell holding a paragraph is a layout container


def _cell_text(frag):
    return _WS.sub(" ", pdf_tables.dehyphenate(
        html.unescape(_TAG.sub(" ", frag)))).strip()


def render_html_table(frag):
    """A <table> fragment as text that survives being read by three consumers.

    Returns the rendering, or None to leave the fragment to the ordinary
    tag-stripping path. None is the safe answer: it reproduces exactly what this
    function replaced, so a table this cannot handle is no worse than before.
    """
    # A TABLE CONTAINING A TABLE IS A LAYOUT CONTAINER. Rendering the outer one
    # would interleave the inner rows into its cells and assert a grid that is
    # not there, which is the failure the PDF gate exists to catch.
    if _TABLE_OPEN.search(frag, 1):
        return None

    caption = ""
    mc = _CAPTION.search(frag)
    if mc:
        caption = _cell_text(mc.group(1))
        frag = frag[:mc.start()] + " " + frag[mc.end():]

    rows = []
    for chunk in _TR_SPLIT.split(frag)[1:]:
        cells = [_cell_text(m.group(2)) for m in _CELL.finditer(chunk)]
        if cells and any(cells):
            rows.append(cells)
    if len(rows) < 2:
        return None

    ncol = max(len(r) for r in rows)
    if ncol < 2 or ncol > MAX_TABLE_COLS:
        return None
    rows = [r + [""] * (ncol - len(r)) for r in rows]
    cells = [c for r in rows for c in r]
    if max((len(c) for c in cells), default=0) > MAX_CELL_CHARS:
        return None
    if sum(1 for c in cells if c) / float(len(cells)) < 0.35:
        return None

    # THE HEADER IS DECLARED, NOT GUESSED. <th> in the first row means those
    # cells are column names; the PDF path has to infer this and sometimes
    # promotes a data row instead.
    first_tr = _TR_SPLIT.split(frag)[1] if len(_TR_SPLIT.split(frag)) > 1 else ""
    header = rows[0] if (_TH.search(first_tr) and sum(1 for c in rows[0] if c) >= 2) else None
    body = rows[1:] if header else rows
    if not body:
        return None

    head = "Table" + (" (%s)" % caption if caption else "")

    if ncol == 2:
        # Two columns are a pairing, and prose keeps it unambiguous for BM25,
        # for the embedder and for a reader at once.
        pairs = []
        if header and any(header):
            pairs.append("%s: %s." % (header[0], header[1]))
        for a, b in body:
            pairs.append("%s: %s." % (a, b) if b else "%s." % a)
        return "%s. %s" % (head, " ".join(p for p in pairs if p.strip(" ."))) 

    out = [head + ":"]
    if header:
        out.append("| " + " | ".join(header) + " |")
        out.append("|" + "|".join(["---"] * ncol) + "|")
    for r in body:
        out.append("| " + " | ".join(r) + " |")
    return "\n".join(out)


def _plain(seg):
    """The original path, unchanged, for everything that is not a table."""
    seg = _TAG.sub(" ", seg)
    seg = html.unescape(seg)
    seg = _TAG.sub(" ", seg)
    seg = _CITEMARK.sub(" ", seg)
    return _WS.sub(" ", pdf_tables.dehyphenate(seg)).strip()


def html_to_text(raw, strip=True):
    """Boilerplate out, tags out, entities decoded - in that order.

    ENTITIES were the larger defect. The first version stripped tags and stopped,
    leaving `&nbsp;`, `&amp;` and `&quot;` in the text: measured on the completed
    pass-1 build, **52.8% of 3.2 million chunks** carried at least one, 160,627
    `&nbsp;` in a 60,000-chunk sample alone. The embedder had to account for them
    and the reader was shown them in citations.

    ORDER MATTERS. Boilerplate goes first, while the containers still exist to
    identify it. Tags are stripped BEFORE unescaping and again after: unescaping
    first would turn `&lt;script&gt;` into a real tag; unescaping last can reveal
    markup that was double-encoded in the source."""
    try:
        t = raw.decode("utf-8", "ignore")
    except Exception:
        return "", ""
    m = _TITLE.search(t)
    title = _WS.sub(" ", _TAG.sub(" ", html.unescape(m.group(1)))).strip() if m else ""

    t = _SCRIPT.sub(" ", t)
    if strip:
        t = strip_boilerplate(t)

    # TABLES ARE RENDERED IN PLACE, AND THE TEXT IS BUILT IN SEGMENTS. It cannot
    # be one pass any more: a rendered table carries newlines that separate its
    # rows, and the final _WS collapse would flatten them straight back into the
    # run this change exists to remove. So the document is cut at table
    # boundaries, each ordinary segment goes through exactly the old path, each
    # table through the renderer, and the pieces are joined with newlines.
    #
    # Boilerplate removal comes FIRST, so the navbox tables are already gone and
    # never reach the renderer. _matching_end is reused rather than a non-greedy
    # match, for the reason written on it: a table contains tables.
    out, pos = [], 0
    while True:
        m = _TABLE_OPEN.search(t, pos)
        if not m:
            break
        head = _plain(t[pos:m.start()])
        if head:
            out.append(head)
        end = _matching_end(t, "table", m.start())
        frag = t[m.start():end]
        rendered = render_html_table(frag)
        # None means this is a table by tag and something else by intent. The
        # fallback is the ORIGINAL behaviour on that fragment, so a table this
        # cannot handle is exactly as good as it was before, never worse.
        piece = rendered if rendered else _plain(frag)
        if piece:
            out.append(piece)
        pos = end
    tail = _plain(t[pos:])
    if tail:
        out.append(tail)
    return "\n".join(out).strip(), title


def human(n):
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return "%.1f %s" % (n, u)
        n /= 1024.0


def load_scope(path=None):
    out = []
    for line in open(path or SCOPE, encoding="utf-8"):
        line = line.split("#", 1)[0].strip()
        if line:
            out.append(line)
    return out


def resolve_scope(name):
    """A bare name means bin/<name>, which is where scope files live. A path is
    taken as given. Either way it must EXIST - a scope that silently falls back
    to the default would build the wrong corpus and say nothing."""
    if not name:
        if os.path.isfile(SCOPE):
            return SCOPE
        sys.exit("default scope file missing: %s" % SCOPE)
    cands = [name, os.path.join(HERE, name), os.path.join(HERE, os.path.basename(name))]
    for c in cands:
        if os.path.isfile(c):
            return os.path.abspath(c)
    sys.exit("scope file not found: %s (looked in . and %s)" % (name, HERE))


def pretty_title(p, doc=None):
    """A citation an operator can act on.

    "files/Emergency Preparedness (12).pdf" is a storage path. The document behind
    it is FM 3-05.70, the Army Survival Manual - which is what belongs in an answer.

    A person identifies a title by LOOKING: it is the biggest text near the top of
    page 1. So that is what this does, via the font sizes fitz already parses. The
    first attempt took "the first sentence-ish string" and produced citations like
    "1 Preface As a soldier, you can be sent to any area of the world" and a bare
    URL - both from the pilot, both worse than the filename they replaced."""
    fname = os.path.basename(str(p))
    fname = fname[:-4] if fname.lower().endswith(".pdf") else fname
    if doc is None:
        return fname

    cand = ""
    try:
        t = _WS.sub(" ", (doc.metadata or {}).get("title") or "").strip()
        if len(t) > 4 and not t.lower().startswith(("microsoft word", "untitled", "http")):
            cand = t
    except Exception:
        pass

    if not cand:
        try:
            pg = doc[0]
            h = pg.rect.height or 1
            best = (0.0, "")
            for blk in pg.get_text("dict").get("blocks", []):
                for line in blk.get("lines", []):
                    txt = _WS.sub(" ", "".join(sp_.get("text", "")
                                               for sp_ in line.get("spans", []))).strip()
                    if not (6 < len(txt) < 120):
                        continue
                    if txt.lower().startswith(("http", "www.", "page ", "figure ")):
                        continue
                    size = max((sp_.get("size", 0) for sp_ in line.get("spans", [])),
                               default=0)
                    # top third of the page, weighted by font size
                    y = line.get("bbox", [0, h, 0, 0])[1] / h
                    if y > 0.5:
                        continue
                    score = size * (1.0 - y)
                    if score > best[0]:
                        best = (score, txt)
            cand = best[1]
        except Exception:
            pass

    if not cand:
        return fname
    return "%s (%s)" % (cand[:110], fname)


# Loaded by path because the file name has a hyphen in it, which is the bin/
# naming convention for scripts and is not importable as a module name.
def _load_pdf_tables():
    import importlib.util
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pdf-tables.py")
    spec = importlib.util.spec_from_file_location("pdf_tables", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


pdf_tables = _load_pdf_tables()
_TABLE_STATS = {"structured": 0, "left_as_text": 0}


def pdf_document(doc):
    """Whole PDF as ONE string plus a page map, so chunks can CROSS page breaks.

    Chunking page by page looked reasonable and was wrong. In the pilot, a water
    purification procedure beginning on page 100 was retrieved from page 101 as
    "...is usually 2 percent strength, so 10 drops will be needed" - ten drops of a
    substance named on the previous page, in a different chunk, with no overlap
    between them. In a manual of step-by-step procedures that is the failure mode
    this archive can least afford. Pages are joined; the map records where each
    began, so a chunk is still cited to the page it starts on."""
    parts, pmap, pos = [], [], 0
    for pno in range(doc.page_count):
        try:
            # TABLE-AWARE SINCE 2026-09-05. doc[pno].get_text() follows visual
            # reading order, which on a two-row dosing table emits the whole
            # first row then the whole second and destroys the pairing. That
            # produced a grounded, cited answer telling an operator to put five
            # teaspoons of bleach in a litre of drinking water. See
            # bin/pdf-tables.py and BUILD-LOG, "The first dangerous answer".
            t, ntab, unstructured = pdf_tables.page_text(doc[pno])
            t = t.strip()
            if ntab:
                _TABLE_STATS["structured"] += ntab
            if unstructured:
                _TABLE_STATS["left_as_text"] += len(unstructured)
        except Exception:
            try:
                t = _WS.sub(" ", pdf_tables.dehyphenate(doc[pno].get_text())).strip()
            except Exception:
                continue
        if not t:
            continue
        pmap.append((pos, pno + 1))
        parts.append(t)
        pos += len(t) + 1
    return "\n".join(parts), pmap


def page_at(pmap, off):
    """The page a character offset falls on."""
    pg = pmap[0][1] if pmap else None
    for o, n in pmap:
        if o > off:
            break
        pg = n
    return pg


def lang_of(name):
    """Language from the Kiwix filename convention. Wrong for a hand-named file,
    which is why it is recorded per artifact and can be corrected in sources.json."""
    for tag in ("_en_", "_es_", ".en.", ".es."):
        if tag in name:
            return tag.strip("_.")
    return "en"


def sha_from_shelf(path):
    """The shelf already hashed this file (rule R7). Read that rather than
    re-hashing 3 GB - and if the manifest does not list it, say so rather than
    inventing a value."""
    d, base = os.path.split(path)
    cf = os.path.join(d, "CHECKSUMS.sha256")
    if not os.path.exists(cf):
        return None
    for line in open(cf, encoding="utf-8", errors="replace"):
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        h, nm = parts
        if os.path.basename(nm.lstrip("*./")) == base:
            return h
    return None


# --------------------------------------------------------------------------
# READERS. Each yields documents as (kind, path, title, page, text).
# A "document" is the smallest unit a citation should point at: one wiki article,
# or ONE PAGE of a PDF. Page-level granularity for PDFs is deliberate - telling an
# operator "page 12 of the field manual" is worth more than telling them the manual.
# --------------------------------------------------------------------------

def iter_zim_counted(path, stats):
    from libzim.reader import Archive
    import fitz
    a = Archive(path)
    get = getattr(a, "_get_entry_by_id", None) or getattr(a, "get_entry_by_id")
    for i in range(a.entry_count):
        try:
            e = get(i)
            if e.is_redirect:
                continue
            item = e.get_item()
            mime = str(item.mimetype).split(";")[0]
            if mime.startswith("text/html"):
                text, title = html_to_text(bytes(item.content))
                if len(text) < MIN_DOC_CHARS:
                    stats["stub_docs"] += 1
                    continue
                stats["html_docs"] += 1
                yield ("html", e.path, title or e.title, None, text)
            elif mime == "application/pdf":
                try:
                    doc = fitz.open(stream=bytes(item.content), filetype="pdf")
                except Exception:
                    stats["pdf_unreadable"] += 1
                    continue
                stats["pdf_files"] += 1
                full, pmap = pdf_document(doc)
                title = pretty_title(e.path, doc)
                doc.close()
                if len(full) < MIN_DOC_CHARS:
                    stats["pdf_no_text"] += 1
                    continue
                stats["pdf_pages"] += len(pmap)
                yield ("pdf", e.path, title, pmap, full)
        except Exception as ex:
            stats["errors"] += 1
            if not stats["first_error"]:
                stats["first_error"] = "%s: %s" % (type(ex).__name__, ex)


def iter_pdf_dir(d, stats):
    import fitz
    files = []
    for rt, _, fs in os.walk(d):
        for f in sorted(fs):
            if f.lower().endswith(".pdf"):
                files.append(os.path.join(rt, f))
    for p in files:
        rel = os.path.relpath(p, d).replace("\\", "/")
        try:
            doc = fitz.open(p)
        except Exception:
            stats["pdf_unreadable"] += 1
            continue
        stats["pdf_files"] += 1
        full, pmap = pdf_document(doc)
        title = pretty_title(rel, doc)
        doc.close()
        if len(full) < MIN_DOC_CHARS:
            stats["pdf_no_text"] += 1
            continue
        stats["pdf_pages"] += len(pmap)
        yield ("pdf", rel, title, pmap, full)


# --------------------------------------------------------------------------
# CHUNKER. Token-counted in BGE-M3's own tokenizer, loaded from local files.
#
# LlamaIndex's SentenceSplitter defaults to tiktoken, which DOWNLOADS its BPE file
# on first use. That works on a laptop with network and fails on the node, looking
# like a corrupt install rather than a missing download. Passing the embedding
# model's tokenizer removes the network dependency AND makes chunk_size mean what
# it appears to mean: 512 tokens as the embedder will actually see them, rather
# than 512 tiktoken tokens that BGE-M3 would re-tokenize into some other count.
# --------------------------------------------------------------------------

# 512 BGE-M3 tokens at the SMALLEST measured characters-per-token on this shelf.
# The 2026-09-12 calibration found 2.07 chars/token for iFixit and 3.26 for mdwiki,
# never the 4.00 an earlier estimator assumed, so sizing the fallback at 2.0 keeps
# it under the limit for every corpus here. Under-sized chunks are harmless; an
# over-sized one is silently truncated at embed time, which is not.
HARD_SPLIT_CHARS = int(CHUNK_TOKENS * 2.0)


def hard_split(text):
    """Length-based chunking for a document the sentence splitter cannot handle.

    Deliberately dumb: no tokenizer, no recursion, no library. It exists to be
    the thing that still works when the clever path does not, so it must not
    share any machinery with the clever path.

    Breaks at the last whitespace inside the window when there is one, which
    keeps most fallback chunks from cutting mid-word; a run with no whitespace
    at all is cut at the window, because that is exactly the input that made the
    real splitter recurse to death and there is nothing better to do with it."""
    out, i, n = [], 0, len(text)
    while i < n:
        end = min(i + HARD_SPLIT_CHARS, n)
        if end < n:
            cut = text.rfind(" ", i + HARD_SPLIT_CHARS // 2, end)
            if cut > i:
                end = cut
        piece = text[i:end].strip()
        if piece:
            out.append(piece)
        i = max(end, i + 1)
    return out


def make_splitter():
    import transformers
    # SentenceSplitter tokenizes the FULL document to measure it, so a 654-page
    # manual logs "199245 > 8192" once per document. Nothing is truncated - chunks
    # are 512 tokens - but at 26 artifacts the real warnings would be buried.
    transformers.logging.set_verbosity_error()
    from transformers import AutoTokenizer
    from llama_index.core.node_parser import SentenceSplitter
    tk = AutoTokenizer.from_pretrained(MODEL, local_files_only=True)
    sp = SentenceSplitter(
        chunk_size=CHUNK_TOKENS, chunk_overlap=CHUNK_OVERLAP,
        tokenizer=lambda t: tk.encode(t, add_special_tokens=False))
    return sp


def build_artifact(rel, args, registry):
    import numpy as np

    path = os.path.join(ROOT, rel)
    name = os.path.basename(rel.rstrip("/")) or rel
    slug = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    chunk_f = os.path.join(OUT, "chunks", slug + ".jsonl")
    vec_f = os.path.join(OUT, "vectors", slug + ".f16.npy")

    if args.resume and os.path.exists(vec_f) and os.path.exists(chunk_f):
        print("  %-52s skipped (already built)" % name[:52])
        return None

    stats = {"html_docs": 0, "stub_docs": 0, "pdf_files": 0, "pdf_pages": 0,
             "pdf_no_text": 0, "pdf_unreadable": 0, "errors": 0, "first_error": "",
             # ADDED 2026-09-14. See the handler in the document loop below.
             "split_fallback": 0, "split_dropped": 0, "first_split_fail": ""}

    sp = make_splitter()
    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer(MODEL, local_files_only=True)
    model.half()                       # 16 GB of VRAM; fp16 halves it for free

    # An artifact keeps the id it was first given. Allocating a fresh one on every
    # rebuild would leave the previous id's rows in Chroma AND in sources.json, so
    # a re-run would silently double the collection instead of replacing it. Caught
    # by re-reading the pilot's own registry rather than by a failure.
    src_id = None
    for k, a in registry["artifacts"].items():
        if a["file"] == name:
            src_id = int(k)
            break
    if src_id is None:
        src_id = registry["next_id"]
        registry["next_id"] += 1

    docs = iter_pdf_dir(path, stats) if os.path.isdir(path) else iter_zim_counted(path, stats)

    t0 = time.time()
    t_embed = [0.0]          # 98 chunks/s in the pilot conflated PDF parsing with
                             # GPU time. Which of the two to fix is not guessable.
    n_chunks = 0
    n_docs = 0
    chunk_chars = 0
    vecs = []
    batch_txt = []
    batch_meta = []

    os.makedirs(os.path.dirname(chunk_f), exist_ok=True)
    os.makedirs(os.path.dirname(vec_f), exist_ok=True)
    cf = open(chunk_f, "w", encoding="utf-8", newline="\n")

    def flush():
        nonlocal batch_txt, batch_meta
        if not batch_txt:
            return
        te = time.time()
        v = model.encode(batch_txt, batch_size=EMBED_BATCH,
                         normalize_embeddings=True, show_progress_bar=False)
        t_embed[0] += time.time() - te
        vecs.append(np.asarray(v, dtype=np.float16))
        for m, t in zip(batch_meta, batch_txt):
            m["text"] = t
            cf.write(json.dumps(m, ensure_ascii=False) + "\n")
        batch_txt = []
        batch_meta = []

    for kind, dpath, title, pmap, text in docs:
        n_docs += 1
        if args.limit and n_docs > args.limit:
            break
        # ONE DOCUMENT OUT OF 443,617 KILLED AN ELEVEN-HOUR RUN, 2026-09-13.
        #
        # A document in security.stackexchange sends llama_index's
        # SentenceSplitter._split recursing ~980 frames deep through nltk punkt
        # until the interpreter's stack runs out. RecursionError propagated out
        # of build_artifact and out of main(), and seventeen finished artifacts
        # survived only because save_registry() happens to run per artifact.
        # THAT IS LUCK, NOT ISOLATION - and the next two corpora on the roadmap
        # are Wikipedia at 7.7M documents and Stack Overflow at 74M chunks, both
        # 60+ hour runs with far more opportunity to contain text like this.
        #
        # NOT a raised recursion limit: that trades a catchable exception for a
        # C-stack segfault, which cannot be caught and takes the run with it.
        #
        # The document is NOT DROPPED if anything can be done with it. hard_split
        # shares no machinery with the splitter that just failed, so it cannot
        # fail the same way. Dropping is the last resort and is counted, named
        # and printed - the archive's rule is that nothing disappears quietly.
        try:
            parts = sp.split_text(text)
        except RecursionError:
            stats["split_fallback"] += 1
            if not stats["first_split_fail"]:
                stats["first_split_fail"] = "%s (recursion)" % dpath
            try:
                parts = hard_split(text)
            except Exception:
                stats["split_dropped"] += 1
                continue
        except Exception as e:
            stats["split_fallback"] += 1
            if not stats["first_split_fail"]:
                stats["first_split_fail"] = "%s (%s)" % (dpath, type(e).__name__)
            try:
                parts = hard_split(text)
            except Exception:
                stats["split_dropped"] += 1
                continue

        # Locate each chunk in the source text so a PDF chunk can name the page it
        # STARTS on, even though it may run past a page break. Located by prefix
        # with a moving cursor; if a chunk cannot be found (the splitter normalised
        # something), the cursor is used and the page is approximate rather than
        # wrong-by-construction. Cheap, and it keeps the citation honest.
        cursor = 0
        for i, part in enumerate(parts):
            page = None
            if pmap:
                probe = part[:80]
                at = text.find(probe, cursor) if probe else -1
                if at < 0:
                    at = cursor
                page = page_at(pmap, at)
                cursor = at + max(1, len(part) // 2)
            meta = {"id": "%d:%d:%d" % (src_id, n_docs, i),
                    "src": src_id, "path": dpath, "title": (title or "")[:200],
                    "i": i, "n": len(parts), "kind": kind}
            if page is not None:
                meta["page"] = page
            batch_meta.append(meta)
            batch_txt.append(part)
            n_chunks += 1
            chunk_chars += len(part)
        if len(batch_txt) >= EMBED_BATCH * 8:
            flush()
            if args.progress and n_docs % 2000 == 0:
                el = time.time() - t0
                sys.stdout.write("\r    %s docs, %s chunks, %.0f chunks/s   "
                                 % ("{:,}".format(n_docs), "{:,}".format(n_chunks),
                                    n_chunks / max(el, 0.001)))
                sys.stdout.flush()
    flush()
    cf.close()

    if not vecs:
        print("  %-52s NO CHUNKS - %s" % (name[:52], stats))
        return None

    arr = np.concatenate(vecs, axis=0)
    assert arr.shape[0] == n_chunks, "vector count %d != chunk count %d" % (arr.shape[0], n_chunks)
    assert arr.shape[1] == DIM, "expected %d dims, got %d" % (DIM, arr.shape[1])
    np.save(vec_f, arr)

    el = time.time() - t0
    registry["artifacts"][str(src_id)] = {
        "file": name, "shelf": os.path.dirname(rel), "lang": lang_of(name),
        "sha256": sha_from_shelf(path) if os.path.isfile(path) else None,
        "kiwix_book": name[:-4] if name.endswith(".zim") else None,
        "docs": n_docs, "chunks": n_chunks,
        "mean_chunk_chars": int(chunk_chars / max(1, n_chunks)),
        "stats": stats, "seconds": round(el, 1),
        "seconds_embed": round(t_embed[0], 1),
        "seconds_read_chunk": round(el - t_embed[0], 1),
        "chunks_per_sec_embed": round(n_chunks / max(t_embed[0], 0.001), 1),
    }

    print("  %-52s %s chunks, %s docs, %s"
          % (name[:52], "{:,}".format(n_chunks), "{:,}".format(n_docs), human(arr.nbytes)))
    print("      %.0f chunk/s overall  =  %.0f/s embedding (%.0fs GPU) + %.0fs reading"
          % (n_chunks / max(el, 0.001), n_chunks / max(t_embed[0], 0.001),
             t_embed[0], el - t_embed[0]))
    if stats["errors"]:
        print("      %d read errors, first: %s" % (stats["errors"], stats["first_error"]))
    # A SILENT GATE IS A GATE NOBODY CAN AUDIT. This path throws away the real
    # chunker for a document; it says so, and names the first one so it can be
    # looked at rather than wondered about.
    if stats["split_fallback"] or stats["split_dropped"]:
        print("      %d documents fell back to length-based chunking%s"
              % (stats["split_fallback"],
                 ", %d DROPPED entirely" % stats["split_dropped"]
                 if stats["split_dropped"] else ""))
        print("      first: %s" % stats["first_split_fail"])
    if stats["stub_docs"]:
        print("      %s documents below %d chars skipped as stubs"
              % ("{:,}".format(stats["stub_docs"]), MIN_DOC_CHARS))
    if stats["pdf_files"]:
        print("      %d PDFs -> %s pages (chunked ACROSS page breaks)%s" % (
            stats["pdf_files"], "{:,}".format(stats["pdf_pages"]),
            ", %d with no text" % stats["pdf_no_text"] if stats["pdf_no_text"] else ""))
        # SAY WHAT THE TABLE PASS DID. A silent quality gate is a gate nobody
        # can audit, and this one deliberately throws work away.
        if _TABLE_STATS["structured"] or _TABLE_STATS["left_as_text"]:
            tot = _TABLE_STATS["structured"] + _TABLE_STATS["left_as_text"]
            print("      tables: %s structured, %s left as flowing text (%.0f%% kept)"
                  % ("{:,}".format(_TABLE_STATS["structured"]),
                     "{:,}".format(_TABLE_STATS["left_as_text"]),
                     100.0 * _TABLE_STATS["structured"] / tot))
    return src_id


def load_registry():
    f = os.path.join(OUT, "sources.json")
    if os.path.exists(f):
        r = json.load(open(f, encoding="utf-8"))
        r.setdefault("next_id", max([int(k) for k in r.get("artifacts", {})] + [0]) + 1)
        return r
    import transformers, llama_index.core as lic
    return {
        "schema": 1,
        "built": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "scope": os.path.basename(SCOPE),
        "extractor": EXTRACTOR,
        "chunker": {"lib": "llama_index.core.node_parser.SentenceSplitter",
                    "llama_index": lic.__version__,
                    "chunk_size_tokens": CHUNK_TOKENS,
                    "chunk_overlap_tokens": CHUNK_OVERLAP,
                    "tokenizer": "bge-m3 (NOT tiktoken - see index-preflight.py)",
                    "transformers": transformers.__version__},
        "embedder": {"model": "bge-m3", "dim": DIM, "normalized": True,
                     "dtype_stored": "float16", "precision_compute": "fp16"},
        "min_doc_chars": MIN_DOC_CHARS,
        "next_id": 1,
        "artifacts": {},
    }


def save_registry(r):
    os.makedirs(OUT, exist_ok=True)
    f = os.path.join(OUT, "sources.json")
    json.dump(r, open(f, "w", encoding="utf-8"), indent=1, ensure_ascii=False)
    return f


# WHAT A BUILD LEAVES UNDONE, PRINTED AT THE END OF EVERY RUN THAT BUILT
# ANYTHING. The .jsonl and .npy files are the record; none of the node's three
# retrieval structures reads them directly. On 2026-09-24 a pass finished, these
# steps were not run, and 82.3% of the index was invisible to both retrieval
# paths while the node reported nothing wrong. 10-index/README.md has the same
# list; this is the copy nobody has to go and find.
NEXT_STEPS = """
THE NEW CHUNKS ARE NOT SEARCHABLE YET. With the node down, one command runs what
is due, in order, and stops at the first failure:

    python bin/ark.py index --plan            # what is due and why; runs nothing
    python bin/ark.py index                   # run it

It runs these, which still work by hand, in this order:

    python bin/index-pq-build.py --verify     # dense half, about 20 min of CPU at 39M chunks
    python bin/index-bm25.py --build          # keyword half
    python bin/index-provenance.py            # citations; resumes per artifact
    python bin/index-mirror-set.py            # its stamp keys on provenance
    python bin/ark.py rehash 10-index         # last, once nothing else writes there

then restart the node, and check /api/health: pq.covers should equal
pq.registry_chunks."""

# MEASURED 2026-09-13 on this build: Chroma HNSW holds 4,287 bytes per vector,
# all of it resident. DESIGN-vector-index-at-scale.md has the matrix.
CHROMA_BYTES_PER_CHUNK = 4287


def chroma_fits(registry, only, max_gb):
    """Say what a Chroma load would cost, and refuse one above max_gb. The
    2026-09-16 hazard was a command that looked like a small rebuild and was
    168 GB; the estimate is printed either way so nobody has to work it out."""
    arts = registry.get("artifacts", {})
    n = sum(a.get("chunks", 0) for a in arts.values()
            if only is None or os.path.basename(a.get("file", "")) in only)
    gb = n * CHROMA_BYTES_PER_CHUNK / 1e9
    print("\nChroma would hold %s chunks: about %.1f GB, all of it in RAM"
          % ("{:,}".format(n), gb))
    if gb > max_gb:
        print("REFUSED: above --chroma-max-gb %.0f. The node does not read Chroma;"
              " its dense index is 10-index/pq/. Raise the limit only on purpose."
              % max_gb)
        return False
    return True


def load_chroma(registry, only=None):
    """Chroma is DERIVED. This reads the .jsonl and .npy files and never touches a
    ZIM, which is the whole point: the index can be rebuilt, re-typed, or thrown
    away without re-embedding anything."""
    import numpy as np, chromadb
    cdir = os.path.join(OUT, "chroma")
    os.makedirs(cdir, exist_ok=True)
    # Exclusion list from bin/index-filter.py, if one exists. The chunks and
    # vectors on disk stay complete; the DERIVED index skips these ids. 15.4% of
    # the pass-1 build was byte-identical duplicates and a further slice was
    # MediaWiki licence footers - see index-audit.py.
    excl = set()
    ef = os.path.join(OUT, "excluded-ids.txt")
    if os.path.exists(ef):
        for line in open(ef, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#"):
                excl.add(line)
        print("  exclusion list: %s ids will be skipped" % "{:,}".format(len(excl)))

    client = chromadb.PersistentClient(path=cdir)
    col = client.get_or_create_collection("ark_pass1", metadata={"hnsw:space": "cosine"})
    total = 0
    for sid, a in sorted(registry["artifacts"].items(), key=lambda kv: int(kv[0])):
        if only and a["file"] not in only:
            continue
        slug = re.sub(r"[^A-Za-z0-9._-]", "_", a["file"])
        cf = os.path.join(OUT, "chunks", slug + ".jsonl")
        vf = os.path.join(OUT, "vectors", slug + ".f16.npy")
        if not (os.path.exists(cf) and os.path.exists(vf)):
            continue
        # STREAMED, not loaded. mdwiki alone is ~2.6M chunks: np.load().astype(float32)
        # would allocate 10.9 GB for the vectors and another 6-8 GB for its text, and
        # would do it at hour ten of a fourteen-hour build. mmap the vectors and read
        # the jsonl in blocks instead - the peak is one block, whatever the artifact.
        vecs = np.load(vf, mmap_mode="r")
        n_expected = vecs.shape[0]
        # Rebuilding with a different chunker produces FEWER chunks as often as more.
        # upsert alone would leave the surplus rows from the previous build behind,
        # answering queries from text that is no longer in the archive of record.
        try:
            col.delete(where={"src": int(sid)})
        except Exception:
            pass
        B = 5000
        seen = 0
        seen_rows = [0]          # position in the .npy, which advances even when skipped
        keep_rows = []
        ids, docs, metas = [], [], []
        with open(cf, encoding="utf-8") as fh:
            for line in fh:
                m = json.loads(line)
                cid = m.pop("id")
                row = seen_rows[0]
                seen_rows[0] += 1
                if cid in excl:
                    continue
                keep_rows.append(row)
                ids.append(cid)
                docs.append(m.pop("text"))
                metas.append(m)
                if len(ids) == B:
                    col.upsert(ids=ids, embeddings=vecs[keep_rows].astype(np.float32).tolist(),
                               documents=docs, metadatas=metas)
                    seen += len(ids)
                    ids, docs, metas, keep_rows = [], [], [], []
            if ids:
                col.upsert(ids=ids, embeddings=vecs[keep_rows].astype(np.float32).tolist(),
                           documents=docs, metadatas=metas)
                seen += len(ids)
        assert seen_rows[0] == n_expected, "%s: %d jsonl rows vs %d vectors" % (
            a["file"], seen_rows[0], n_expected)
        total += seen
        print("  loaded %-46s %s chunks" % (a["file"][:46], "{:,}".format(seen)))
    print("  chroma collection 'ark_pass1' now holds %s chunks" % "{:,}".format(col.count()))
    return total


def dry_run(targets):
    """Everything that can be checked without a GPU, a model or a write.

    The point is that the command printed in a scope file's header can be RUN,
    cheaply, before the eight-hour version of it. It answers three questions a
    build otherwise answers hours in: does every path in the scope exist, which
    artifacts are new, and which are already built and would therefore be
    RE-INDEXED in place - which is correct behaviour (an artifact keeps its id)
    but is not usually what someone adding a corpus intends."""
    reg = {}
    f = os.path.join(OUT, "sources.json")
    if os.path.exists(f):
        reg = json.load(open(f, encoding="utf-8")).get("artifacts", {})
    by_name = {a["file"]: a for a in reg.values()}

    print("DRY RUN - nothing is written\n")
    print("scope   %s" % SCOPE)
    print("index   %s\n" % OUT)

    missing, new, rebuilt, src_bytes, known_chunks = [], [], [], 0, 0
    for rel in targets:
        path = os.path.join(ROOT, rel)
        name = os.path.basename(rel.rstrip("/")) or rel
        slug = re.sub(r"[^A-Za-z0-9._-]", "_", name)
        have_vec = os.path.exists(os.path.join(OUT, "vectors", slug + ".f16.npy"))
        have_txt = os.path.exists(os.path.join(OUT, "chunks", slug + ".jsonl"))

        if not os.path.exists(path):
            missing.append(rel)
            print("  MISSING   %s" % rel)
            continue

        if os.path.isdir(path):
            n = sum(os.path.getsize(os.path.join(dp, fn))
                    for dp, _d, fns in os.walk(path) for fn in fns)
        else:
            n = os.path.getsize(path)
        src_bytes += n

        a = by_name.get(name)
        if a and have_vec and have_txt:
            rebuilt.append(name)
            known_chunks += a.get("chunks", 0)
            print("  RE-INDEX  %-46s %8s   id %s, %s chunks on record"
                  % (name[:46], human(n), a.get("id", "?"),
                     "{:,}".format(a.get("chunks", 0))))
        else:
            new.append(name)
            state = "partial on disk" if (have_vec or have_txt) else "new"
            print("  build     %-46s %8s   %s" % (name[:46], human(n), state))

    print("\n  %d in scope: %d new, %d would be re-indexed, %d MISSING"
          % (len(targets), len(new), len(rebuilt), len(missing)))
    print("  %s of source to read" % human(src_bytes))
    if rebuilt:
        print("  %s chunks already on record for the re-indexed artifacts;"
              " --resume skips them" % "{:,}".format(known_chunks))
    if missing:
        print("\n  REFUSING nothing - this is a dry run - but a real build would"
              " print MISSING and continue past them.")
    return


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pilot", action="store_true",
                    help="one artifact, capped - proves the pipeline in minutes")
    ap.add_argument("--artifact", help="build one artifact by filename")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--scope", default="",
                    help="scope file; a bare name resolves to bin/<name>. "
                         "Default index-scope-pass1.txt")
    ap.add_argument("--dry-run", action="store_true",
                    help="resolve the scope, check every path against the disk, "
                         "name what is new and what would be RE-INDEXED, then stop")
    ap.add_argument("--limit", type=int, default=0, help="cap documents per artifact")
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--chroma", action="store_true",
                    help="also load the built artifacts into a Chroma collection "
                         "(off by default: about 4.3 KB of RAM per chunk, and the "
                         "node does not read it)")
    ap.add_argument("--no-chroma", action="store_true",
                    help="accepted for old command lines; Chroma is off by default")
    ap.add_argument("--load-only", action="store_true",
                    help="rebuild Chroma from existing .jsonl/.npy, embed nothing. "
                         "Needs --chroma, and refuses above --chroma-max-gb")
    ap.add_argument("--chroma-max-gb", type=float, default=16.0,
                    help="refuse a Chroma load estimated above this (default 16)")
    ap.add_argument("--progress", action="store_true", default=True)
    ap.add_argument("--reset", action="store_true",
                    help="delete 10-index/{chunks,vectors,chroma} and sources.json first. "
                         "The pilot writes a CAPPED collection; a full build must not "
                         "inherit it")
    args = ap.parse_args()

    # --reset deletes the index and runs BEFORE everything else, so a --dry-run
    # that also carried it would destroy 10-index while printing "nothing is
    # written". Refuse the pair rather than order around it.
    if args.reset and args.dry_run:
        sys.exit("--reset and --dry-run are contradictory: --reset deletes "
                 "10-index/{chunks,vectors,chroma} and sources.json")

    if args.reset:
        import shutil
        for d in ("chunks", "vectors", "chroma"):
            shutil.rmtree(os.path.join(OUT, d), ignore_errors=True)
        f = os.path.join(OUT, "sources.json")
        if os.path.exists(f):
            os.remove(f)
        print("reset: 10-index cleared\n")

    global SCOPE
    SCOPE = resolve_scope(args.scope)
    scope = load_scope(SCOPE)
    if args.pilot:
        # zimgit-post-disaster: the PDF-container case, the html case, and the
        # highest-consequence content in pass 1, in one small artifact.
        targets = [r for r in scope if "zimgit-post-disaster" in r] or scope[:1]
        args.limit = args.limit or 300
    elif args.artifact:
        targets = [r for r in scope if os.path.basename(r.rstrip("/")) == args.artifact]
        if not targets:
            sys.exit("not in scope: %s" % args.artifact)
    elif args.all:
        targets = scope
    elif args.load_only:
        targets = []
    else:
        sys.exit("choose --pilot, --artifact NAME, --all, or --load-only")

    if args.dry_run:
        return dry_run(targets)

    registry = load_registry()

    if args.load_only:
        if not args.chroma:
            sys.exit("--load-only rebuilds a Chroma collection from every artifact on "
                     "record, and Chroma is off by default.\n"
                     "The node's dense index is 10-index/pq/: python bin/index-pq-build.py "
                     "--verify\nTo build Chroma anyway, add --chroma.")
        if not chroma_fits(registry, None, args.chroma_max_gb):
            sys.exit(1)
        load_chroma(registry)
        return

    print("Ark index build - %d artifact(s)%s" % (
        len(targets), ", limit %d docs each" % args.limit if args.limit else ""))
    print("chunk %d BGE-M3 tokens, overlap %d, fp16, network forbidden\n"
          % (CHUNK_TOKENS, CHUNK_OVERLAP))

    built = []
    for rel in targets:
        if not os.path.exists(os.path.join(ROOT, rel)):
            print("  MISSING: %s" % rel)
            continue
        sid = build_artifact(rel, args, registry)
        if sid is not None:
            built.append(os.path.basename(rel.rstrip("/")))
        save_registry(registry)

    if built and args.chroma:
        if chroma_fits(registry, set(built), args.chroma_max_gb):
            print("\nloading Chroma from the .jsonl/.npy files (nothing re-embedded)")
            load_chroma(registry, only=set(built))

    # "scope" is written once, when sources.json is created, and after a second
    # corpus it describes only the first. Rather than rewrite a field other tools
    # read, append what each run actually built. A registry that names one scope
    # while holding two is the provenance version of a stale manifest.
    if built:
        registry.setdefault("scopes", []).append(
            {"file": os.path.basename(SCOPE),
             "built": time.strftime("%Y-%m-%dT%H:%M:%S"),
             "artifacts": sorted(built)})

    print("\nregistry: %s" % save_registry(registry))
    tot = sum(a["chunks"] for a in registry["artifacts"].values())
    print("total chunks on record: %s" % "{:,}".format(tot))
    if built and os.environ.get("ARK_SETUP"):
        print("\n(setup runs the steps that make these chunks searchable next)")
    elif built:
        print(NEXT_STEPS)


if __name__ == "__main__":
    main()
