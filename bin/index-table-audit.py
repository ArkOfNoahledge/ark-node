#!/usr/bin/env python3
"""How much of the indexed text comes from HTML tables, and what it lost.

THE QUESTION THIS ANSWERS. bin/index-build.py's html_to_text strips markup with a
single tag regex. There is no <table>, <tr>, <td> or <th> handling anywhere in the
HTML path, so a table does not merely lose its column names: it loses its ROW
BOUNDARIES. A four-column table becomes one unbroken run of cell text in which
nothing marks where a row ended. The note on record - that HTML serialises
row-major and therefore preserves pairing - is half true. Adjacent cells stay
adjacent; nothing says which cells were a row.

The size of that was never counted, and the PDF work this month showed twice that
an uncounted defect is a mis-sized one. This counts it.

IT MEASURES AFTER BOILERPLATE REMOVAL, NOT BEFORE. Most tables in a MediaWiki ZIM
are navboxes, and strip_boilerplate already deletes those. Counting raw <table>
tags would inflate the answer by a large and unknown factor, so this imports the
real html_to_text and the real _STRIP rules from index-build.py and measures what
actually survives into a chunk.

THE ESTIMATOR. For a sampled article, table share is the extracted-text length
inside <table> elements over the extracted-text length of the whole article.
Chunks are cut from that same text at a fixed token size, so text share is a
sound proxy for chunk share. Reported per artifact and scaled by that artifact's
real chunk count from provenance.

Read only. Touches no index file and writes nothing.

Usage:
    python bin/index-table-audit.py                 sample 300 articles per ZIM
    python bin/index-table-audit.py --sample 1000   slower, tighter
    python bin/index-table-audit.py --only wikem    one artifact, substring match
"""

import os, sys, re, json, argparse, sqlite3, importlib.util

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(ROOT, "10-index")

_spec = importlib.util.spec_from_file_location("ark_index_build",
                                               os.path.join(HERE, "index-build.py"))
ib = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ib)

# Non-greedy, so a nested table closes at the inner </table>. Nested tables are
# rare in article prose and the effect is to UNDER-count, which is the safe
# direction for a number that will be used to justify work.
_TABLE = re.compile(r"(?is)<table\b.*?</table>")
_TH = re.compile(r"(?i)<th\b")
_TR = re.compile(r"(?i)<tr\b")
_TD = re.compile(r"(?i)<td\b")


def audit_html(raw):
    """Returns (total_chars, table_chars, n_tables, n_with_header, n_rows)."""
    try:
        body = raw.decode("utf-8", "ignore")
    except Exception:
        return 0, 0, 0, 0, 0
    text, _ = ib.html_to_text(raw)
    total = len(text)
    if total < ib.MIN_DOC_CHARS:
        return 0, 0, 0, 0, 0
    # Boilerplate first, exactly as the real path does, so navbox tables are gone
    # before anything is counted.
    kept = ib._SCRIPT.sub(" ", body)
    kept = ib.strip_boilerplate(kept)
    tchars = ntab = nhdr = nrow = 0
    for m in _TABLE.finditer(kept):
        frag = m.group(0)
        ftext = ib._WS.sub(" ", ib._TAG.sub(" ", frag)).strip()
        if len(ftext) < 40:            # a layout table holding an image or a link
            continue
        ntab += 1
        tchars += len(ftext)
        if _TH.search(frag):
            nhdr += 1
        nrow += len(_TR.findall(frag))
    return total, min(tchars, total), ntab, nhdr, nrow


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=300)
    ap.add_argument("--only")
    a = ap.parse_args()

    from libzim.reader import Archive

    prov = os.path.join(OUT, "provenance.sqlite3")
    chunks = {}
    if os.path.exists(prov):
        con = sqlite3.connect("file:%s?mode=ro" % prov, uri=True)
        for f, n in con.execute(
                "SELECT a.file, COUNT(*) FROM chunk c JOIN artifact a ON a.src=c.src "
                "GROUP BY a.file"):
            chunks[f] = n
        con.close()

    scope = [l.split("#")[0].strip() for l in open(
        os.path.join(HERE, "index-scope-pass1.txt"), encoding="utf-8")]
    zims = [p for p in scope if p and p.lower().endswith(".zim")]
    if a.only:
        zims = [p for p in zims if a.only.lower() in os.path.basename(p).lower()]
    if not zims:
        sys.exit("no ZIMs matched")

    print("\n%-46s %7s %7s %7s %7s %9s" %
          ("ARTIFACT", "DOCS", "TABLE%", "W/ <TH>", "ROWS/T", "EST CHUNKS"))
    grand = 0
    for rel in zims:
        path = rel if os.path.isabs(rel) else os.path.join(ROOT, rel)
        if not os.path.exists(path):
            print("%-46s  (missing)" % os.path.basename(path)); continue
        try:
            arc = Archive(path)
        except Exception as e:
            print("%-46s  (unreadable: %s)" % (os.path.basename(path), e)); continue
        get = getattr(arc, "_get_entry_by_id", None) or getattr(arc, "get_entry_by_id")
        n = arc.entry_count
        step = max(1, n // max(1, a.sample * 3))
        seen = ttot = ttab = ntab = nhdr = nrow = 0
        i = 0
        while i < n and seen < a.sample:
            try:
                e = get(i)
                if not e.is_redirect:
                    it = e.get_item()
                    if str(it.mimetype).split(";")[0].startswith("text/html"):
                        tot, tab, k, h, r = audit_html(bytes(it.content))
                        if tot:
                            seen += 1; ttot += tot; ttab += tab
                            ntab += k; nhdr += h; nrow += r
            except Exception:
                pass
            i += step
        if not seen:
            print("%-46s  (no html sampled)" % os.path.basename(path)); continue
        share = ttab / float(ttot) if ttot else 0.0
        base = os.path.basename(path)
        est = int(round(share * chunks.get(base, 0)))
        grand += est
        print("%-46s %7d %6.1f%% %6.1f%% %7.1f %9s" %
              (base[:46], seen, 100.0 * share,
               100.0 * nhdr / ntab if ntab else 0.0,
               nrow / float(ntab) if ntab else 0.0,
               "{:,}".format(est)))
    print("\nestimated chunks carrying table text: %s" % "{:,}".format(grand))
    print("every one of them has lost its row boundaries; the W/ <TH> column is\n"
          "the share of those tables that also had column headers to lose.\n")


if __name__ == "__main__":
    main()
