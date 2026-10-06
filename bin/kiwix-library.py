#!/usr/bin/env python3
"""
kiwix-library.py - build the Kiwix library, and record what it calls each book.

    python bin/kiwix-library.py                    # build library.xml + record names
    python bin/kiwix-library.py --check            # compare only, change nothing
    python bin/kiwix-library.py --from-library X   # use an existing library.xml

WHY THIS EXISTS, AND A CORRECTION IT CAUSED. Read the correction; it is the more
useful half.

The tool's job is to build `library.xml`, which is what `kiwix-serve` is started
with, and to CONFIRM - against the running server - that the book key the node
puts in a citation is the key that server answers to.

THE CORRECTION, 2026-09-04. On the way to writing this, `sources.json`'s
`kiwix_book` values were declared wrong. They are the ZIM filename stem, e.g.
`zimgit-water_en_2024-08`. `kiwix-manage` was asked what it called that book and
answered `zimgit-water_en` - no date - and all 25 disagreed the same way, so the
conclusion looked overwhelming: every ZIM citation must be 404ing.

**It was the wrong conclusion, reached by asking the wrong program.** A live
`kiwix-serve` was then started and the URLs fetched:

    /content/appropedia_en_all              404   <- the "corrected" name
    /content/appropedia_en_all_maxi_2026-02 200   <- the original filename stem

`kiwix-serve` builds a book's URL from its FILE PATH, not from the `name` the
library records out of ZIM metadata. Its own catalogue says so plainly, and this
is the line that settles it:

    <link type="text/html" href="/content/mdwiki_en_all_maxi_2025-11"/>

while the same entry's `<name>` is `mdwiki_en_all`. Two different fields for two
different jobs, and only one of them appears in a URL.

So `sources.json` was right all along, `kiwix-manage` answered a question nobody
had asked, and the check that settled it was **fetching the URL from the server
that serves it**. That is what `--verify-against` does, and it is the only check
here worth trusting: `library.xml` is an input to the server, and an input cannot
tell you what the server will do with it.
"""

import argparse
import glob
import io
import json
import os
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
INDEX = os.path.join(ROOT, "10-index")
BOOKS = os.path.join(INDEX, "kiwix-books.json")
def default_library():
    r"""The archive ROOT, because the paths inside are absolute.

    SUPERSEDES THE 2026-09-04 RULE, MEASURED 2026-09-12 by
    `bin/kiwix-probe-shelves.py` against a running kiwix-serve 3.8.1:

        stored path                                 book  article
        ------------------------------------------  ----  -------
        beside the ZIMs, bare filename               200      200
        root library, RELATIVE  02-corpora-core/x     404      404   <- and the
          (also tried: basename, library name)        404            bare
                                                                     basename
                                                                     404s too
        root library, ABSOLUTE  C:\ark\02-...        404      -     as stored
          addressed by BASENAME                       200      200   <- works

    So a RELATIVE multi-segment path is genuinely unaddressable, which is what
    09-04 found. **But an absolute path is fine**: kiwix-serve opens the file
    and derives the key from the BASENAME. The old rule generalised from the
    relative case to all cases and cost this archive its reach - `library.xml`
    held 25 books, all in one directory, while 42 artifacts and 686 GB on
    shelves 02 to 06 could not be reached by the running node at all.

    WHY IT WAS MISSED, AND IT IS IN THIS FILE'S OWN COMMENTS. Absolute paths
    WERE tried in 09-04, through `kiwix-manage --zimPathToSave`, which
    relativises everything it is handed - so the note recorded *"it does not
    recognise C:/... as absolute either"*. That is true of **kiwix-manage**, and
    says nothing about kiwix-serve. The tool that writes the file cannot express
    the thing the server accepts, so the file is post-processed instead. Same
    move, and same justification, as the separator normalisation below.

    THE COST OF ABSOLUTE PATHS IS THAT THIS FILE IS MACHINE-SPECIFIC. It already
    was - it is excluded from every CHECKSUMS.sha256 for exactly that reason -
    but it is now sharply so: a restored archive on a different drive letter or
    a different mount point must REBUILD THE LIBRARY BEFORE SERVING, and
    RECOVERY.md says so."""
    return os.path.join(ROOT, "library.xml")


