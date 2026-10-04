#!/usr/bin/env python3
"""
kiwix-probe-shelves.py - can ONE kiwix-serve serve ZIMs from more than one shelf?

    python bin/kiwix-probe-shelves.py                 # probe, report, clean up
    python bin/kiwix-probe-shelves.py --shelf 04-corpora-mathematics
    python bin/kiwix-probe-shelves.py --port 8099 --keep

WHY THIS EXISTS. `library.xml` holds **25 books, all from
07-corpora-supplemental**, because `bin/kiwix-library.py` puts the library beside
the ZIMs and every indexed ZIM happens to live in one directory. The consequence
was not noticed until 2026-09-12: **shelves 02 to 06 are not merely unindexed,
nothing in the running node can reach them at all** - not by search, not by link,
not by browsing. That is 270.7 GB on `02-corpora-core` alone, including
`wikipedia_en_all_maxi` (115.5 GB) and `stackoverflow.com_en_all` (107 GB).

`kiwix-library.py` explains why the library sits where it does, and the reason is
a measurement from 2026-09-04:

    library at <archive>/library.xml
        path  07-corpora-supplemental/mdwiki_en_all_maxi_2025-11.zim
        key   07-corpora-supplemental/mdwiki_en_all_maxi_2025-11
        -> the book resolves, and every ARTICLE under it 404s

**That measurement was taken while a SEPARATE defect was also present** - Windows
backslashes in the stored path, which produce the same symptom by a different
route - and the multi-shelf case was never re-tested after the separator was
fixed. So the rule this archive is currently living under may be true, or may be
a conclusion drawn from the wrong cause. One is a design constraint costing five
extra server processes; the other is a fixed bug.

This script asks the running server instead of asking the note. It is the same
discipline `kiwix-library.py` records for the book-name correction: **an input
cannot tell you what the server will do with it, so fetch the URL.**

WHAT IT TESTS. Three library shapes, each against a real kiwix-serve on a spare
port, using the two SMALLEST ZIMs it can find so nothing takes long:

    A  baseline    the shipped library, beside the ZIMs, one directory
                   -> must pass, or the harness is wrong rather than the config
    B  root        library at the archive root, relative multi-segment paths,
                   forward slashes - the 09-04 configuration WITHOUT the
                   separator defect that was present when it was judged
    C  absolute    library at the archive root, absolute native paths written
                   into the XML directly. kiwix-manage cannot store these - it
                   relativises everything - but the FILE is just XML and
                   kiwix-serve is the thing whose opinion matters

For each it reports three different questions, because they fail separately:

    catalogue      does the book appear, and what key does the server advertise
    book           GET /content/<key>                     -> is the ZIM open
    article        GET the target of /random?content=<key> -> can an entry be
                   addressed beneath that key.  THIS IS THE ONE THAT DECIDES IT.
    search         GET /search?books.name=<name>&pattern=. -> is the fallback
                   this whole exercise is for actually reachable

IT LEAVES NOTHING BEHIND. The probe library is written at the archive ROOT, which
carries no CHECKSUMS.sha256, and is deleted in a finally block; `--keep` skips
that for debugging. It never touches `07-corpora-supplemental/library.xml`, never
writes inside a checksummed shelf, and never stops a server it did not start.

Standard library only.
"""

import argparse
import glob
import io
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.environ.get("ARK_ROOT") or os.path.dirname(HERE)
TOOLS = os.path.join(ROOT, "09-software", "kiwix-tools")
SHIPPED_LIB = os.path.join(ROOT, "07-corpora-supplemental", "library.xml")
PROBE_LIB = os.path.join(ROOT, "library-probe.xml")
WINDOWS = os.name == "nt"


def exe(n):
    return n + ".exe" if WINDOWS else n


def find(name):
    """The vendored copy first. A node rebuilt from cold storage has the tarball
    and no system install - same lookup order as bin/kiwix-library.py."""
    for d in (TOOLS, ROOT):
        for hit in glob.glob(os.path.join(d, "**", exe(name)), recursive=True):
            return hit
    found = shutil.which(name) or shutil.which(exe(name))
    if found:
        return found
    sys.exit("%s not found. It is vendored, not installed: extract the archive "
             "for this platform under %s" % (name, TOOLS))


