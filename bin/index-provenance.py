#!/usr/bin/env python3
"""
index-provenance.py - build the provenance store the citation path needs.

    python bin/index-provenance.py              # build or resume
    python bin/index-provenance.py --status     # what is done, what is left
    python bin/index-provenance.py --rebuild    # start over

WHY THIS EXISTS, AND WHY IT IS NOT A DUPLICATE OF CHROMA.

`NODE-ARCHITECTURE.md` §3 and §5 say the provenance store is missing and that
spec §9.2 depends on it. That was written 2026-08-30, before the index existed.
It is now half wrong and worth stating precisely, because the half that is right
is the half this file fixes.

**The provenance DATA is already there.** `10-index/sources.json` carries a
`kiwix_book` for all 25 ZIM artifacts; every chunk row carries `path`, `title`
and `i`/`n`; PDF chunks carry `page`. Chunk to artifact to a resolvable URL has
been derivable since 2026-09-02.

**What is missing is a way to READ it without loading Chroma.** `index-query.py`
gets its metadata from the Chroma collection, so today a keyword-only lookup -
which needs no embedding model at all - still pays for a 35 GB store and the
chromadb package. That coupling is wrong in three separate ways:

  1. **Power.** Constraint 3.1 is priority one. Answering a lookup from BM25 with
     no model and no vector store is the cheapest useful thing the node can do,
     and it is currently impossible.
  2. **Graceful degradation (§3.4).** If Chroma is corrupt, absent, or being
     rebuilt, the archive should still be searchable and still be citable. Today
     it is neither.
  3. **Dependencies.** The keyword path needs `sqlite3`, which ships with Python.
     Adding this file means an interface can be built with NOTHING vendored,
     which is the strongest possible answer to §5.5.4.

So: a small store, built once, holding what a citation needs and nothing else.

WHAT IT HOLDS, AND THE ONE IDEA WORTH NOTING. Alongside the identifiers it stores
the BYTE OFFSET of each chunk's JSON line inside `10-index/chunks/*.jsonl`. That
means a passage is one seek and one readline away, so the full text of any hit is
retrievable without Chroma and without holding 3.4 GB of JSONL in memory. The
text is not copied - it stays in exactly one place, and this is a pointer to it.

RESUMABLE BY ARTIFACT, deliberately. 29 files and 3.4 GB is longer than one
session should assume it has. Each artifact is committed as a unit and recorded
as done; re-running continues rather than restarting. The same reason
`backup-cold.sh` works the way it does.
"""

import argparse
import io
import json
import os
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(ROOT, "10-index")
CHUNKS = os.path.join(OUT, "chunks")
DB = os.path.join(OUT, "provenance.sqlite3")
SCHEMA = 1

DDL = """
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);

CREATE TABLE IF NOT EXISTS artifact (
  src        INTEGER PRIMARY KEY,
  file       TEXT NOT NULL,      -- the .zim filename, or the folder name for PDFs
  shelf      TEXT NOT NULL,      -- archive-relative; for PDFs it INCLUDES the folder
  lang       TEXT,
  sha256     TEXT,
  kiwix_book TEXT,               -- NULL for the four PDF-folder artifacts
  jsonl      TEXT NOT NULL,      -- filename under 10-index/chunks/
  docs       INTEGER,
  chunks     INTEGER,
  done       INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS doc (
  src   INTEGER NOT NULL,
  dnum  INTEGER NOT NULL,        -- the middle field of a chunk id, "10:1:0" -> 1
  path  TEXT NOT NULL,           -- ZIM entry path, or the PDF filename
  title TEXT,
  kind  TEXT,
  PRIMARY KEY (src, dnum)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS chunk (
  cid  TEXT PRIMARY KEY,
  src  INTEGER NOT NULL,
  dnum INTEGER NOT NULL,
  i    INTEGER NOT NULL,         -- chunk index within the document
  n    INTEGER NOT NULL,         -- how many chunks that document has
  page INTEGER,                  -- PDFs only
  off  INTEGER NOT NULL,         -- byte offset of the JSON line in its jsonl
  len  INTEGER NOT NULL          -- byte length of that line, newline excluded
) WITHOUT ROWID;
"""