# ---------------------------------------------------------------------------
# THE SUPERSEDED RULE. It governed this file from 2026-09-04 to 2026-09-12, and
# it is why the running node served 25 books out of 67 and could not reach
# 686 GB on shelves 02 to 06 by any route. Kept rather than deleted, because the
# observation was sound and only the generalisation from it was wrong.
#
# What 09-04 recorded:
#
#     library at <archive>/library.xml
#         path  07-corpora-supplemental/mdwiki_en_all_maxi_2025-11.zim
#         key   07-corpora-supplemental/mdwiki_en_all_maxi_2025-11
#         -> the book resolves, and every ARTICLE under it 404s
#
#     library beside the ZIMs
#         path  mdwiki_en_all_maxi_2025-11.zim
#         key   mdwiki_en_all_maxi_2025-11
#         -> works
#
# and concluded: if every ZIM shares one directory, the library goes there.
#
# TWO CORRECTIONS FROM 2026-09-12, both from bin/kiwix-probe-shelves.py against
# a running server rather than from reasoning:
#
#   1. For a relative multi-segment path the BOOK 404s as well, not just the
#      article - and so do the bare basename and the library `name`. All three
#      spellings were tried. The recorded symptom was milder than the reality.
#   2. The conclusion does not hold for ABSOLUTE paths, which were never really
#      tested. They were attempted through `kiwix-manage --zimPathToSave`, which
#      relativises everything it is handed, so what 09-04 actually measured was
#      kiwix-manage's input handling - the note even says so: "it does not
#      recognise C:/... as absolute either". kiwix-serve, handed an absolute
#      path in the file, opens the ZIM and answers to the BASENAME.
#
# THE LESSON IS THE ONE THIS FILE ALREADY TEACHES, APPLIED ONE LEVEL UP. Its own
# header says an input cannot tell you what the server will do with it, so fetch
# the URL. 09-04 did that for book NAMES and got the right answer. It did not do
# it for the absolute-path case, because the tool it used could not produce one -
# and a configuration your tooling cannot express looks exactly like a
# configuration the server rejects.
# ---------------------------------------------------------------------------


TOOLS = os.path.join(ROOT, "09-software", "kiwix-tools")


def find_kiwix_manage(explicit):
    """Look where the archive actually keeps it before looking on PATH. A node
    rebuilt from cold storage has the tarball and no system install."""
    if explicit:
        if not os.path.exists(explicit):
            sys.exit("no kiwix-manage at %s" % explicit)
        return explicit
    for pat in ("kiwix-manage", "kiwix-manage.exe"):
        for d in (TOOLS, ROOT):
            for hit in glob.glob(os.path.join(d, "**", pat), recursive=True):
                return hit
    found = shutil.which("kiwix-manage") or shutil.which("kiwix-manage.exe")
    if found:
        return found
    sys.exit(
        "kiwix-manage not found.\n"
        "  It is vendored, not installed. Extract the archive for this platform:\n"
        "    %s\n"
        "  then re-run, or pass --kiwix-manage <path>." % TOOLS)


def zim_artifacts():
    p = os.path.join(INDEX, "sources.json")
    if not os.path.exists(p):
        sys.exit("no %s" % p)
    reg = json.load(io.open(p, encoding="utf-8"))
    out = []
    for k, v in sorted(reg["artifacts"].items(), key=lambda kv: int(kv[0])):
        if v.get("kiwix_book"):
            out.append((int(k), v["kiwix_book"],
                        os.path.abspath(os.path.join(ROOT, v["shelf"], v["file"]))))
    return out


# 08-maps SINCE 2026-10-06. It holds two ZIMs (the GIS Stack Exchange and the
# OpenStreetMap wiki) that were on the drive and never served: the corpus view
# (corpus.py) drew them red on its first run against this archive, which is the
# 686 GB mistake above in a smaller place. Only .zim files are read, so the
# shelf's map tiles are untouched.
CORPUS_SHELVES = ("02-corpora-core", "03-corpora-economics",
                  "04-corpora-mathematics", "05-corpora-language",
                  "06-corpora-literature", "07-corpora-supplemental",
                  "08-maps")