def smallest_zim(shelf):
    z = [(os.path.getsize(p), p) for p in
         glob.glob(os.path.join(ROOT, shelf, "*.zim"))]
    if not z:
        return None
    return sorted(z)[0][1]


def free_port(p):
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", p))
        return True
    except OSError:
        return False
    finally:
        s.close()


def get(url, timeout=20):
    """Status code and final URL, following redirects. A 404 is an ANSWER here,
    not an error, so it must not raise."""
    try:
        r = urllib.request.urlopen(url, timeout=timeout)
        return r.getcode(), r.geturl(), r.read(400)
    except urllib.error.HTTPError as e:
        return e.code, url, b""
    except Exception as e:                       # noqa: BLE001 - reported
        return 0, "%s: %s" % (type(e).__name__, e), b""


def rewrite_paths(raw, zims, absolute):
    """Normalise separators, and optionally make every path absolute.

    A PURE FUNCTION BECAUSE IT IS THE THING THAT CONFOUNDED THE LAST
    MEASUREMENT. On Windows kiwix-manage stores
    `07-corpora-supplemental\\x.zim`, and kiwix-serve splits the book key on
    "/" only - so the backslash is not a separator to it, the whole string
    becomes the key, and every article 404s. That is a DIFFERENT defect from the
    multi-segment question, it produces the SAME symptom, and it was present in
    2026-09-04's test. If this normalisation is wrong, config B measures the old
    bug again and reports it as a constraint. So it is separated from the
    subprocess and asserted in --selftest."""
    raw = re.sub(r'path="([^"]*)"',
                 lambda m: 'path="%s"' % m.group(1).replace("\\", "/"), raw)
    if absolute:
        byname = {os.path.basename(z): z for z in zims}
        raw = re.sub(
            r'path="([^"]*)"',
            lambda m: 'path="%s"' % byname.get(
                os.path.basename(m.group(1)), m.group(1)), raw)
    return raw


def build_library(km, lib_path, zims, absolute=False):
    """kiwix-manage builds it, then the path attribute is rewritten if needed.

    BUILT BY THE REAL TOOL, THEN ONE FIELD CHANGED. Hand-writing the XML would
    mean inventing book ids and metadata that libkiwix normally reads out of the
    ZIM, and a probe that fails because its fixture is malformed proves nothing
    about the configuration it claims to test. So kiwix-manage produces valid
    entries and only `path` - the field under test, the one doing two jobs - is
    edited afterwards."""
    if os.path.exists(lib_path):
        os.remove(lib_path)
    # THE LIBRARY COMES FIRST, THEN THE SUBCOMMAND. `kiwix-manage <lib> add
    # <zim>`, not `kiwix-manage add <lib> <zim>` - the second returns
    # 4294967295 and says nothing. Copied from bin/kiwix-library.py, which has
    # been running this exact call for a week, rather than written from memory
    # of how other tools order their arguments.
    for z in zims:
        r = subprocess.run([km, lib_path, "add", "--zimPathToSave",
                            os.path.abspath(z), z],
                           capture_output=True, text=True)
        if r.returncode:
            sys.exit("kiwix-manage failed on %s (exit %s):\n%s"
                     % (os.path.basename(z), r.returncode, r.stderr or r.stdout))
    raw = rewrite_paths(io.open(lib_path, encoding="utf-8").read(), zims, absolute)
    io.open(lib_path, "w", encoding="utf-8", newline="\n").write(raw)
    return raw


def books_in(lib_path):
    """(advertised key, book name, zim basename) per entry, read from the file
    the server is about to be handed."""
    out = []
    for b in ET.parse(lib_path).getroot().findall("book"):
        p = (b.get("path") or "").replace("\\", "/")
        key = re.sub(r"\.zim$", "", p)
        # an absolute path's key is whatever the server decides; the file cannot
        # say, so this is a guess for display and the catalogue is the truth
        out.append((key, b.get("name") or "", os.path.basename(p)))
    return out


