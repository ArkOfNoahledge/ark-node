#!/usr/bin/env python3
"""
catalog-build.py - the public download catalog, generated from MANIFEST.csv.

    python bin/catalog-build.py            write 13-ark-node/catalog/catalog.csv
    python bin/catalog-build.py --check    exit 1 if the file on disk is stale
    python bin/catalog-build.py --report   also list every gap it found

WHY GENERATED. MANIFEST.csv is this archive's record: 205 rows, one per thing on
the drive or deliberately not on it, with notes that name this machine and its
history. A person building their own node needs something else: for each file,
where to get it, how to check it, and what its licence lets them do. Kept by
hand, a second list drifts from the first the day a file is added - the
argument this build has made about the ark.py epilog, the ark.toml example and
the site's figures. So the catalog is derived, and --check says when it is not.

WHAT IT KEEPS. Rows that describe a file or folder someone can obtain: status
VERIFIED or UNPROVEN, a real file name, a source URL. Project folders, notes,
plans, omissions, generated files and the pip wheel caches are left out; the
wheels are what `pip install -r requirements` produces, not something to fetch.

HOW EACH ROW IS FETCHED, one of:

    direct        the URL is the file (Kiwix, figshare, Protomaps, Mapterhorn)
    hf-file       one file in a Hugging Face repository, resolve/<revision>
    hf-repo       a whole Hugging Face repository, at a pinned commit if known,
                  or only the files `include` lists when the archive keeps a
                  subset (MANIFEST.csv notes: PARTIAL SELECTIVE CHECKOUT)
    github-asset  a file attached to a GitHub release
    manual        a person has to get it: a store, a catalogue page, a form

REVISIONS. A Hugging Face row is pinned to a commit when MANIFEST.csv's notes
say PINNED COMMIT <sha>, or when 13-ark-node/catalog/pins.csv, written by
`ark.py fetch --pin`, names the commit the archive's copy matches. A row pinned
by neither follows `main`, and the build lists it as a gap.

SIZES. `bytes` is what a fetch of the row downloads. For a file that is the
file's size on disk. For a whole repository it is pins.csv's fetch_bytes: what
`ark.py fetch` takes at the pinned commit (less the ONNX export and the weight
copies a .safetensors makes redundant), added up by `--pin` from that commit's
own file list. Without it, the row keeps the archive folder's size and the
build lists it as a gap.

The sha256 comes from MANIFEST.csv, or, when MANIFEST leaves it blank, from the
shelf's own CHECKSUMS.sha256 - the file was verified when it was filed, and the
shelf manifest is what records that.

LICENCES. `license_status` is `recorded` when 00-docs/CREDITS-LICENSES.md states
the licence, and `to-verify` when the rule below was written from general
knowledge. Nothing marked to-verify should be repeated as fact until someone has
read the upstream terms. `redistribute` answers the question a person building a
node for someone else will ask: yes, non-commercial, per-item, ask, or no.

Standard library only. Writes one file, LF line endings.
"""

import argparse
import csv
import io
import json
import os
import re
import sys
import urllib.parse

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.environ.get("ARK_ROOT") or os.path.dirname(HERE)
MANIFEST = os.path.join(ROOT, "MANIFEST.csv")
SOURCES = os.path.join(ROOT, "10-index", "sources.json")
OUT = os.path.join(ROOT, "13-ark-node", "catalog", "catalog.csv")
PINS = os.path.join(ROOT, "13-ark-node", "catalog", "pins.csv")

COLUMNS = ["id", "shelf", "file", "kind", "profile", "fetch", "fetch_url",
           "revision", "sha256", "bytes", "license", "license_status",
           "redistribute", "indexed", "source_page", "include"]

KEEP_STATUS = ("VERIFIED", "UNPROVEN")

# Folders under 09-software that pip or a package manager recreates, and files
# that describe this machine's install rather than something to download.
SKIP = re.compile(r"^(python-wheels|links-|requirements-|library\.xml)")