def all_zims():
    """Every .zim on every corpus shelf, as (None, stem, path).

    THE LIBRARY IS BUILT FROM THE DISK; kiwix-books.json is still recorded from
    `sources.json`. Those are two different questions and conflating them is
    what hid 686 GB: `zim_artifacts()` reads the INDEX registry, 29 artifacts,
    and the library was built from it - so an artifact the retrieval index had
    not ingested was also unreachable by browsing, by link and by Kiwix's own
    search. Being unindexed and being unserved are not the same fault and should
    not have shared a cause.

    A book with no `src` is served and simply never cited, which is correct: the
    node cites what it can resolve to a chunk, and offers the rest to a human."""
    out = []
    for shelf in CORPUS_SHELVES:
        d = os.path.join(ROOT, shelf)
        if not os.path.isdir(d):
            continue
        for f in sorted(os.listdir(d)):
            if f.lower().endswith(".zim"):
                out.append((None, re.sub(r"\.zim$", "", f, flags=re.I),
                            os.path.join(d, f)))
    return out


def build_library(km, zims, lib_path, zim_prefix=None):
    # BUILD IT WHERE IT WILL LIVE. Not a stylistic choice:
    # kiwix-manage RELATIVISES whatever path you hand it against the library
    # file's own directory, so a library built in a temp directory records
    # "../../real/place/x.zim" and is wrong the moment it is copied. There is no
    # way to make it store an absolute path; --zimPathToSave is relativised too.
    #
    # kiwix-manage APPENDS, so the target must start empty, and `os.remove` fails
    # with "Operation not permitted" on the Cowork bridge mount - which permits
    # writing a file but not unlinking one. Truncating to an empty library
    # element does the same job and works on both.
    try:
        if os.path.exists(lib_path):
            os.remove(lib_path)
    except OSError:
        with io.open(lib_path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write('<?xml version="1.0" encoding="UTF-8" ?>\n'
                     '<library version="20110515">\n</library>\n')
    paths = [z[2] for z in zims]
    missing = [p for p in paths if not os.path.exists(p)]
    if missing:
        sys.exit("these ZIMs are not on disk:\n  " + "\n  ".join(missing))
    # ALWAYS WRITE FORWARD SLASHES, and this is not cosmetic.
    #
    # On Windows kiwix-manage stores "07-corpora-supplemental\\mdwiki....zim".
    # kiwix-serve then derives the book key by splitting on "/" only, so the
    # backslash is not a separator to it: the whole string minus the extension
    # becomes the key, and the catalogue advertises
    #     /content/07-corpora-supplemental/mdwiki_en_all_maxi_2025-11
    # instead of
    #     /content/mdwiki_en_all_maxi_2025-11
    # The book resolves - the catalogue says so - but the entry path inside the
    # ZIM does not, and every article 404s with "the requested path was not
    # found". Measured on 2026-09-04: identical library, identical ZIMs, forward
    # slashes returned 200 on all 16 test citations and backslashes returned 404.
    #
    # --zimPathToSave is the only lever kiwix-manage offers, and it is relativised
    # against the library's directory, which is where the library is built, so
    # passing the already-relative POSIX form is a no-op that fixes the separator.
    if not zim_prefix:
        # PASS AN ABSOLUTE PATH IN THE PLATFORM'S OWN FORM. Two ways of getting
        # this wrong were tried first, and both failed silently-ish:
        #
        #   relative      kiwix-manage resolves it against ITS OWN working
        #                 directory, not the library's, so running from the home
        #                 directory produced ..\\Users\\<user>\\07-corpora-...
        #   absolute, /   it does not recognise "C:/..." as absolute either -
        #                 same blind spot kiwix-serve has with --library - and
        #                 produced ../Users/<user>/C:/ark/07-corpora-...
        #
        # So: native separators here, and normalise the FILE afterwards. Do not
        # try to be clever with the input; kiwix-manage rewrites it regardless.
        for p in paths:
            r = subprocess.run([km, lib_path, "add", "--zimPathToSave",
                                os.path.abspath(p), p],
                               capture_output=True, text=True)
            if r.returncode:
                sys.exit("kiwix-manage failed on %s:\n%s" % (p, r.stderr))
        _rewrite_absolute(lib_path, paths)
        _assert_absolute_paths(lib_path)
        _assert_zims_resolve(lib_path)
        _assert_unique_keys(lib_path)
        return

    if zim_prefix:
        # --zimPathToSave takes one value, so one invocation per ZIM.
        #
        for p in paths:
            save = zim_prefix.rstrip("/\\") + "/" + os.path.basename(p)
            r = subprocess.run([km, lib_path, "add", "--zimPathToSave", save, p],
                               capture_output=True, text=True)
            if r.returncode:
                sys.exit("kiwix-manage failed on %s:\n%s" % (p, r.stderr))
        # THE OVERRIDE STILL STORES RELATIVE PATHS, so it still needs the
        # separator normalisation the default branch no longer does - and it
        # inherits the 09-04 constraint in full: a prefix with a directory in it
        # produces a key nothing answers to. Stated here rather than discovered.
        _normalise_separators(lib_path)
        _assert_zims_resolve(lib_path)
    else:
        r = subprocess.run([km, lib_path, "add"] + paths, capture_output=True, text=True)
        if r.returncode:
            sys.exit("kiwix-manage failed:\n%s" % r.stderr)



# `_assert_single_segment_keys` and `_assert_forward_slashes` were REMOVED on
# 2026-09-12, not kept as dead code. Both enforced the superseded rule - bare
# filenames only, forward slashes only - and called on a correct library they
# would now REJECT it: paths are absolute and, on Windows, backslashed. A
# retired check that still runs is worse than no check. Their replacements are
# `_assert_absolute_paths` and `_assert_unique_keys` below; the reasoning they
# encoded is in the comment block above and in DECISIONS.md 2026-09-12.


def _rewrite_absolute(lib_path, paths):
    r"""Replace every stored path with the absolute path to that ZIM.

    POST-PROCESSING ANOTHER TOOL'S OUTPUT, FOR THE SECOND TIME IN THIS FILE AND
    FOR THE SAME REASON. `kiwix-manage` relativises every path it is given
    against the library's own directory and offers no lever that changes it -
    `--zimPathToSave` is relativised too. So the only way to store what
    kiwix-serve actually accepts is to write it afterwards. Measured 2026-09-12:
    with an absolute path the server opens the ZIM and answers to the BASENAME;
    with a relative multi-segment path it answers to nothing at all.

    NATIVE SEPARATORS ARE KEPT. On Windows this writes `C:\ark\...` with
    backslashes, which is what the probe proved works - and it works BECAUSE the
    key is the basename, so the separator never reaches a URL. That is why
    `_assert_forward_slashes` no longer applies to these paths and why its
    replacement checks absoluteness instead: the separator mattered only while
    the whole stored string was the key."""
    byname = {os.path.basename(p): os.path.abspath(p) for p in paths}
    n = [0]

    def fix(m):
        v = m.group(1)
        full = byname.get(os.path.basename(v.replace("\\", "/")))
        if full and full != v:
            n[0] += 1
            return 'path="%s"' % full
        return m.group(0)

    out = re.sub(r'path="([^"]*)"', fix, io.open(lib_path, encoding="utf-8").read())
    io.open(lib_path, "w", encoding="utf-8", newline="\n").write(out)
    print("  rewrote %d path(s) to absolute" % n[0])


def _assert_absolute_paths(lib_path):
    """Every stored path must be absolute, which is what makes the key the
    basename. A relative path here is the 2026-09-04 failure returning: the
    server loads, reports success, and answers to nothing - not the stored
    string, not the basename, not the library name. All three were tried on
    2026-09-12 and all three 404'd."""
    bad = [v for v in re.findall(r'path="([^"]*)"',
                                 io.open(lib_path, encoding="utf-8").read())
           if not os.path.isabs(v.replace("/", os.sep))]
    if bad:
        sys.exit(
            "library.xml records %d RELATIVE path(s), e.g. %r\n"
            "  A relative multi-segment path is unaddressable: kiwix-serve loads\n"
            "  the library, says so, and 404s the book under every spelling of its\n"
            "  key. Paths must be absolute - see _rewrite_absolute()."
            % (len(bad), bad[0]))
    print("  all paths are absolute - the book key is the bare filename stem")


def _assert_unique_keys(lib_path):
    """Two ZIMs with the same filename on different shelves would collide.

    A NEW RISK THAT ARRIVED WITH THE SECOND SHELF, and it is silent. The key is
    the basename, so `02-corpora-core/x.zim` and `06-corpora-literature/x.zim`
    are one URL: the server answers with one of them and the other is simply
    gone, with nothing anywhere saying which. While the library held one
    directory this could not happen and nothing checked for it."""
    stems = {}
    for v in re.findall(r'path="([^"]*)"',
                        io.open(lib_path, encoding="utf-8").read()):
        stem = re.sub(r"\.zim$", "", os.path.basename(v.replace("\\", "/")), flags=re.I)
        stems.setdefault(stem, []).append(v)
    dupes = {k: v for k, v in stems.items() if len(v) > 1}
    if dupes:
        k, v = sorted(dupes.items())[0]
        sys.exit("%d book key(s) are claimed by more than one ZIM, e.g. %r:\n  %s\n"
                 "  The key is the basename, so one of these silently shadows the\n"
                 "  other. Rename a file or exclude a shelf."
                 % (len(dupes), k, "\n  ".join(v)))
    print("  all %d book keys are unique" % len(stems))


def _normalise_separators(lib_path):
    """Rewrite every path="..." to use forward slashes.

    Post-processing another tool's output is normally the wrong move, and it was
    resisted twice here. It is right in this one case: it is a separator
    normalisation, it is total, and nothing else controls the outcome.
    kiwix-manage relativises whatever it is handed and emits the platform
    separator while doing it, so on Windows there is no input that produces a
    forward slash. And the cost of getting it wrong is not a visible error - the
    server loads, lists 25 books, and 404s every article.

    Only the path attribute is touched. Titles and base64 favicons are left
    alone."""
    s = io.open(lib_path, encoding="utf-8").read()
    n = [0]

    def fix(m):
        v = m.group(1)
        if "\\" in v:
            n[0] += 1
            v = v.replace("\\", "/")
        return 'path="%s"' % v

    out = re.sub(r'path="([^"]*)"', fix, s)
    if n[0]:
        io.open(lib_path, "w", encoding="utf-8", newline="\n").write(out)
        print("  normalised %d path separator(s) to /" % n[0])


def _assert_zims_resolve(lib_path):
    """Every path in the library must name a file that exists, resolved the way
    kiwix-serve resolves it: relative to the library file's own directory.

    THIS IS THE CHECK THAT SHOULD HAVE EXISTED FIRST. Three separate malformed
    libraries were produced and handed over before it did, and each one loaded
    with "The library was successfully loaded." and then served nothing, or
    served books whose every article 404'd. The message is a statement about
    parsing the XML, not about finding the ZIMs, and nothing downstream says
    otherwise until a human clicks a link."""
    lib_dir = os.path.dirname(os.path.abspath(lib_path))
    missing = []
    for v in re.findall(r'path="([^"]*)"',
                        io.open(lib_path, encoding="utf-8").read()):
        full = v if os.path.isabs(v) else os.path.join(lib_dir, v)
        if not os.path.exists(full):
            missing.append(v)
    if missing:
        sys.exit("library.xml names %d ZIM(s) that do not exist where it says.\n"
                 "  first: %s\n  resolved from: %s\n"
                 "  kiwix-serve would load this library, report success, and serve "
                 "nothing." % (len(missing), missing[0], lib_dir))
    print("  all %d ZIM paths resolve to files on disk"
          % len(re.findall(r'path="[^"]*"',
                           io.open(lib_path, encoding="utf-8").read())))


def names_from(lib_path):
    root = ET.parse(lib_path).getroot()
    out = {}
    for b in root.findall("book"):
        out[os.path.basename(b.get("path", ""))] = {
            "name": b.get("name"), "title": b.get("title"),
            "language": b.get("language"), "id": b.get("id")}
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kiwix-manage")
    ap.add_argument("--library", default=None,
                    help="defaults to <archive>/library.xml with absolute paths "
                         "inside, which is what lets one server span shelves - "
                         "see default_library()")
    ap.add_argument("--from-library", help="parse this instead of running kiwix-manage")
    ap.add_argument("--verify-against", metavar="URL",
                    help="a RUNNING kiwix-serve, e.g. http://localhost:8080 - "
                         "confirms each book key against its catalogue and records "
                         "the confirmed keys")
    ap.add_argument("--zim-prefix", help="rewrite the ZIM directory recorded in library.xml")
    ap.add_argument("--check", action="store_true", help="report only, write nothing")
    args = ap.parse_args()

    # TWO SETS, AND THEY ANSWER TWO QUESTIONS.
    # `served` is what the library will hold: every .zim on every corpus shelf,
    # read off the disk. `zims` is what kiwix-books.json records a confirmed key
    # for: the 29 artifacts sources.json knows, because those are the only ones
    # a citation can ever point at. Conflating them is what left 686 GB
    # unreachable - being unindexed and being unserved are different faults.
    zims = zim_artifacts()
    served_set = all_zims()
    if not served_set:
        sys.exit("no .zim files found on %s" % ", ".join(CORPUS_SHELVES))
    if not zims:
        sys.exit("sources.json lists no ZIM artifacts")
    extra = len(served_set) - len(zims)
    print("library will hold %d ZIM(s) across %d shelf(s); %d of them are in "
          "the retrieval index and %d are served but never cited"
          % (len(served_set), len(CORPUS_SHELVES), len(zims), extra))
    if not args.library:
        args.library = default_library()

    lib_path = args.from_library or args.library
    # --verify-against MUST NOT REBUILD. It is a check, and a check that first
    # rewrites the thing it is checking is not one. Worse, the running server has
    # the OLD library loaded, so a rebuild mid-verify compares a live server
    # against a file it has never read. Found 2026-09-04 by doing exactly that.
    if not args.from_library and not args.verify_against:
        km = find_kiwix_manage(args.kiwix_manage)
        if args.check and not os.path.exists(lib_path):
            sys.exit("--check needs an existing library; run without it first")
        if not args.check:
            print("using %s" % km)
            build_library(km, served_set, lib_path, args.zim_prefix)
            print("wrote %s" % lib_path)
    if not os.path.exists(lib_path):
        sys.exit("no library at %s" % lib_path)

    served = {}
    if args.verify_against:
        served = catalog_keys(args.verify_against.rstrip("/"))
        if served:
            print("  keys it answers to, first five:")
            for k in sorted(served)[:5]:
                print("     %s" % k)
        print("\nkiwix-serve at %s serves %d book(s)"
              % (args.verify_against, len(served)))

    meta = names_from(lib_path)
    rows, bad = [], []
    for src, stem, path in zims:
        info = meta.get(os.path.basename(path)) or {}
        # MATCH ON THE LAST SEGMENT, NOT ON THE WHOLE KEY.
        # Two forms were observed on 2026-09-04, both derived from the stored
        # path minus the extension:
        #     /content/mdwiki_en_all_maxi_2025-11
        #     /content/07-corpora-supplemental/mdwiki_en_all_maxi_2025-11
        # WHAT DECIDES WHICH IS NOT KNOWN. The two observations differed in both
        # the binary (3.8.2 vs 3.8.1) and the path separator stored in
        # library.xml (a Linux build wrote "/", a Windows build wrote "\"), so
        # the cause is not isolated and no claim is made about it here. It does
        # not need to be isolated: the key is read from the server rather than
        # constructed, which is correct whatever the rule turns out to be.
        # What is stable across both forms is the LAST SEGMENT, so that is what
        # an artifact is matched on, and the WHOLE key is what gets recorded.
        hits = [k for k in served if k.rsplit("/", 1)[-1] == stem] if served else []
        key = hits[0] if len(hits) == 1 else None
        if served and not key:
            bad.append((src, stem, "ambiguous" if hits else "not served"))
        rows.append((src, stem, key, info.get("name"), info.get("title")))

    w = max(len(r[1]) for r in rows)
    print("\n%-3s %-*s %s" % ("id", w, "filename stem", "key the server answers to"))
    for src, stem, key, name, title in rows:
        print("%-3s %-*s %s" % (src, w, stem, key if key else ("-" if not served else "NOT SERVED")))

    if served:
        differ = sum(1 for r in rows if r[2] and r[2] != r[1])
        if differ:
            print("\n%d of %d keys are NOT the bare filename stem, and both forms have "
                  "been seen\non this archive. Which one a server uses depends on "
                  "something not isolated -\nthe binary and the path separator in "
                  "library.xml both differed between the two\nobservations. That is "
                  "exactly why this is read from the server instead of\nconstructed: "
                  "it is right whatever the rule is." % (differ, len(rows)))
    if bad:
        print("\n%d BOOK(S) THE SERVER DOES NOT ANSWER TO:" % len(bad))
        for src, stem, why in bad:
            print("   src %-3s %-46s %s" % (src, stem, why))
        sys.exit(1)

    if args.check:
        if not os.path.exists(BOOKS):
            print("\nNO %s - the resolver has no confirmed keys and will say so." % BOOKS)
            sys.exit(1)
        stored = json.load(io.open(BOOKS, encoding="utf-8"))["books"]
        miss = [r[1] for r in rows
                if (stored.get(str(r[0])) or {}).get("key") != (r[2] or r[1])]
        if miss:
            print("\nkiwix-books.json disagrees for %d artifact(s): %s" % (len(miss), miss[:5]))
            sys.exit(1)
        print("\nkiwix-books.json agrees with the library for all %d." % len(rows))
        return

    if not served and os.environ.get("ARK_SETUP"):
        print("\n(setup starts kiwix-serve and verifies these keys against it next)")
        return
    if not served:
        print("\nNOT WRITTEN: %s\n  Nothing was confirmed, because no running server "
              "was given.\n  Re-run with --verify-against http://localhost:8080 once "
              "kiwix-serve is up.\n  Until then the node marks every ZIM citation "
              "book_verified=false." % BOOKS)
        print("\nstart the server with:\n    kiwix-serve --port 8080 --library %s" % lib_path)
        return

    st = os.stat(lib_path)
    out = {"schema": 3,
           "from_library": os.path.abspath(lib_path),
           # STAMP THE LIBRARY THESE KEYS CAME FROM. A rebuilt library.xml with a
           # kiwix-books.json left over from the previous one produces citations
           # that look verified and 404 - which is exactly what happened on
           # 2026-09-04, twice. store.py compares these and says so.
           "library_mtime": int(st.st_mtime),
           "library_size": st.st_size,
           "verified_against": args.verify_against,
           "books": {str(src): {"key": key, "stem": stem,
                                "metadata_name": name, "title": title}
                     for src, stem, key, name, title in rows}}
    tmp = BOOKS + ".new"
    with io.open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1)
    # Replace by copy, not rename: the Cowork bridge mount permits writing a file
    # but not replacing one. rehash.sh carries this exact fallback.
    try:
        os.replace(tmp, BOOKS)
    except OSError:
        with io.open(tmp, encoding="utf-8") as a2, \
             io.open(BOOKS, "w", encoding="utf-8", newline="\n") as b2:
            b2.write(a2.read())
    print("\nwrote %s - %d keys confirmed against a live server" % (BOOKS, len(rows)))


def catalog_keys(base):
    """Ask the SERVER what it serves. This is the only authority: library.xml is an
    input to kiwix-serve and cannot say what kiwix-serve will do with it."""
    import urllib.request
    url = base + "/catalog/v2/entries?count=-1"
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            xml = r.read().decode("utf-8", "replace")
    except Exception as e:
        sys.exit("could not read %s\n  %s: %s\n  Is kiwix-serve running?"
                 % (url, type(e).__name__, e))
    keys = set()
    for href in re.findall(r'<link[^>]*type="text/html"[^>]*href="([^"]+)"', xml):
        if href.startswith("/content/"):
            keys.add(href[len("/content/"):].strip("/"))
    if not keys:
        sys.exit("the catalogue at %s listed no /content/ links" % url)
    return keys


if __name__ == "__main__":
    main()