def serve(ks, lib, port):
    p = subprocess.Popen([ks, "--port", str(port), "--library", lib],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = "http://127.0.0.1:%d" % port
    for _ in range(60):
        if p.poll() is not None:
            return p, base, False
        code, _u, _b = get(base + "/", timeout=2)
        if code:
            return p, base, True
        time.sleep(0.5)
    return p, base, False


def catalogue_entries(base):
    """Parse the OPDS feed properly and return what the server ACTUALLY calls
    each book, plus every link it advertises.

    THE THIRD ATTEMPT AT THIS, AND THE FIRST THAT READS THE FEED. Version one
    regexed for `href="/content/..."`, found nothing, and reported the shipped
    library as unusable. Version two added two more guessed patterns and still
    found nothing, which left the config B and C 404s ambiguous between
    *multi-segment keys do not work* and *I guessed the key wrong twice* - two
    conclusions with opposite consequences. Guessing a pattern a third time
    would be the same mistake in new clothes, so this parses the XML, ignores
    namespaces, and reports whatever is actually there.

    Returns (endpoint, [(title, name, [hrefs])], raw). An empty list with a 200
    means the feed parsed and held no entries, which is itself an answer."""
    for path in ("/catalog/v2/entries?count=-1", "/catalog/root.xml",
                 "/catalog/v2/entries"):
        code, _u, body = get(base + path, timeout=25)
        if code != 200 or not body:
            continue
        try:
            root = ET.fromstring(body)
        except ET.ParseError:
            continue
        tag = lambda e: e.tag.rsplit("}", 1)[-1]        # noqa: E731 - namespaces
        out = []
        for e in root.iter():
            if tag(e) != "entry":
                continue
            title = name = ""
            hrefs = []
            for c in e.iter():
                t = tag(c)
                if t == "title" and not title:
                    title = (c.text or "").strip()
                elif t == "name" and not name:
                    name = (c.text or "").strip()
                elif t == "link" and c.get("href"):
                    hrefs.append(c.get("href"))
            out.append((title, name, hrefs))
        if out:
            return path, out, body
    return "(no endpoint answered with entries)", [], body if 'body' in dir() else b""


def keys_from_catalogue(entries):
    """Every string the feed suggests could be a book key, in link order."""
    keys = []
    for _t, name, hrefs in entries:
        for h in hrefs:
            for marker in ("/content/", "/viewer#", "/raw/"):
                if marker in h:
                    k = h.split(marker, 1)[1].split("?")[0].rstrip("/")
                    if k and k not in keys:
                        keys.append(k)
        if name and name not in keys:
            keys.append(name)
    return keys


def key_candidates(path_key, name, zim):
    """Every string this book might be addressable as, most-literal first.

    THE PROBE THAT WAS MISSING, AND IT IS THE WHOLE QUESTION. The first four
    versions asked for exactly one key - the stored path minus `.zim` - and
    when that 404'd on a multi-segment path they concluded that multi-segment
    keys do not work. **That conclusion never tested the basename.** libkiwix
    may derive a book's id from the FILENAME rather than from the stored path,
    in which case a root-level library produces clean single-segment keys and
    the constraint this archive has been living under since 2026-09-04 does not
    exist. A 404 on one guessed spelling is not evidence about the others.

    Config A cannot distinguish these: beside the ZIMs the stored path IS the
    basename, so every candidate collapses to the same string and the test
    passes for reasons it does not reveal. Only a multi-directory library
    separates them, which is exactly the case under test."""
    out = [("stored path", path_key)]
    stem = re.sub(r"\.zim$", "", os.path.basename(zim))
    for label, k in (("basename", stem), ("library name", name or "")):
        if k and k not in [c[1] for c in out]:
            out.append((label, k))
    return out


def probe(name, note, ks, lib, port, limit):
    print("\n" + "=" * 74)
    print("%-12s %s" % (name, note))
    print("=" * 74)

    # THE KEYS COME FROM THE LIBRARY FILE. Deterministic, and it is the thing
    # whose shape is under test. Capped, because the shipped library holds 25
    # books and the question does not need 25 answers - but the cap always
    # keeps one book per DIRECTORY, since a config that works for one shelf and
    # not the other is exactly the outcome being looked for.
    entries = books_in(lib)
    bydir, picked = {}, []
    for key, nm, zim in entries:
        d = key.rsplit("/", 1)[0] if "/" in key else "(beside the library)"
        bydir.setdefault(d, []).append((key, nm, zim))
    for d in sorted(bydir):
        picked.extend(bydir[d][:max(1, limit // max(1, len(bydir)))])
    print("   library      %d book(s) across %d director%s; probing %d"
          % (len(entries), len(bydir), "y" if len(bydir) == 1 else "ies",
             len(picked)))
    for d in sorted(bydir):
        print("                  %-40s %d book(s)" % (d[:40], len(bydir[d])))

    proc, base, up = serve(ks, lib, port)
    try:
        if not up:
            print("   !! kiwix-serve did not come up on %d with this library" % port)
            return {"name": name, "up": False}

        src, entries, raw = catalogue_entries(base)
        adv = keys_from_catalogue(entries)
        print("   catalogue    %s -> %d entr%s" % (src, len(entries),
                                                   "y" if len(entries) == 1 else "ies"))
        for t, nm, hrefs in entries[:4]:
            print("                  title=%-26s name=%-22s" % (t[:26], nm[:22]))
            for h in hrefs[:4]:
                print("                     link %s" % h[:76])
        if not entries:
            print("                  feed parsed to zero entries; raw head: %r"
                  % raw[:180])

        rows = []
        for key, nm, zim in picked:
            # WALK THE LADDER UNTIL SOMETHING ANSWERS 200, and name the spelling
            # that worked. Reporting "404" for a book that is perfectly
            # reachable under another key is how the last four runs of this
            # script produced a confident wrong answer.
            won, bc = None, None
            for label, cand in key_candidates(key, nm, zim):
                code, _u, _b = get("%s/content/%s" % (base, cand))
                print("      try %-13s %-46s book %s" % (label, cand[-46:], code))
                if code == 200:
                    won, bc = (label, cand), code
                    break
                bc = code if bc is None else bc
            if not won:
                rows.append((key, bc, bc, 0, 0))
                continue
            label, k = won
            rc, ru, _rb = get("%s/random?content=%s" % (base, k))
            ac = rc if rc != 200 else get(ru)[0]
            # TWO SEARCH SPELLINGS, because 400 came back on config A too -
            # where books demonstrably resolve - so the 400 was this script's
            # URL, not the server's capability.
            sc, hits = 0, 0
            for form in ("books.name=%s" % urllib.request.quote(nm or k),
                         "content=%s" % urllib.request.quote(k),
                         "books.filter.title=%s" % urllib.request.quote(nm or k)):
                c2, _u2, sb = get("%s/search?%s&pattern=the" % (base, form))
                if c2 == 200:
                    sc = 200
                    hits = len(re.findall(rb'<a\s+href=', sb or b""))
                    break
                sc = c2 if not sc else sc
            rows.append((k, 200, ac, sc, hits))
            print("   %-30s via %-13s article %-4s search %-4s (%d hits)"
                  % (k[-30:], label, ac, sc, hits))

        # IF OUR KEY 404s, TRY THE SERVER'S OWN. For a single-directory library
        # the stored path and the URL key are the same string, which is why
        # config A passes with a guessed key - and that is exactly what makes a
        # guessed key untrustworthy for B and C, where the two may differ. A
        # 404 on a key nobody advertised proves nothing at all.
        if rows and all(bb != 200 for _k, bb, _a, _s, _h in rows) and adv:
            print("   retry        our keys 404; trying the %d key(s) the "
                  "catalogue advertises" % len(adv))
            rows = []
            for k in adv[:max(2, limit)]:
                bc, _bu, _bb = get("%s/content/%s" % (base, k))
                rc, ru, _rb = get("%s/random?content=%s" % (base, k))
                ac = rc if rc != 200 else get(ru)[0]
                rows.append((k, bc, ac, 0, 0))
                print("   %-38s book %-4s article %-4s  (catalogue key)"
                      % (k[-38:], bc, ac))

        ok = bool(rows) and all(b == 200 and a == 200 for _k, b, a, _s, _h in rows)
        searchable = bool(rows) and all(sx == 200 for _k, _b, _a, sx, _h in rows)
        print("   ---> %s%s" % (
            "BOOKS AND ARTICLES RESOLVE" if ok else "NOT USABLE as shipped",
            ", and search works" if ok and searchable else ""))
        return {"name": name, "up": True, "ok": ok, "search": searchable,
                "rows": rows, "dirs": len(bydir)}
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:                        # noqa: BLE001
            proc.kill()


def selftest():
    """The path rewriting only. NO BINARY, NO ZIM, NO PORT.

    What it cannot prove is which configuration kiwix-serve accepts - that is
    the entire point of the script and it needs a running server. What it CAN
    prove is that config B is a clean test of the multi-segment question rather
    than an accidental re-run of the 2026-09-04 separator bug, which is the one
    way this probe could confidently report the wrong answer."""
    ok = fail = 0

    def check(name, got, want, note=""):
        nonlocal ok, fail
        if got == want:
            ok += 1
            print("  ok    %-30s %s" % (name, note))
        else:
            fail += 1
            print("  FAIL  %-30s got %r want %r  %s" % (name, got, want, note))

    win = ('<book id="1" path="07-corpora-supplemental\\zimgit-knots_en.zim"/>'
           '<book id="2" path="02-corpora-core\\iot.stackexchange.zim"/>')
    zims = [os.path.join("C:", "ark", "07-corpora-supplemental",
                         "zimgit-knots_en.zim"),
            os.path.join("C:", "ark", "02-corpora-core",
                         "iot.stackexchange.zim")]

    rel = rewrite_paths(win, zims, absolute=False)
    check("no backslash survives", "\\" in rel, False,
          "the 09-04 confounder, removed before the test it would corrupt")
    check("two paths rewritten", rel.count('path="'), 2)
    check("multi-segment preserved",
          'path="07-corpora-supplemental/zimgit-knots_en.zim"' in rel, True,
          "config B must still be multi-segment or it tests nothing")
    check("second shelf preserved",
          'path="02-corpora-core/iot.stackexchange.zim"' in rel, True)

    ab = rewrite_paths(win, zims, absolute=True)
    for z in zims:
        check("absolute path present", ('path="%s"' % z) in ab, True,
              os.path.basename(z))
    # THE ATTRIBUTE VALUE, NOT A SUBSTRING OF IT. First version asserted that
    # "07-corpora-supplemental/zimgit" was absent, which is false for a correct
    # absolute path - C:/ark/07-corpora-supplemental/zimgit... contains
    # it. The check was wrong, the code was right, and only running it showed
    # which. Same shape as the three assertions that outlived their contracts
    # on 09-12.
    check("no bare relative path left",
          'path="07-corpora-supplemental/zimgit-knots_en.zim"' in ab, False,
          "the attribute must not still START with the shelf")

    # A ZIM THE CALLER DID NOT NAME MUST NOT BE SILENTLY DROPPED OR MANGLED.
    # The absolute rewrite maps by basename; an entry with no match keeps what
    # it had rather than vanishing, so a library with an extra book is still a
    # valid library and the probe reports on it.
    extra = rewrite_paths(win + '<book id="3" path="x/unknown.zim"/>',
                          zims, absolute=True)
    check("unknown entry kept", 'path="x/unknown.zim"' in extra, True,
          "an unmatched book survives rather than disappearing")
    check("three entries still", extra.count('path="'), 3)

    print("\n%d/%d" % (ok, ok + fail))
    print("\nWHAT THIS DOES NOT PROVE: which configuration kiwix-serve accepts.")
    print("Only a running server answers that, which is what the script is for.")
    return fail == 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--shelf", default="02-corpora-core",
                    help="the second shelf to prove reachable (default 02)")
    ap.add_argument("--port", type=int, default=8099)
    ap.add_argument("--books", type=int, default=4,
                    help="how many books to probe per config, at least one per "
                         "directory (default 4)")
    ap.add_argument("--keep", action="store_true",
                    help="leave library-probe.xml behind for inspection")
    ap.add_argument("--selftest", action="store_true",
                    help="the path rewriting, against synthetic XML; needs no "
                         "binary, no ZIM and no port")
    a = ap.parse_args()

    if a.selftest:
        return 0 if selftest() else 1

    ks, km = find("kiwix-serve"), find("kiwix-manage")
    print("kiwix-serve   %s" % ks)
    print("kiwix-manage  %s" % km)
    if not free_port(a.port):
        sys.exit("port %d is busy - the node's own servers are on 8080/8081/8090, "
                 "so pick another with --port" % a.port)

    here = smallest_zim("07-corpora-supplemental")
    there = smallest_zim(a.shelf)
    if not here or not there:
        sys.exit("need a .zim in both 07-corpora-supplemental and %s" % a.shelf)
    print("probe ZIMs    %s  (%.0f MB)" % (os.path.basename(here),
                                           os.path.getsize(here) / 1e6))
    print("              %s  (%.0f MB)" % (os.path.basename(there),
                                           os.path.getsize(there) / 1e6))
    print("port          %d" % a.port)

    results = []
    try:
        if os.path.exists(SHIPPED_LIB):
            results.append(probe(
                "A baseline", "the shipped library, beside the ZIMs, one directory",
                ks, SHIPPED_LIB, a.port, a.books))
        else:
            print("\nA baseline   skipped: no %s" % SHIPPED_LIB)

        build_library(km, PROBE_LIB, [here, there], absolute=False)
        results.append(probe(
            "B root", "library at the archive root, relative multi-segment paths",
            ks, PROBE_LIB, a.port, a.books))

        build_library(km, PROBE_LIB, [here, there], absolute=True)
        results.append(probe(
            "C absolute", "library at the archive root, absolute paths in the XML",
            ks, PROBE_LIB, a.port, a.books))
    finally:
        if not a.keep and os.path.exists(PROBE_LIB):
            try:
                os.remove(PROBE_LIB)
                print("\nremoved %s" % PROBE_LIB)
            except OSError as e:
                print("\n!! could not remove %s (%s) - delete it by hand; it is "
                      "at the archive root, which carries no CHECKSUMS.sha256, "
                      "so nothing is out of date because of it" % (PROBE_LIB, e))

    print("\n" + "=" * 74)
    print("VERDICT")
    print("=" * 74)
    for r in results:
        # REPORT THE COLUMNS, NOT A SENTENCE WRITTEN IN ADVANCE. The first
        # version printed "book resolves, articles do not" for a run whose book
        # column read 404 - a verdict describing the failure that was EXPECTED
        # rather than the one that happened, which is how a measurement gets
        # quietly replaced by the note it was meant to test.
        if not r.get("up"):
            state = "server did not start"
        elif r.get("ok"):
            state = "WORKS"
        else:
            bk = sorted({x for _k, x, _a, _s, _h in r.get("rows", [])})
            ar = sorted({x for _k, _b, x, _s, _h in r.get("rows", [])})
            state = "book %s, article %s" % (
                "/".join(str(x) for x in bk) or "?",
                "/".join(str(x) for x in ar) or "?")
        print("  %-12s %s" % (r["name"], state))
    workable = [r["name"] for r in results
                if r.get("ok") and r["name"] != "A baseline"]
    control = next((r for r in results if r["name"] == "A baseline"), None)
    print()
    if control and not control.get("ok"):
        print("THE CONTROL FAILED. The shipped library is the one the node serves")
        print("from every day, so a failure here is this script's, not the")
        print("archive's. Conclude nothing about B or C.")
    elif workable:
        print("ONE SERVER CAN SERVE EVERY SHELF, using config %s." % workable[0])
        print("The 2026-09-04 note is superseded. Change default_library() in")
        print("bin/kiwix-library.py to build at the archive root in that shape,")
        print("widen the enumeration past the 29 artifacts in sources.json, and")
        print("re-run bin/kiwix-library.py --verify-against http://localhost:8080")
        print("Note WHICH key spelling answered, above - that is what")
        print("store.citation() has to put in a URL.")
    elif any(r.get("up") for r in results):
        print("EVERY CANDIDATE SPELLING 404s FOR A MULTI-DIRECTORY LIBRARY.")
        print("Not just the stored path - the basename and the library name too,")
        print("which is what the first four runs of this script never asked.")
        print("So the 2026-09-04 note holds and it is a real constraint: one")
        print("library cannot span shelves on this binary. The route to shelf 02")
        print("is a SECOND kiwix-serve with its own library beside its own ZIMs,")
        print("one port per shelf, which works by construction because it is")
        print("what 07 already does.")
    else:
        print("Nothing came up. That is a harness or binary problem, not an")
        print("answer about configurations - do not conclude anything from it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