# The chunk table is deliberately NOT indexed on (src, dnum). Every query this
# store answers arrives with a cid, which is the primary key. An index nobody
# reads is 90 MB of disk and a slower build, and it can be added in one statement
# the day something needs it.


def connect():
    con = sqlite3.connect(DB)
    con.execute("PRAGMA journal_mode=OFF")
    con.execute("PRAGMA synchronous=OFF")
    con.executescript(DDL)
    return con


def load_registry():
    p = os.path.join(OUT, "sources.json")
    if not os.path.exists(p):
        sys.exit("no %s - build the index first" % p)
    return json.load(io.open(p, encoding="utf-8"))


def jsonl_for(src, a):
    """The chunk file an artifact wrote. ZIMs keep the .zim in the name; the four
    PDF folders do not. Matched against what is on disk rather than reconstructed,
    because a rule that is right 25 times out of 29 is a rule that fails silently
    on the other four."""
    for cand in (a["file"] + ".jsonl", os.path.basename(a["file"]) + ".jsonl"):
        if os.path.exists(os.path.join(CHUNKS, cand)):
            return cand
    for name in sorted(os.listdir(CHUNKS)):
        if name.endswith(".jsonl") and name[:-6] in (a["file"], os.path.basename(a["file"])):
            return name
    return None


