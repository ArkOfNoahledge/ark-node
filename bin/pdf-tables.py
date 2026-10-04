#!/usr/bin/env python3
"""
pdf-tables.py - table-aware page text for the index build. Standard library
plus PyMuPDF, which is already vendored (09-software/python-wheels, pinned in
requirements-lock.txt as pymupdf==1.28.2). No new dependency, no network.

    python bin/pdf-tables.py <file.pdf> [page]      # inspect one page
    python bin/pdf-tables.py <file.pdf> --audit     # every table, kept or not

WHY THIS EXISTS. On 2026-09-05 the answer pane produced a grounded, correctly
cited, DANGEROUS answer: five teaspoons of 5% bleach per litre of drinking water,
against a correct figure of two drops. The model was not at fault. It was handed
this, from `page.get_text()` on ehb_water_EN_2016_web.pdf page 43:

    For 1 liter or 1 quart  For 1 gallon or 4 liters  For 5 gallons or 20 liter
    For a 50 gallon or 200 liter barrel  5 teaspoons  1/2 teaspoon  8 drops
    2 drops  WATER BLEACH

A two-row table. get_text() follows visual reading order, so the whole WATER row
came first and the whole BLEACH row second, and the pairing was gone. The model
paired them in the only order available to it and inverted the entire dose scale.
BUILD-LOG, "The first dangerous answer, and it was grounded".

WHAT THIS CHANGES. find_tables() per page; a table that passes a quality gate is
rendered as structured text and its region is REMOVED from the flowing text; a
table that fails the gate is left exactly as it was and the page is marked.

THE GATE IS THE POINT, NOT THE EXTRACTION. Measured over three real PDFs from
this archive, find_tables() is excellent on ruled tables and poor on unruled ones,
and its poor output is worse than the flattened original - whole columns collapse
into a single cell, producing scrambled text wearing a table's clothes. That
asserts a structure which is not there, which is the same defect this file exists
to remove. Roughly half the detected tables are rejected. A rejected table costs
nothing: the page keeps the text it always had.
"""


import re

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

# SOFT HYPHEN IS THE OPPOSITE OPERATION TO EVERYTHING ELSE IN _WS, WHICH IS WHY
# IT IS NOT IN IT. U+00AD is the discretionary hyphen a PDF producer inserts at a
# line break. It is invisible, and the two halves it separates are ONE WORD.
#
# Adding it to _WS above would map it to a space and make the break permanent:
# "pres<SHY> sure" becomes "pres sure", two tokens forever. Measured 2026-09-05:
# FTS5 matches `pressure` against none of "pres<SHY> sure", "pres<SHY>sure" or
# "pres-\nsure", and the USDA dial-gauge canning guide states its figure as
# "you should have selected a pres<SHY> sure of 10 lbs" - a sentence carrying a
# pressure figure that does not contain the word pressure. 1,742 chunk lines
# across 24 artifacts, concentrated in the safety-critical shelves.
#
# So the soft hyphen and any whitespace AFTER it are deleted, joining the halves,
# and this must run BEFORE _WS collapses whitespace. Defined here rather than in
# index-build.py because index-build.py imports this module, and one definition
# with two callers is the whole point of tonight.
_SHY = re.compile(r"(?<=[^\W\d_])\u00ad\s*(?=[^\W\d_])", re.UNICODE)


def dehyphenate(t):
    """Rejoin a word a PDF broke across a line. Run before whitespace collapse."""
    if "\u00ad" not in t:
        return t
    t = _SHY.sub("", t)
    # Any soft hyphen left is not between two letters. It is invisible either
    # way, so it is removed rather than left to reach a tokenizer or a reader.
    return t.replace("\u00ad", "")


def _clean(c):
    return _WS.sub(" ", dehyphenate((c or "").replace("\n", " "))).strip()


# NUMERIC TOKENS, NOT DIGITS. Counting digits rejected a correctly extracted
# food-safety table because "54\u00b0C to 60\u00b0C (130\u00b0F to 140\u00b0F)" holds ten digit
# characters and four numbers. A column that has collapsed holds a long RUN of
# separate figures; a sentence about temperatures does not.
_NUMS = re.compile(r"\d+(?:[.,]\d+)?")


