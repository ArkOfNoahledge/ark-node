#!/usr/bin/env python3
"""Repair stored chunk text in place of a re-index.

WHY THIS EXISTS INSTEAD OF A REBUILD. Two text defects were measured on
2026-09-05. Neither changes which chunks exist, where they start, or what they
are about, so neither needs new embeddings - and re-embedding the affected
artifacts would cost hundreds of thousands of chunks to fix characters.

  1. U+00AD, the soft hyphen a PDF producer inserts at a line break. FTS5 matches
     `pressure` against none of "pres<SHY> sure", "pres<SHY>sure" or "pres-\\nsure".
     The USDA dial-gauge canning guide states its figure as "you should have
     selected a pres<SHY> sure of 10 lbs": a sentence carrying a pressure figure
     that does not contain the word pressure. 1,742 chunk lines, 24 artifacts.

  2. Separators that are not \\s - C0 controls, zero width, exotic spaces - used
     as space glyphs. These do NOT break BM25: FTS5's unicode61 already treats
     every one of them as a token separator, which is the measurement that
     retired the "7,218 unsearchable chunks" item. They are repaired anyway
     because the same text is handed to the model and shown to the reader, and
     "Canner\\x1aPressure\\x1a(PSI)" is not what either should receive.

WHAT IT DELIBERATELY DOES NOT DO: collapse ordinary whitespace. The build path's
_WS does, but the stored text is past that point and its newlines are load
bearing - bin/pdf-tables.py emits markdown tables, and flattening those would
destroy the pairing that the whole table change exists to protect.

AFTER THIS RUNS, TWO THINGS ARE STALE AND MUST BE REBUILT:
    python bin/index-bm25.py --build          minutes, reads only chunks/*.jsonl
    python bin/index-provenance.py --rebuild  ~40s, byte offsets into those files
The two flags differ - bm25 takes --build, provenance takes --rebuild - and this
file said --build for both on its first run, so the provenance step failed with a
usage error at the exact moment the offsets had just been invalidated. A wrong
flag in a warning is worse than no warning: it reads as done.
Provenance stores the byte offset and length of each JSON line. Repairing text
changes line lengths, so every offset after the first repair in a file is wrong.
A citation would then open the wrong passage while reporting success, which is
the failure this archive keeps producing and the reason this warning is here
rather than in a commit message.

NON-DESTRUCTIVE BY CONSTRUCTION. Writes <file>.repaired, verifies it line for
line and field for field, and only then swaps, keeping the original as
<file>.orig until you remove it. 10-index exists on one drive as of this writing.

Usage:
    python bin/index-repair-text.py              report only, writes nothing
    python bin/index-repair-text.py --apply      repair, verify, swap
"""

import io, os, re, sys, json, glob, importlib.util

# NO BYTECODE. This module loads bin/pdf-tables.py through importlib, and Python
# writes a .pyc beside any module it IMPORTS rather than runs. That is how three
# .pyc files reached bin/'s checksum manifest on 2026-09-04, and how one reached
# the cold copy's mirror list on 2026-09-05 - caught by a --dry-run, not by any
# check that exists to catch it. ark-api/serve.py has carried this line since the
# first time; these did not.
sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
CHUNKS = os.path.join(os.path.dirname(HERE), "10-index", "chunks")

# ONE DEFINITION, IMPORTED. dehyphenate lives in pdf-tables.py because
# index-build.py already imports that module, and a second copy of the rule here
# is precisely the failure bin/check-copies.sh was written for.
_spec = importlib.util.spec_from_file_location(
    "pdf_tables", os.path.join(HERE, "pdf-tables.py"))
pdf_tables = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pdf_tables)

# The build's _WS MINUS \s. Ordinary whitespace is left exactly as it is.
_ODD = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f ​"
                  " -   　]+")


def repair(t):
    if not t:
        return t, 0, 0
    a = pdf_tables.dehyphenate(t)
    n_shy = 1 if a != t else 0
    b = _ODD.sub(" ", a)
    n_odd = 1 if b != a else 0
    return b, n_shy, n_odd


def process(path, apply):
    out = path + ".repaired"
    lines = changed = shy = odd = 0
    w = io.open(out, "w", encoding="utf-8", newline="\n") if apply else None
    try:
        with io.open(path, encoding="utf-8") as fh:
            for line in fh:
                lines += 1
                d = json.loads(line)
                t2, a, b = repair(d.get("text", ""))
                ti2, c, e = repair(d.get("title", "") or "")
                if a or c: shy += 1
                if b or e: odd += 1
                if t2 != d.get("text", "") or ti2 != (d.get("title") or ""):
                    changed += 1
                    d["text"] = t2
                    if d.get("title") is not None:
                        d["title"] = ti2
                if w:
                    w.write(json.dumps(d, ensure_ascii=False) + "\n")
    finally:
        if w: w.close()
    return lines, changed, shy, odd, out


def verify(orig, new):
    """Every field except text and title must be byte-identical, and the line
    count must match. A repair that silently dropped a chunk would be invisible
    downstream until a citation pointed at nothing."""
    n = 0
    with io.open(orig, encoding="utf-8") as a, io.open(new, encoding="utf-8") as b:
        for la, lb in zip(a, b):
            n += 1
            da, db = json.loads(la), json.loads(lb)
            if list(da.keys()) != list(db.keys()):
                return False, "line %d: key order changed" % n
            for k in da:
                if k in ("text", "title"):
                    continue
                if da[k] != db[k]:
                    return False, "line %d: field %r changed" % (n, k)
        if a.readline() or b.readline():
            return False, "line count differs"
    return True, "%d lines" % n


def main():
    apply = "--apply" in sys.argv
    if not os.path.isdir(CHUNKS):
        sys.exit("no chunks directory at %s" % CHUNKS)
    files = sorted(glob.glob(os.path.join(CHUNKS, "*.jsonl")))
    if not files:
        sys.exit("no chunk files found")

    print("\n%-52s %9s %9s %7s %7s" % ("ARTIFACT", "LINES", "CHANGED", "SHY", "ODD"))
    tl = tc = 0
    pending = []
    for f in files:
        lines, changed, shy, odd, out = process(f, apply)
        tl += lines; tc += changed
        if changed or not apply:
            print("%-52s %9d %9d %7d %7d" % (os.path.basename(f), lines, changed, shy, odd))
        if apply:
            if changed == 0:
                os.remove(out)
                continue
            ok, why = verify(f, out)
            if not ok:
                os.remove(out)
                sys.exit("VERIFY FAILED on %s: %s - nothing swapped" % (f, why))
            pending.append((f, out))

    print("\n%d lines, %d changed" % (tl, tc))
    if not apply:
        print("\nreport only. re-run with --apply to repair.\n")
        return

    for f, out in pending:
        os.replace(f, f + ".orig")
        os.replace(out, f)
    print("swapped %d file(s); originals kept as *.orig\n" % len(pending))
    if pending:
        print("  NOW REBUILD BOTH, IN THIS ORDER:")
        print("    python bin/index-bm25.py --build")
        print("    python bin/index-provenance.py --rebuild")
        print("  Provenance byte offsets are stale until it is rebuilt, and a")
        print("  stale offset opens the wrong passage while reporting success.\n")


if __name__ == "__main__":
    main()
