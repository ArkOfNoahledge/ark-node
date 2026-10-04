#!/usr/bin/env python3
"""
kiwix-search-form.py - what does THIS kiwix-serve accept as a search URL?

    python bin/kiwix-search-form.py                       # against :8080
    python bin/kiwix-search-form.py --base http://host:8080
    python bin/kiwix-search-form.py --pattern "ada lovelace"

WHY THIS EXISTS. `store.KIWIX_SEARCH` is the URL the node offers an operator
when the index cannot answer their question - the door onto the 42 artifacts
kiwix-serve holds and retrieval does not cover. It was written from memory as
`/search?books.filter.lang=&pattern=...` and returns **400**.

THAT IS THE THIRD TIME ON THIS NODE THAT A URL WAS CONSTRUCTED RATHER THAN
FETCHED. The book key was wrong for eight days on the same reasoning; the OPDS
catalogue parser was wrong twice in one evening. The rule this archive already
wrote down, in bin/kiwix-library.py's own header, is that an input cannot tell
you what the server will do with it - so ask the server.

WHAT IT DOES, IN THE ORDER THAT MATTERS:

  1. READS THE SERVER'S OWN SEARCH FORM. kiwix-serve renders one on its landing
     page, and its field names are the authoritative answer - no guessing, no
     version table, no documentation for a build we are not running.
  2. PRINTS THE BODY OF THE 400. kiwix-serve explains its own rejections and
     nobody had looked; the message may name the missing parameter outright.
  3. TRIES CANDIDATE FORMS and reports status and hit count for each, so the
     winner is chosen by evidence rather than by which one was remembered first.

Standard library only. Reads, never writes, and starts no server.
"""

import argparse
import io
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.dont_write_bytecode = True


def get(url, timeout=20):
    try:
        r = urllib.request.urlopen(url, timeout=timeout)
        return r.getcode(), r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as e:                       # noqa: BLE001 - reported
        return 0, ("%s: %s" % (type(e).__name__, e)).encode()


def forms(html):
    """Every <form> whose action mentions search, with its field names."""
    out = []
    for m in re.finditer(rb"<form\b([^>]*)>(.*?)</form>", html, re.S | re.I):
        attrs, body = m.group(1), m.group(2)
        action = re.search(rb'action="([^"]*)"', attrs)
        action = action.group(1).decode("utf-8", "replace") if action else ""
        if "search" not in action.lower():
            continue
        fields = []
        for fm in re.finditer(rb"<(input|select)\b([^>]*)>", body, re.I):
            a = fm.group(2)
            nm = re.search(rb'name="([^"]*)"', a)
            val = re.search(rb'value="([^"]*)"', a)
            typ = re.search(rb'type="([^"]*)"', a)
            if nm:
                fields.append((nm.group(1).decode("utf-8", "replace"),
                               (typ.group(1).decode() if typ else "text"),
                               (val.group(1).decode("utf-8", "replace")
                                if val else "")))
        out.append((action, fields))
    return out


def book_langs():
    """stem -> declared language, read from library.xml."""
    here = os.path.dirname(os.path.abspath(__file__))
    lib = os.path.join(os.path.dirname(here), "library.xml")
    out = {}
    if not os.path.exists(lib):
        return out
    raw = io.open(lib, encoding="utf-8", errors="replace").read()
    for m in re.finditer(r"<book\b([^>]*)>", raw):
        a = m.group(1)
        pm = re.search(r'path="([^"]*)"', a)
        lm = re.search(r'language="([^"]*)"', a)
        if pm:
            stem = re.sub(r"\.zim$", "",
                          os.path.basename(pm.group(1).replace("\\", "/")), flags=re.I)
            out[stem] = (lm.group(1) if lm else "?")
    return out


def coverage(body, langs):
    """Which BOOKS the results actually came from, and in what languages.

    A 200 IS NOT COVERAGE, AND THIS IS THE THIRD TIME TODAY THAT DISTINCTION HAS
    MATTERED. `books.filter.lang=eng&books.filter.lang=spa` answers 200 and
    returns exactly as many links as `eng` alone - which is equally consistent
    with kiwix honouring both values and with it taking one and discarding the
    other. The only way to tell is to look at WHERE the hits came from, so the
    result hrefs are mapped back to book stems and those to the languages
    library.xml declares."""
    seen = {}
    for m in re.finditer(rb'href="[^"]*/(?:content|viewer)[/#]([^"/?#]+)', body):
        stem = m.group(1).decode("utf-8", "replace")
        seen[stem] = langs.get(stem, "?")
    by = {}
    for stem, lg in seen.items():
        by.setdefault(lg, []).append(stem)
    return seen, by