def _quality(rows, ncol, orig_ncol=None, bbox=None, page_width=None):
    """Does this look like a table PyMuPDF actually understood?

    MEASURED, NOT ASSUMED. Over three real PDFs, find_tables() is excellent on
    ruled tables and poor on unruled ones, and its poor output is WORSE than the
    flattened original: whole columns collapse into one cell, so the result is
    scrambled text wearing a table's clothes. It asserts a structure that is not
    there, which is the same defect this whole change exists to remove.

    A table that fails these checks is left as flowing text and the page is
    MARKED, so the surface can say a table on this page could not be structured.
    That warning is worth having only because it is now rare.
    """
    if ncol < 2 or len(rows) < 2:
        return False, "too small"
    if ncol > 8:
        return False, "%d columns - almost always a mis-detected grid" % ncol
    cells = [c for r in rows for c in r]
    filled = sum(1 for c in cells if c)
    if filled / float(len(cells)) < 0.40:
        return False, "only %d of %d cells have content" % (filled, len(cells))
    longest = max((len(c) for c in cells), default=0)
    if longest > 200:
        return False, "a cell holds %d characters - a column collapsed" % longest
    # A COLLAPSED COLUMN IS NUMBERS WITH NOTHING BETWEEN THEM.
    # Counting numbers alone rejected a food-safety cooking table whose cells
    # legitimately read "63C (145F) for 15 seconds; 60C (140F) for 12 minutes" -
    # nine figures in a sentence. The collapsed FAO cell reads "Percent 43.2 10.3
    # 7.2 2.2 1.4 24.1 6.9 38.1" - twenty figures and no sentence. The ratio of
    # figures to words separates them; the count alone does not.
    for c in cells:
        nums = len(_NUMS.findall(c))
        words = max(1, len(c.split()))
        if nums >= 6 and nums / float(words) > 0.6:
            return False, "a cell is %d figures in %d words - a column collapsed" % (nums, words)

    # ---------------------------------------------------------------------
    # ADDED 2026-09-05. THE THREE CHECKS ABOVE PASSED BOTH OF THE ONLY TWO
    # TABLES THIS GATE ACCEPTED IN THE USDA HOME CANNING GUIDE, AND BOTH WERE
    # WRONG IN THE SAME WAY: the column that identifies the row was missing, so
    # what survived read as definite and could not be attributed to anything.
    # That is worse than no table, because a reader cannot see that it is
    # partial. Every check below asks one question - can a row still be
    # identified? - and a table that cannot answer it is left as flowing text.
    # ---------------------------------------------------------------------

    # SLICED COLUMNS. find_tables cut a 53-point strip through the middle of the
    # numbers on page 23, so "25" arrived as "2" and "5" in separate cells and
    # rendered as "2: 5." Real data cells are rarely one character, and almost
    # never one character in the majority.
    filled_cells = [c for c in cells if c]
    if len(filled_cells) >= 6:
        one = sum(1 for c in filled_cells if len(c) == 1)
        if one / float(len(filled_cells)) >= 0.40:
            return False, ("%d of %d cells are a single character - the columns were "
                           "sliced mid-value" % (one, len(filled_cells)))

    # A FRAGMENT OF A WIDER TABLE. Page 32's plan table is nine columns; the
    # detector returned it with seven of them empty, and dropping the empties
    # left "1/2 cup: 12", a serving size paired with a count and no food name.
    # When more than half the detected columns hold nothing, what remains is a
    # slice of a structure, not the structure.
    if orig_ncol and orig_ncol >= 4 and ncol * 2 <= orig_ncol:
        return False, ("%d of %d columns are empty - this is a slice of a wider "
                       "table" % (orig_ncol - ncol, orig_ncol))

    # A SLIVER BY GEOMETRY. Cheapest of the three and it catches the page 23
    # case on its own: a multi-column table occupying 9%% of the page width is a
    # strip cut out of something, not a table. Real tables in this corpus run
    # 55 to 75%% of the text width.
    if page_width and bbox:
        frac = (bbox[2] - bbox[0]) / float(page_width)
        if frac < 0.18:
            return False, ("the table is %.0f%%%% of the page width - a fragment, not "
                           "a table" % (100.0 * frac))

    return True, ""