# THE STARTER KIT, 2026-09-28 (AUDIT-open-source.md section 4): four small ZIMs,
# the embedding model, the 16 GB profile's model, and the Windows binaries that
# run them. Everything else is `full`.
STARTER = {
    "zimgit-water_en_2024-08.zim", "zimgit-medicine_en_2024-08.zim",
    "zimgit-post-disaster_en_2024-05.zim", "wikipedia_es_medicine_maxi_2026-07.zim",
    "bge-m3/", "Qwen3.8-27B-IQ4_XS.gguf",
    "llamacpp-bin/llama-b10566-bin-win-cuda-12.4-x64.zip",
    "llamacpp-bin/cudart-llama-bin-win-cuda-12.4-x64.zip",
    "kiwix-tools/kiwix-tools_win-x86_64-3.8.1.zip",
    # macOS, Apple silicon (2026-10-09): `setup` fetches the software rows of
    # the platform it runs on, so the starter carries both and each machine
    # takes its own (ark.py row_platform).
    "llamacpp-bin/llama-b10566-bin-macos-arm64.tar.gz",
    "kiwix-tools/kiwix-tools_macos-arm64-3.8.2.tar.gz",
}

# Files the node needs on Windows or macOS that MANIFEST.csv records only as
# part of a folder row. Taken from the shelf's CHECKSUMS.sha256 at build time.
EXTRA = [
    ("09-software", "kiwix-tools/kiwix-tools_win-x86_64-3.8.1.zip",
     "https://download.kiwix.org/release/kiwix-tools/kiwix-tools_win-x86_64-3.8.1.zip"),
    ("09-software", "kiwix-tools/kiwix-tools_macos-arm64-3.8.2.tar.gz",
     "https://download.kiwix.org/release/kiwix-tools/kiwix-tools_macos-arm64-3.8.2.tar.gz"),
    ("09-software", "go-pmtiles_1.31.2_Windows_x86_64.zip",
     "https://github.com/protomaps/go-pmtiles/releases/download/v1.31.2/"
     "go-pmtiles_1.31.2_Windows_x86_64.zip"),
]

# (pattern on the file name, licence, status, redistribute). First match wins.
# `recorded` rows repeat 00-docs/CREDITS-LICENSES.md; change that file first.
LICENSES = [
    (r"^hesperian/", "Hesperian terms: copying encouraged, republishing prohibited",
     "recorded", "no"),
    (r"^fao/", "CC BY-NC-SA 3.0 IGO", "recorded", "non-commercial"),
    (r"^openstax", "CC BY 4.0 for most books; some CC BY-NC-SA, one CC BY-ND",
     "recorded", "per-item"),
    (r"^(wikipedia|wiktionary|wikispecies)_", "CC BY-SA 4.0", "recorded", "yes"),
    (r"stackexchange|stackoverflow", "CC BY-SA 4.0", "recorded", "yes"),
    (r"^(mdwiki|wikem|appropedia|energypedia|archlinux|scoutwiki|proofwiki|"
     r"artofproblemsolving|wikivet|librepathology|openstreetmap-wiki|restarters)",
     "CC BY-SA", "recorded", "yes"),
    (r"^gutenberg_", "Public domain in the US; Project Gutenberg trademark terms",
     "recorded", "yes"),
    (r"^(theworldfactbook|usda)", "Public domain (US Government work)",
     "recorded", "yes"),
    (r"^\d{8}\.pmtiles$", "ODbL 1.0, (c) OpenStreetMap contributors", "recorded",
     "yes"),
    (r"^zimgit-", "Mixed: component licences vary", "recorded", "per-item"),
    (r"^skin-of-color", "(c) Skin of Color Society", "recorded", "ask"),
    (r"^trueprepper", "(c) TruePrepper", "recorded", "ask"),
    (r"(?i)qwen|gemma|kokoro|bge-m3", "Apache-2.0", "recorded", "yes"),
    (r"(?i)phi-4|whisper|^ggml-", "MIT", "recorded", "yes"),
    (r"^piper-voices", "Per voice; see each voice's model card", "recorded",
     "per-item"),
    (r"^(llama\.cpp/|llamacpp-bin/)", "MIT", "recorded", "yes"),
    (r"^espeak-ng", "GPL-3.0", "recorded", "yes"),
    (r"^python-3", "PSF License", "recorded", "yes"),
    (r"^qbittorrent", "GPL-2.0", "recorded", "yes"),
    # FROM GENERAL KNOWLEDGE, NOT FROM CREDITS-LICENSES.md. Read the upstream
    # terms, then move the line above this comment and mark it recorded.
    (r"^ifixit_", "CC BY-NC-SA 3.0", "to-verify", "non-commercial"),
    (r"^khanacademy_", "CC BY-NC-SA 3.0 US", "to-verify", "non-commercial"),
    (r"^wikisource_", "CC BY-SA 4.0; many texts public domain", "to-verify", "yes"),
    (r"^kiwix-tools", "GPL-3.0", "to-verify", "yes"),
    (r"^mapterhorn", "Unknown: read download.mapterhorn.com terms", "to-verify",
     "ask"),
    (r"pmtiles", "BSD-3-Clause", "to-verify", "yes"),
    (r"^os-ubuntu", "Ubuntu: GPL and others, per package", "to-verify", "yes"),
    (r"^koppen-geiger", "CC BY 4.0 (Beck et al.)", "to-verify", "yes"),
    (r"^standard-ebooks", "CC0 and public domain", "to-verify", "yes"),
    (r"^freedict", "GPL (per dictionary)", "to-verify", "yes"),
    (r"^map-viewer", "BSD-3-Clause (MapLibre, PMTiles JS)", "to-verify", "yes"),
    (r"^(ollama|whisper\.cpp)", "MIT", "to-verify", "yes"),
]