def ingest(con, src, a, jsonl):
    """One artifact, one transaction. Returns (docs, chunks, seconds)."""
    path = os.path.join(CHUNKS, jsonl)
    t0 = time.time()
    docs, rows = {}, []
    nchunks = 0
    off = 0
    with open(path, "rb") as fh:
        for raw in fh:
            ln = len(raw)
            line = raw.rstrip(b"\r\n")
            if line:
                r = json.loads(line)
                cid = r["id"]
                parts = cid.split(":")
                if len(parts) != 3:
                    sys.exit("unexpected chunk id %r in %s" % (cid, jsonl))
                csrc, dnum, i = int(parts[0]), int(parts[1]), int(parts[2])
                if csrc != src:
                    sys.exit("chunk %s claims src %d, registry says %d" % (cid, csrc, src))
                if dnum not in docs:
                    docs[dnum] = (src, dnum, r.get("path") or "", r.get("title"),
                                  r.get("kind"))
                rows.append((cid, src, dnum, i, int(r.get("n", 1)),
                             r.get("page"), off, len(line)))
                nchunks += 1
                if len(rows) >= 20000:
                    con.executemany("INSERT OR REPLACE INTO chunk VALUES (?,?,?,?,?,?,?,?)", rows)
                    rows = []
            off += ln
    if rows:
        con.executemany("INSERT OR REPLACE INTO chunk VALUES (?,?,?,?,?,?,?,?)", rows)
    con.executemany("INSERT OR REPLACE INTO doc VALUES (?,?,?,?,?)", list(docs.values()))
    con.execute("UPDATE artifact SET done=1, docs=?, chunks=? WHERE src=?",
                (len(docs), nchunks, src))
    con.commit()
    return len(docs), nchunks, time.time() - t0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--rebuild", action="store_true")
    ap.add_argument("--max-seconds", type=float, default=0,
                    help="stop cleanly after this long; resume by re-running")
    ap.add_argument("--only", help="one artifact src id, for debugging")
    args = ap.parse_args()

    if args.rebuild and os.path.exists(DB):
        os.remove(DB)

    reg = load_registry()
    con = connect()
    con.execute("INSERT OR REPLACE INTO meta VALUES ('schema', ?)", (str(SCHEMA),))
    con.execute("INSERT OR REPLACE INTO meta VALUES ('index_built', ?)",
                (reg.get("built", ""),))

    for src_s, a in sorted(reg["artifacts"].items(), key=lambda kv: int(kv[0])):
        src = int(src_s)
        jl = jsonl_for(src, a)
        if jl is None:
            sys.exit("no chunk file on disk for artifact %d (%s)" % (src, a["file"]))
        con.execute(
            "INSERT INTO artifact (src,file,shelf,lang,sha256,kiwix_book,jsonl,docs,chunks,done) "
            "VALUES (?,?,?,?,?,?,?,?,?,0) ON CONFLICT(src) DO UPDATE SET "
            "file=excluded.file, shelf=excluded.shelf, lang=excluded.lang, "
            "sha256=excluded.sha256, kiwix_book=excluded.kiwix_book, jsonl=excluded.jsonl",
            (src, a["file"], a["shelf"], a.get("lang"), a.get("sha256"),
             a.get("kiwix_book"), jl, a.get("docs"), a.get("chunks")))
    # A REWRITTEN .jsonl INVALIDATES EVERY OFFSET THIS STORE HOLDS FOR IT.
    # The whole design is that a chunk is one seek into chunks/*.jsonl. Rebuild an
    # artifact and those byte offsets point at different text - so its citations
    # resolve to the wrong passage, or to nothing.
    #
    # Found 2026-09-05, the hard way. `index-build.py --artifact hesperian` rewrote
    # that file after the table-aware extraction landed; this script then printed
    # "nothing to do - all 29 artifacts ingested" and exited, because `done=1` was
    # never cleared. It reported success while leaving 8,833 stale offsets in
    # place. A resume flag that keys on "was this ever ingested" rather than "has
    # its input changed" is a check that has quietly stopped running.
    #
    # Size and mtime are recorded in `meta` rather than as new columns, so this
    # needs no schema change and no ALTER TABLE.
    stale = []
    for src, jl in con.execute("SELECT src, jsonl FROM artifact").fetchall():
        p = os.path.join(CHUNKS, jl)
        if not os.path.exists(p):
            continue
        st = os.stat(p)
        sig = "%d:%d" % (st.st_size, int(st.st_mtime))
        k = "jsonl:%d" % src
        row = con.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        if row and row[0] != sig:
            stale.append((src, jl))
            con.execute("DELETE FROM chunk WHERE src=?", (src,))
            con.execute("DELETE FROM doc WHERE src=?", (src,))
            con.execute("UPDATE artifact SET done=0 WHERE src=?", (src,))
        con.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (k, sig))
    if stale:
        print("  %d artifact(s) whose chunks file changed since ingest - re-reading:"
              % len(stale))
        for src, jl in stale:
            print("     src %-3d %s" % (src, jl))
    con.commit()

    todo = con.execute("SELECT src, file, jsonl FROM artifact WHERE done=0 ORDER BY src").fetchall()
    done = con.execute("SELECT COUNT(*) FROM artifact WHERE done=1").fetchone()[0]
    total = con.execute("SELECT COUNT(*) FROM artifact").fetchone()[0]

    if args.status:
        nc = con.execute("SELECT COUNT(*) FROM chunk").fetchone()[0]
        nd = con.execute("SELECT COUNT(*) FROM doc").fetchone()[0]
        size = os.path.getsize(DB) if os.path.exists(DB) else 0
        print("provenance.sqlite3 - %d/%d artifacts, %s docs, %s chunks, %.0f MB"
              % (done, total, "{:,}".format(nd), "{:,}".format(nc), size / 1e6))
        for src, f, jl in todo:
            print("   pending %-3d %s" % (src, f))
        return

    if not todo:
        print("nothing to do - all %d artifacts ingested" % total)
        return

    started = time.time()
    for src, f, jl in todo:
        if args.only and str(src) != args.only:
            continue
        if args.max_seconds and (time.time() - started) > args.max_seconds:
            print("   stopping cleanly at the time budget - re-run to continue")
            break
        nd, nc, secs = ingest(con, src, jl and reg["artifacts"][str(src)] or None, jl)
        print("   %-3d %-46s %7s docs %9s chunks  %5.1fs"
              % (src, f[:46], "{:,}".format(nd), "{:,}".format(nc), secs))

    left = con.execute("SELECT COUNT(*) FROM artifact WHERE done=0").fetchone()[0]
    nc = con.execute("SELECT COUNT(*) FROM chunk").fetchone()[0]
    print("\n%s chunks stored, %d artifact(s) still pending" % ("{:,}".format(nc), left))
    if left == 0:
        con.execute("INSERT OR REPLACE INTO meta VALUES ('completed', ?)",
                    (time.strftime("%Y-%m-%dT%H:%M:%S"),))
        con.commit()
        con.execute("VACUUM")
        print("provenance.sqlite3 complete - %.0f MB" % (os.path.getsize(DB) / 1e6))
    con.close()


if __name__ == "__main__":
    main()