def _render_table(tb, page_width=None):
    """A table as text that survives being flattened again downstream.

    TWO COLUMNS BECOME PROSE, THREE OR MORE BECOME MARKDOWN. The chunk text is
    read by three things with different needs: BM25 wants words, BGE-M3 wants
    natural language, and the model wants unambiguous pairing. Prose rows give
    all three at two columns. Beyond that prose stops being unambiguous, so the
    pairing wins and the retrievers take the hit.

    Returns (text, ok, why). ok=False means DO NOT use this - keep the page's
    plain text for the region and mark the page instead.
    """
    rows = [[_clean(c) for c in r] for r in tb.extract()]
    rows = [r for r in rows if any(r)]
    if not rows:
        return "", False, "empty"

    ncol = max(len(r) for r in rows)
    orig_ncol = ncol
    rows = [r + [""] * (ncol - len(r)) for r in rows]
    keep = [i for i in range(ncol) if any(r[i] for r in rows)]
    rows = [[r[i] for i in keep] for r in rows]
    ncol = len(keep)
    if ncol == 0:
        return "", False, "empty"

    # A ROW WITH ONE NON-EMPTY CELL IS A CAPTION, NOT A PAIR.
    # The Hesperian chlorine table's first row is ['WATER BLEACH', ''] - a merged
    # header cell spanning both columns. Treated as a data row it would pair the
    # caption with an empty string and read as a fact.
    caption = ""
    while rows and sum(1 for c in rows[0] if c) == 1:
        caption = next(c for c in rows[0] if c)
        rows = rows[1:]
    if not rows:
        return "", False, "caption only"

    # PREFER THE LIBRARY'S OWN HEADER over inferring one. When find_tables marks
    # a header as external it means the header sits ABOVE the detected grid, and
    # inferring row 0 as the header would silently promote the first data row.
    header = None
    try:
        h = getattr(tb, "header", None)
        names = [_clean(x) for x in (h.names or [])] if h else []
        if names and any(names) and len(names) == ncol:
            header = names
            if not getattr(h, "external", False) and rows and rows[0] == names:
                rows = rows[1:]
    except Exception:
        header = None

    ok, why = _quality(rows, ncol, orig_ncol, tb.bbox, page_width)
    if not ok:
        return "", False, why

    head = "Table"
    if caption:
        head += " (%s)" % caption

    if ncol == 2:
        parts = ["%s: %s." % (a, b) if b else "%s." % a for a, b in rows]
        return "%s. %s" % (head, " ".join(parts)), True, ""

    # A HEADER WITH ONE NON-EMPTY CELL IS A MERGED CAPTION, NOT COLUMN NAMES.
    # The food-safety control-point table reports its header as
    # ['Critical control point Type of food Temperature', '', ''] - all three
    # column names run into one cell. Emitting that as a markdown header printed
    # the caption twice and gave two of the three columns no name at all. Where
    # there are no real column names, the rows are emitted without a header line;
    # the caption already carries them, and inventing names is worse than none.
    hdr = header if (header and sum(1 for c in header if c) >= 2) else None
    if hdr is None and not header and rows and sum(1 for c in rows[0] if c) == ncol:
        hdr, rows = rows[0], rows[1:]
    out = [head + ":"]
    if hdr:
        out.append("| " + " | ".join(hdr) + " |")
        out.append("|" + "|".join(["---"] * ncol) + "|")
    for r in rows:
        out.append("| " + " | ".join(r) + " |")
    return "\n".join(out), True, ""


def _inside(block, boxes, pad=2.0):
    x0, y0, x1, y1 = block[:4]
    cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
    for bx0, by0, bx1, by1 in boxes:
        if bx0 - pad <= cx <= bx1 + pad and by0 - pad <= cy <= by1 + pad:
            return True
    return False


def page_text(page):
    """Plain text with any table replaced, in place, by a structured rendering.

    THE TABLE REGION IS REMOVED FROM THE FLOWING TEXT, not merely appended to.
    Leaving both would put the correct pairing and the scrambled one in the same
    chunk, and a model reading both has no way to know which to believe - which
    is worse than the original defect, because it looks like corroboration.
    """
    try:
        finder = page.find_tables()
        tables = list(finder.tables)
    except Exception:
        return _WS.sub(" ", page.get_text()).strip(), 0, []

    if not tables:
        return _WS.sub(" ", page.get_text()).strip(), 0, []

    rendered, boxes, unstructured = [], [], []
    for tb in tables:
        txt, ok, why = _render_table(tb, page.rect.width)
        if ok:
            rendered.append((tb.bbox[1], txt))
            boxes.append(tb.bbox)
        else:
            unstructured.append(why)

    pieces = []
    for b in page.get_text("blocks"):
        if len(b) > 6 and b[6] != 0:      # image block
            continue
        if _inside(b, boxes):
            continue
        t = _WS.sub(" ", b[4]).strip()
        if t:
            pieces.append((b[1], t))
    pieces.extend(rendered)
    pieces.sort(key=lambda p: p[0])
    return ("\n".join(t for _, t in pieces).strip(),
            len(rendered), unstructured)