def license_for(fname, url=""):
    """First rule matching the file name wins; failing that, the first matching
    the source URL (Qwen-Coder's `params` and `template` carry no model name,
    and their repository does)."""
    for text in (fname, url):
        for pat, lic, status, redis in LICENSES:
            if re.search(pat, text):
                return lic, status, redis
    return "Unknown", "to-verify", "ask"


def kind_of(fname, shelf):
    f = fname.lower()
    if f.endswith(".zim"):
        return "zim"
    if f.endswith(".gguf"):
        return "gguf"
    if f.endswith(".pmtiles"):
        return "pmtiles"
    if f.endswith(".pdf"):
        return "pdf"
    if f.endswith("/"):
        return "model-repo" if shelf.startswith("01-models") else "folder"
    if f.endswith((".zip", ".tar.gz", ".exe", ".msi")):
        return "software"
    return "file"


def fetch_of(url, fname, notes):
    """(fetch, fetch_url, revision) from what MANIFEST recorded."""
    base = fname.rstrip("/").split("/")[-1]
    u = url.strip().split(" ")[0]
    if not u.startswith("http"):
        return "manual", "", ""
    m = re.match(r"https://huggingface\.co/([^/]+/[^/]+)/?$", u)
    if m:
        pin = re.search(r"PINNED COMMIT ([0-9a-f]{40})", notes)
        rev = pin.group(1) if pin else "main"
        if fname.endswith("/"):
            return "hf-repo", "https://huggingface.co/%s" % m.group(1), rev
        return ("hf-file", "https://huggingface.co/%s/resolve/%s/%s"
                % (m.group(1), rev, base), rev)
    m = re.match(r"https://github\.com/([^/]+/[^/]+)/releases/tag/([^/\s]+)", u)
    if m and not fname.endswith("/"):
        return ("github-asset", "https://github.com/%s/releases/download/%s/%s"
                % (m.group(1), m.group(2), base), m.group(2))
    if u.rstrip("/").endswith(base) and not fname.endswith("/"):
        return "direct", u, ""
    # SAVED UNDER ANOTHER NAME. Mapterhorn serves planet.pmtiles and this
    # archive files it as mapterhorn-planet-full.pmtiles: the URL is still the
    # file, and the catalog's `file` column says what to call it on disk.
    last = u.rstrip("/").rsplit("/", 1)[-1]
    ext = base.rsplit(".", 1)[-1].lower() if "." in base else ""
    if ext and last.lower().endswith("." + ext) and not fname.endswith("/"):
        return "direct", u, ""
    if "/resolve/" in u:
        return "direct", u, ""
    # figshare's ndownloader URLs are the file, named only by a number.
    if re.match(r"https://ndownloader\.figshare\.com/files/\d+$", u) \
            and not fname.endswith("/"):
        return "direct", u, ""
    return "manual", "", ""