def hits(body):
    """How many results the page claims, without pretending to parse Kiwix's
    HTML. Two independent signals, because either alone can be zero for a page
    that worked."""
    n = len(re.findall(rb'<a\s+href="[^"]*/content/', body))
    m = re.search(rb"(\d[\d,]*)\s*(?:results|resultados)", body, re.I)
    return n, (m.group(1).decode() if m else "-")


def library_languages():
    """The `language` values kiwix-serve was actually handed, with counts.

    THE FILTER THAT WORKS IS A LANGUAGE FILTER, so which languages exist stops
    being a detail and becomes the design. This archive is deliberately
    bilingual - `wikipedia_es_all_maxi`, `gutenberg_es`, `wiktionary_es`,
    `ifixit_es`, and one of the five acceptance questions is in Spanish - so a
    fallback link carrying `lang=eng` would quietly hide every Spanish book on
    the drive. Read from library.xml rather than assumed, because ISO 639 has
    three spellings of most things and only one of them is in that file."""
    here = os.path.dirname(os.path.abspath(__file__))
    lib = os.path.join(os.path.dirname(here), "library.xml")
    if not os.path.exists(lib):
        return lib, {}
    counts = {}
    for m in re.finditer(r'language="([^"]*)"',
                         io.open(lib, encoding="utf-8", errors="replace").read()):
        for code in m.group(1).split(","):
            code = code.strip()
            if code:
                counts[code] = counts.get(code, 0) + 1
    return lib, counts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8080")
    ap.add_argument("--pattern", default="ada lovelace")
    ap.add_argument("--book", default=None,
                    help="a book name to use in per-book candidates; one is "
                         "taken from the landing page when omitted")
    a = ap.parse_args()
    base = a.base.rstrip("/")
    q = urllib.parse.quote(a.pattern, safe="")

    code, body = get(base + "/")
    print("landing page %s -> HTTP %s, %d bytes\n" % (base, code, len(body)))
    if code != 200:
        sys.exit("kiwix-serve is not answering at %s" % base)

    print("=" * 74)
    print("1. THE SERVER'S OWN SEARCH FORM")
    print("=" * 74)
    fs = forms(body)
    if not fs:
        print("   no <form action=...search...> on the landing page.")
        print("   That is an answer too: this build may only expose search from")
        print("   inside a book's viewer, in which case the honest fallback is a")
        print("   link to the LIBRARY rather than to a query.")
    for action, fields in fs:
        print("   action %s" % action)
        for nm, typ, val in fields:
            print("      field %-24s type=%-8s default=%r" % (nm, typ, val))

    # the first book on the landing page, for the per-book candidates
    book = a.book
    if not book:
        m = re.search(rb'href="[^"]*/(?:content|viewer)[/#]([^"/?#]+)', body)
        book = m.group(1).decode("utf-8", "replace") if m else None
    print("\n   a book to test with: %s" % (book or "(none found on the page)"))

    print("\n" + "=" * 74)
    print("2. WHAT THE 400 ACTUALLY SAYS")
    print("=" * 74)
    bad = "%s/search?books.filter.lang=&pattern=%s" % (base, q)
    code, body400 = get(bad)
    print("   %s" % bad)
    print("   HTTP %s" % code)
    txt = re.sub(rb"<[^>]+>", b" ", body400)
    txt = b" ".join(txt.split())[:600].decode("utf-8", "replace")
    print("   %s" % (txt or "(empty body)"))

    print("\n" + "=" * 74)
    print("3. WHAT LANGUAGES ARE ON THIS NODE")
    print("=" * 74)
    lib, langs = library_languages()
    if not langs:
        print("   could not read %s - falling back to eng/spa as candidates" % lib)
        langs = {"eng": 0, "spa": 0}
    for code, n in sorted(langs.items(), key=lambda kv: -kv[1]):
        print("   %-8s %d book(s)" % (code, n))
    codes = sorted(langs, key=lambda c: -langs[c])

    print("\n" + "=" * 74)
    print("4. CANDIDATE FORMS")
    print("=" * 74)
    cands = [
        ("bare pattern", "/search?pattern=%s" % q),
        ("pattern + userlang", "/search?pattern=%s&userlang=en" % q),
        ("lang filter eng", "/search?books.filter.lang=eng&pattern=%s" % q),
        ("filter.q", "/search?books.filter.q=&pattern=%s" % q),
        ("all books explicit", "/search?books.filter.maxSize=0&pattern=%s" % q),
    ]
    # DOES ONE LINK COVER BOTH LANGUAGES? That is the question this round exists
    # for. If none of these answers, the honest surface is one link per language
    # rather than one link that silently drops half the drive.
    # EVERY LANGUAGE ON ITS OWN, NOT JUST THE COMMONEST. The first version tested
    # only codes[0] - eng, 58 books - so `spa` alone was never asked, and that is
    # the one query that separates "the filter ignores the repeated parameter"
    # from "these books have no hits for this word". A probe that cannot tell
    # those apart cannot end the question, and this one was about to.
    for c in codes:
        cands.append(("single lang %s" % c,
                      "/search?books.filter.lang=%s&pattern=%s" % (c, q)))
    if len(codes) > 1:
        rep = "".join("books.filter.lang=%s&" % c for c in codes[:3])
        cands += [
            ("lang repeated", "/search?%spattern=%s" % (rep, q)),
            ("lang comma", "/search?books.filter.lang=%s&pattern=%s"
             % (",".join(codes[:3]), q)),
            ("lang pipe", "/search?books.filter.lang=%s&pattern=%s"
             % ("|".join(codes[:3]), q)),
            ("lang mul", "/search?books.filter.lang=mul&pattern=%s" % q),
        ]
    if book:
        cands += [
            ("books.name", "/search?books.name=%s&pattern=%s"
             % (urllib.parse.quote(book, safe=""), q)),
            ("content=", "/search?content=%s&pattern=%s"
             % (urllib.parse.quote(book, safe=""), q)),
            ("books.filter.title", "/search?books.filter.title=%s&pattern=%s"
             % (urllib.parse.quote(book, safe=""), q)),
        ]
    langs_by_book = book_langs()
    winners = []
    for label, path in cands:
        code, b = get(base + path)
        n, claimed = hits(b)
        seen, by = coverage(b, langs_by_book)
        spread = " ".join("%s:%d" % (lg, len(v)) for lg, v in sorted(by.items()))
        mark = "ok  " if code == 200 else "    "
        print("   %s %-20s HTTP %-4s links=%-4d books=%-3d %s"
              % (mark, label, code, n, len(seen), spread or "-"))
        if code == 200:
            # THE SCORE IS LANGUAGES COVERED, THEN BOOKS, THEN LINKS.
            # A form that answers 200 while reaching one language is worse than
            # one that reaches two, however many links it returns.
            winners.append((len(by), len(seen), n, label, path, by))

    print("\n" + "=" * 74)
    print("VERDICT")
    print("=" * 74)
    if not winners:
        print("Nothing returned 200. Read section 1 - if this build has no")
        print("cross-book search endpoint, the node should link to the LIBRARY")
        print("page instead and say that the operator picks a book first. That")
        print("is still a door, and it is still honest.")
        return 1
    winners.sort(reverse=True)
    nlang, nbooks, n, label, path, by = winners[0]
    want = len([c for c in codes if langs.get(c)])
    print("languages on this node: %d   best candidate reaches: %d" % (want, nlang))
    for lg, books in sorted(by.items()):
        print("   %-5s %d book(s): %s" % (lg, len(books), ", ".join(sorted(books)[:3])))
    print()
    if want > 1 and nlang < 2:
        print("NO FORM REACHED MORE THAN ONE LANGUAGE on this pattern. That may be")
        print("the filter, or it may be that %r simply has no hits in the other" % a.pattern)
        print("language - re-run with --pattern on a word that does, before")
        print("concluding. If it IS the filter, the surface offers one link per")
        print("language rather than one that silently drops half the drive.")
    else:
        print("Use %r:" % label)
        print("    %s" % path)
    print("\nWhichever is chosen goes in store.KIWIX_SEARCH and is fetched by")
    print("serve.py --selftest, which is how the 400 was found rather than shipped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