# The three checks added on 2026-09-05 can only ever ADD rejections, never
# passes, so the delta against the previous behaviour is exactly the number of
# tables rejected for one of these three reasons. That makes the regression
# check one line of output to read.
_NEW_REASONS = ("single character", "columns are empty", "of the page width")


if __name__ == "__main__":
    import sys, os, collections
    import pymupdf
    if len(sys.argv) < 2:
        print(__doc__.strip().splitlines()[0]); sys.exit(2)

    if "--audit" in sys.argv:
        # AUDIT TAKES A DIRECTORY AS WELL AS A FILE. The checks exist because of
        # GUIDE01_HomeCan_rev0715.pdf, where the only two tables the gate had
        # ever accepted in forty pages were both missing the column that
        # identifies the row. Expected there: page 23 rejected for single
        # characters, page 32 for empty columns. Run it over a shelf to see
        # whether anything ELSE was relying on the old behaviour.
        target = sys.argv[1]
        if not os.path.isabs(target):
            root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            if os.path.exists(os.path.join(root, target)):
                target = os.path.join(root, target)
        # A PATH THAT DOES NOT EXIST IS NOT AN EMPTY AUDIT. Given a bad path,
        # the first version fell through to the single-file branch, failed to
        # open it, and printed "0 tables, 0 newly rejected" - which reads exactly
        # like a clean regression check. Caught on the first real run, 2026-09-05.
        if not os.path.exists(target):
            sys.exit("no such path: %s\n"
                     "  (paths are relative to the archive root, e.g. "
                     "07-corpora-supplemental/hesperian)" % target)
        if os.path.isdir(target):
            paths = []
            for r, _d, fs in os.walk(target):
                paths += [os.path.join(r, f) for f in sorted(fs)
                          if f.lower().endswith(".pdf")]
        else:
            paths = [target]
        if not paths:
            sys.exit("no PDFs under %s" % target)
        verbose = "--verbose" in sys.argv
        reasons = collections.Counter()
        kept = npdf = npage = 0
        print("auditing %d PDF(s) under %s" % (len(paths), target))
        for p in paths:
            try:
                doc = pymupdf.open(p)
            except Exception as e:
                print("  unreadable: %s (%s)" % (os.path.basename(p), e)); continue
            npdf += 1
            for pno in range(doc.page_count):
                page = doc[pno]
                npage += 1
                try:
                    tables = list(page.find_tables().tables)
                except Exception:
                    continue
                for tb in tables:
                    txt, ok, why = _render_table(tb, page.rect.width)
                    if ok:
                        kept += 1
                        if verbose:
                            print("  KEEP %s p%d  %s"
                                  % (os.path.basename(p), pno + 1,
                                     txt[:70].replace("\n", " ")))
                    else:
                        reasons[re.sub(r"\d+", "N", why.split(" - ")[0])] += 1
                        if verbose and any(r in why for r in _NEW_REASONS):
                            print("  NEW-REJECT %s p%d  %s"
                                  % (os.path.basename(p), pno + 1, why))
                            # THE ROWS, NOT JUST THE VERDICT. A count of newly
                            # rejected tables cannot tell you whether the gate is
                            # working or whether a threshold is wrong. The rows can.
                            for row in tb.extract()[:4]:
                                print("        %s" % [
                                    _WS.sub(" ", (c or "")).strip()[:24] for c in row])
            doc.close()
        tot = kept + sum(reasons.values())
        print("\n%d PDFs, %d pages, %d tables: %d structured, %d left as text%s"
              % (npdf, npage, tot, kept, tot - kept,
                 " (%.0f%% kept)" % (100.0 * kept / tot) if tot else ""))
        print("\nrejected, by reason:")
        for k, v in reasons.most_common():
            print("  %6d  %s%s" % (v, k,
                  "   <- NEW 2026-09-05" if any(r in k for r in _NEW_REASONS) else ""))
        newly = sum(v for k, v in reasons.items()
                    if any(r in k for r in _NEW_REASONS))
        print("\n%d table(s) newly rejected. Each was previously emitted as a"
              " structured table.\n" % newly)
        sys.exit(0)

    doc = pymupdf.open(sys.argv[1])
    pages = [int(a) for a in sys.argv[2:] if a.isdigit()] or [1]
    for p in pages:
        txt, n, unstructured = page_text(doc[p - 1])
        print("=== page %d: %d table(s) structured, %d left as text ==="
              % (p, n, len(unstructured)))
        for w in unstructured:
            print("    not structured: %s" % w)
        print(txt[:3000])