def shelf_checksums(shelf):
    """{relative path: sha256} from <shelf>/CHECKSUMS.sha256, if there is one."""
    top = shelf.split("/")[0]
    path = os.path.join(ROOT, top, "CHECKSUMS.sha256")
    out = {}
    if os.path.isfile(path):
        for line in open(path, encoding="utf-8", errors="replace"):
            parts = line.strip().split(None, 1)
            if len(parts) == 2 and re.fullmatch(r"[0-9a-f]{64}", parts[0]):
                rel = parts[1].lstrip("*").lstrip("./").replace("\\", "/")
                out[rel] = parts[0]
    return out


def slug(fname):
    b = fname.rstrip("/").split("/")[-1]
    b = re.sub(r"\.(zim|gguf|pmtiles|pdf|zip|tar\.gz|exe|msi|txt|md|bin)$", "", b)
    return re.sub(r"[^A-Za-z0-9._-]+", "-", b).strip("-").lower()


def indexed_set():
    """Where each index artifact lives, relative to the archive root, or None.

    BY PATH, NOT BY NAME, SINCE 2026-10-06. This compared bare names, so the
    one indexed `02-corpora-core/openstax/` made the three other `openstax/`
    folders (03, 04, 06) read `indexed: yes` when none of them is in the index.
    corpus.py's artifact_path() and covers() are the one definition; the node's
    library page and this column now agree."""
    try:
        d = json.load(open(SOURCES, encoding="utf-8"))
        return set(_corpus().artifact_path(v) for v in d["artifacts"].values())
    except (OSError, ValueError, KeyError):
        return None


def _corpus():
    """13-ark-node/ark-api/corpus.py: the topic list, what counts as corpus,
    and how an index artifact maps to a catalog row."""
    if "corpus" not in sys.modules:
        sys.path.insert(0, os.path.join(ROOT, "13-ark-node", "ark-api"))
    import corpus                                           # noqa: E402
    return corpus


def check_topics(out):
    """topics.csv must name exactly the catalog's corpus rows, with known
    topics, a language and a yes/no suggest flag. A corpus without a topics row
    would never be suggested and would draw on no topic tile; a topics row for
    a corpus that left the catalog would suggest something unfetchable."""
    c = _corpus()
    try:
        top = c.load_csv(c.TOPICS_CSV)
    except OSError as e:
        return ["topics.csv unreadable: %s" % e]
    bad = []
    corpus_ids = [d["id"] for d in out if c.is_corpus(d)]
    tids = [t["id"] for t in top]
    for i in sorted(set(corpus_ids) - set(tids)):
        bad.append("corpus row with no topics.csv row: %s" % i)
    for i in sorted(set(tids) - set(corpus_ids)):
        bad.append("topics.csv row for no corpus in the catalog: %s" % i)
    for i in sorted(set(x for x in tids if tids.count(x) > 1)):
        bad.append("topics.csv names %s twice" % i)
    for t in top:
        for x in (t.get("topics") or "").split("|"):
            if x and x not in c.TOPIC_IDS:
                bad.append("%s: unknown topic %r (the list is in corpus.py)" % (t["id"], x))
        if not t.get("topics"):
            bad.append("%s: no topics" % t["id"])
        if t.get("lang") not in ("en", "es", "mul"):
            bad.append("%s: lang must be en, es or mul" % t["id"])
        if t.get("suggest") not in ("yes", "no"):
            bad.append("%s: suggest must be yes or no" % t["id"])
        if t.get("suggest") == "yes" and not (t.get("keywords_en") or t.get("keywords_es")):
            bad.append("%s: suggested but has no keywords" % t["id"])
        if not t.get("blurb_en"):
            bad.append("%s: no blurb_en" % t["id"])
    return bad


def load_pins():
    """{id: row} from pins.csv, which `ark.py fetch --pin` writes; {} if none."""
    if not os.path.isfile(PINS):
        return {}
    with open(PINS, encoding="utf-8", newline="") as fh:
        return dict((r["id"], r) for r in csv.DictReader(fh))


def build():
    rows = list(csv.DictReader(open(MANIFEST, encoding="utf-8")))
    idx = indexed_set()
    sums = {}
    out, gaps = [], []

    def add(shelf, fname, url, sha, size, notes):
        lic, lstat, redis = license_for(fname, url)
        fetch, furl, rev = fetch_of(url, fname, notes)
        top = shelf.split("/")[0]
        if top not in sums:
            sums[top] = shelf_checksums(top)
        sub = shelf[len(top):].strip("/")
        rel = "/".join(p for p in (sub, fname) if p)
        shelf_sha = sums[top].get(rel, "") if not fname.endswith("/") else ""
        if not re.fullmatch(r"[0-9a-f]{64}", sha or ""):
            sha = shelf_sha
        elif shelf_sha and shelf_sha != sha:
            gaps.append("sha256 differs between MANIFEST.csv and %s/CHECKSUMS.sha256: "
                        "%s" % (top, fname))
        # THE SIZE ON DISK WINS OVER THE SIZE IN THE MANIFEST, 2026-09-30. The
        # first `fetch --list` reported the Qwen IQ4_XS model as a CONFLICT: the
        # manifest said 16,857,890,816 bytes and the file, whose sha256 matches
        # both manifests, is 15,567,824,480. Twelve rows were off, the four Qwen
        # quantizations by up to 2 GB, six Stack Exchange ZIMs by a few KB to 6 MB
        # - sizes written from a download page's rounded figure and never
        # re-read. The file is what was checksummed, so its size is the truth;
        # the manifest's figure is reported as a gap to be corrected there.
        p = os.path.join(ROOT, top, *rel.split("/"))
        if os.path.isfile(p) and not fname.endswith("/"):
            real = str(os.path.getsize(p))
            if size and size != real:
                gaps.append("MANIFEST.csv size is wrong for %s: %s recorded, %s on disk"
                            % (fname, size, real))
            size = real
        rpath = shelf.strip("/") + "/" + fname.strip("/")
        ix = "" if idx is None else (
            "yes" if any(_corpus().covers(a, rpath) for a in idx) else "no")
        page = url.strip().split(" ")[0] if url.strip().startswith("http") else ""
        # A PARTIAL REPOSITORY, 2026-09-30. piper-voices is 3,301 files at its
        # pinned commit and the archive keeps 10: five voices, each an .onnx
        # and its .onnx.json. A fetch of the row took the whole repository. The
        # archive's own folder is the list, as MANIFEST.csv's notes say
        # (PARTIAL SELECTIVE CHECKOUT), and the row's size is what those
        # files weigh, which is what a fetch now downloads.
        include = ""
        if fetch == "hf-repo" and "PARTIAL SELECTIVE CHECKOUT" in notes:
            d = os.path.join(ROOT, top, *rel.split("/"))
            names = []
            for a, _ds, fs in os.walk(d):
                for f in fs:
                    if not f.startswith("CHECKSUMS"):
                        names.append(os.path.relpath(os.path.join(a, f), d)
                                     .replace(os.sep, "/"))
            if names:
                include = ";".join(sorted(names))
                size = str(sum(os.path.getsize(os.path.join(d, *n.split("/")))
                               for n in names))
            else:
                gaps.append("a partial repository with no archive folder to list: "
                            "%s" % fname)
        out.append({
            "id": slug(fname), "shelf": shelf, "file": fname,
            "kind": kind_of(fname, shelf),
            "profile": "starter" if fname in STARTER else "full",
            "fetch": fetch, "fetch_url": furl, "revision": rev,
            "sha256": sha, "bytes": size, "license": lic,
            "license_status": lstat, "redistribute": redis, "indexed": ix,
            "source_page": page, "include": include})
        if fetch != "manual" and not sha and not fname.endswith("/"):
            gaps.append("no sha256 for a fetchable file: %s" % fname)
        if lstat == "to-verify":
            gaps.append("licence to verify: %s (%s)" % (fname, lic))

    for r in rows:
        f = (r["filename"] or "").strip()
        if (r["status"] not in KEEP_STATUS or not f or f.startswith("(")
                or SKIP.match(f) or not r["source_url"].strip().startswith("http")):
            continue
        add(r["category"], f, r["source_url"], r["sha256"],
            r["expected_size_bytes"], r["notes"])
    for shelf, f, url in EXTRA:
        add(shelf, f, url, "", "", "")

    out.sort(key=lambda d: (d["profile"] != "starter", d["shelf"], d["file"]))
    # IDS ARE UNIQUE OR THE BUILD SAYS WHY NOT. A fetch tool keys on them, so
    # two rows called `openstax` would be one download and one silent skip. The
    # same folder name on several shelves takes the shelf number; the same
    # release in two formats takes its extension.
    def ext(d):
        b = d["file"].rstrip("/").lower()
        return "tar.gz" if b.endswith(".tar.gz") else b.rsplit(".", 1)[-1]
    seen = {}
    for d in out:
        seen.setdefault(d["id"], []).append(d)
    for i, ds in seen.items():
        if len(ds) > 1:
            by_shelf = len(set(d["shelf"] for d in ds)) == len(ds)
            for d in ds:
                d["id"] = "%s-%s" % (i, d["shelf"][:2] if by_shelf else ext(d))
    ids = {}
    for d in out:
        ids[d["id"]] = ids.get(d["id"], 0) + 1
    for i, n in ids.items():
        if n > 1:
            gaps.append("duplicate id %s (%d rows)" % (i, n))
    # PINS, 2026-09-30. MANIFEST.csv recorded Hugging Face repository URLs, not
    # commits, so every such row but BGE-M3 followed `main`, and the first real
    # fetch met a file that had changed there (`params`, 188 bytes filed, 178
    # served). `ark.py fetch --pin` finds the commit whose files match the
    # archive's copy and writes pins.csv; this reads it. A MANIFEST pin wins and
    # a disagreement is a gap; a row still on `main` is a gap, with pins.csv's
    # reason when it has one.
    pins = load_pins()
    for d in out:
        if d["fetch"] not in ("hf-file", "hf-repo"):
            continue
        p = pins.get(d["id"]) or {}
        rev = p.get("revision", "")
        if re.fullmatch(r"[0-9a-f]{40}", rev):
            if d["revision"] == "main":
                d["revision"] = rev
                if d["fetch"] == "hf-file":
                    # The repository's own name for the file: the archive
                    # renamed some (mmproj-BF16.gguf became
                    # mmproj-gemma-4-12b-it-BF16.gguf), and --pin found them by
                    # sha256. `file` stays the archive's name.
                    d["fetch_url"] = "%s/resolve/%s/%s" % (
                        d["fetch_url"].split("/resolve/", 1)[0], rev,
                        urllib.parse.quote(p.get("path") or d["file"]))
                # LINE ENDINGS. The archive's small text files were saved with
                # CRLF; upstream has LF. The catalog lists what a download
                # produces, and says the archive's copy differs.
                if p.get("upstream_sha256") and d["fetch"] == "hf-file":
                    d["sha256"] = p["upstream_sha256"]
                    d["bytes"] = p.get("upstream_bytes") or d["bytes"]
                    gaps.append("the archive's %s has CRLF line endings; the catalog "
                                "lists the upstream LF file" % d["id"])
            elif d["revision"] != rev:
                gaps.append("pins.csv puts %s at %s, MANIFEST.csv at %s"
                            % (d["id"], rev[:10], d["revision"][:10]))
        # WHAT A FETCH TAKES, 2026-10-01. A repository row carried its archive
        # folder's size: BGE-M3 6.9 GB, of which a fetch takes 2.3 GB, so the
        # starter kit read 24.5 GB for about 19.9 GB. pins.csv's fetch_bytes
        # is the commit's own figure for what `ark.py fetch` takes.
        if d["fetch"] == "hf-repo":
            fb = p.get("fetch_bytes", "")
            if fb.isdigit() and p.get("revision") == d["revision"]:
                d["bytes"] = fb
            else:
                gaps.append("%s: bytes is the archive folder's, not what a fetch "
                            "takes; run python bin/ark.py fetch --pin %s"
                            % (d["id"], d["id"]))
        if p.get("state") == "DISAGREES":
            gaps.append("the archive's copy of %s is not the pinned commit: %s"
                        % (d["id"], p.get("note", "")))
        if d["revision"] == "main":
            gaps.append("follows main, not pinned: %s%s" % (
                d["id"], (" (%s)" % p["note"]) if p.get("note") else ""))
    if idx is not None:
        paths = [d["shelf"].strip("/") + "/" + d["file"].strip("/") for d in out]
        for a in sorted(idx):
            if not any(_corpus().covers(a, p) for p in paths):
                gaps.append("indexed but not in the catalog: %s" % a)
    starters = set(d["file"] for d in out if d["profile"] == "starter")
    for s in sorted(STARTER - starters):
        gaps.append("starter file missing from the catalog: %s" % s)
    return out, gaps


def render(out):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=COLUMNS, lineterminator="\n")
    w.writeheader()
    w.writerows(out)
    return buf.getvalue()


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0].strip())
    ap.add_argument("--check", action="store_true",
                    help="exit 1 if catalog.csv differs from a fresh build")
    ap.add_argument("--report", action="store_true",
                    help="list every gap, not only the count")
    a = ap.parse_args()
    out, gaps = build()
    text = render(out)
    tbad = check_topics(out)
    if a.check:
        cur = open(OUT, encoding="utf-8").read() if os.path.isfile(OUT) else ""
        if cur != text:
            print("catalog.csv is STALE against MANIFEST.csv: "
                  "run python bin/catalog-build.py")
            return 1
        print("catalog.csv matches MANIFEST.csv (%d rows)" % len(out))
        if tbad:
            print("topics.csv disagrees with the catalog:")
            for b in tbad:
                print("    " + b)
            return 1
        print("topics.csv covers the %d corpus rows"
              % sum(1 for d in out if _corpus().is_corpus(d)))
        return 0
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    by = {}
    for d in out:
        by[d["fetch"]] = by.get(d["fetch"], 0) + 1
    st = [d for d in out if d["profile"] == "starter"]
    size = sum(int(d["bytes"] or 0) for d in st)
    print("wrote %s" % OUT)
    print("  %d rows: %s" % (len(out), ", ".join("%s %d" % kv for kv in sorted(by.items()))))
    print("  starter: %d rows, %.1f GB, what a fetch downloads" % (len(st), size / 1e9))
    hf = [d for d in out if d["fetch"] in ("hf-file", "hf-repo")]
    print("  Hugging Face rows: %d pinned to a commit, %d follow main"
          % (sum(d["revision"] != "main" for d in hf),
             sum(d["revision"] == "main" for d in hf)))
    print("  licences: %d recorded, %d to verify"
          % (sum(d["license_status"] == "recorded" for d in out),
             sum(d["license_status"] == "to-verify" for d in out)))
    for b in tbad:
        print("  topics.csv: " + b)
    print("  %d gap(s)%s" % (len(gaps), "" if a.report or not gaps else
                               " - run with --report to list them"))
    if a.report:
        for g in gaps:
            print("    " + g)
    return 0


if __name__ == "__main__":
    sys.exit(main())
