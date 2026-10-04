#!/usr/bin/env python3
"""
ci.py - every check that runs on a fresh clone, with no archive, no model and
no graphics card. What GitHub Actions runs, and what anyone can run first.

    python bin/ci.py                 run them all; skips what cannot run here
    python bin/ci.py --strict        a skip is a failure (what CI passes)

THE SELFTESTS ALREADY EXIST; THIS ONLY RUNS THEM, so CI cannot drift from what a
person runs by hand: each check below is a command the tool itself documents.
What needs the real archive (serve.py --selftest, index-query, the gazetteer's
--selftest against a built index) is not here and cannot be, and saying so is
part of the output: a green run proves the tools' logic, not an archive.

TWO CHECKS NEED MORE THAN PYTHON. The PQ selftests need numpy and faiss-cpu
(requirements/node.txt pins both); the page check needs Node.js to parse the
pages' JavaScript. Without them those checks are SKIPPED and named; --strict
turns a skip into a failure, so CI cannot pass by quietly running less.

NOTHING IS WRITTEN. Python is compiled in memory (no __pycache__), every
subprocess runs with PYTHONDONTWRITEBYTECODE, and the selftests build their
fixtures in the temporary folder.

Standard library only.
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
API = os.path.join(ROOT, "13-ark-node", "ark-api")
WEB = os.path.join(ROOT, "13-ark-node", "ark-web")

# (name, folder, arguments after the interpreter, needs)
CHECKS = [
    ("ark.py selftest", ROOT, ["bin/ark.py", "selftest"], ()),
    ("index-measure", ROOT, ["bin/index-measure.py", "--selftest"], ()),
    ("index-mirror-set", ROOT, ["bin/index-mirror-set.py", "--selftest"], ()),
    ("kiwix-probe-shelves", ROOT, ["bin/kiwix-probe-shelves.py", "--selftest"], ()),
    ("gazetteer builder", ROOT, ["bin/test-index-gazetteer.py"], ()),
    ("safety detector", API, ["safety.py", "--selftest"], ()),
    ("llm cancellation", API, ["llm.py", "--selftest"], ()),
    ("answer assembly", API, ["answer.py", "--selftest"], ()),
    ("store filters", API, ["store.py", "--selftest"], ()),
    ("serve, no index", API, ["serve.py", "--unit"], ()),
    ("pq build", ROOT, ["bin/index-pq-build.py", "--selftest"], ("numpy", "faiss")),
    ("pq probe", ROOT, ["bin/index-pq-probe.py", "--selftest"], ("numpy", "faiss")),
]


def importable(mod):
    try:
        __import__(mod)
        return True
    except Exception:                                      # noqa: BLE001
        return False


def compile_all():
    """Every .py in the tree parses under this interpreter. In memory."""
    bad = []
    n = 0
    for top in ("bin", "13-ark-node"):
        for dp, dn, fn in os.walk(os.path.join(ROOT, top)):
            dn[:] = [d for d in dn if d != "__pycache__"]
            for f in fn:
                if not f.endswith(".py"):
                    continue
                p = os.path.join(dp, f)
                n += 1
                try:
                    with open(p, "rb") as fh:
                        compile(fh.read(), p, "exec", dont_inherit=True)
                except SyntaxError as e:
                    bad.append("%s:%s %s" % (os.path.relpath(p, ROOT), e.lineno, e.msg))
    return n, bad


def check_pages(node):
    """services.js and every inline <script> in the pages parse (node --check)."""
    bad, n = [], 0
    tmp = tempfile.mkdtemp(prefix="ark-ci-")
    try:
        files = [(os.path.join(WEB, "services.js"), None)]
        for page in ("index.html", "home.html"):
            with open(os.path.join(WEB, page), encoding="utf-8") as fh:
                for i, js in enumerate(re.findall(r"<script>(.*?)</script>",
                                                  fh.read(), re.S)):
                    p = os.path.join(tmp, "%s.%d.js" % (page, i))
                    with open(p, "w", encoding="utf-8") as out:
                        out.write(js)
                    files.append((p, "%s script %d" % (page, i)))
        for p, label in files:
            n += 1
            r = subprocess.run([node, "--check", p], capture_output=True, text=True)
            if r.returncode:
                bad.append("%s: %s" % (label or os.path.relpath(p, ROOT),
                                       (r.stderr.strip().splitlines() or ["?"])[-1]))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return n, bad


def main():
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("--strict", action="store_true",
                    help="a check that cannot run here is a failure")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="print every check's full output, not only a failure's")
    a = ap.parse_args()

    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
    env.pop("ARK_CONFIG", None)          # a clone's defaults, not this machine's
    rows = []

    n, bad = compile_all()
    rows.append(("python parses", "FAIL" if bad else "ok", "%d files" % n, bad))

    for name, cwd, argv, needs in CHECKS:
        missing = [m for m in needs if not importable(m)]
        if missing:
            rows.append((name, "SKIP", "needs " + ", ".join(missing), []))
            continue
        t = time.time()
        r = subprocess.run([sys.executable] + argv, cwd=cwd, env=env,
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace")
        out = (r.stdout or "") + (r.stderr or "")
        # THE TALLY IF THERE IS ONE ("126/126", "selftest passed"), else the
        # last line: faiss prints a training warning after its own result.
        lines = [l.strip() for l in out.splitlines() if l.strip()]
        last = next((l for l in reversed(lines)
                     if re.match(r"^\d+/\d+$", l) or "pass" in l.lower()),
                    lines[-1] if lines else "")
        rows.append((name, "FAIL" if r.returncode else "ok",
                     "%.0f s  %s" % (time.time() - t, last[:60]),
                     out.splitlines()[-40:] if r.returncode else []))
        if a.verbose:
            print(out)

    node = shutil.which("node")
    if node:
        n, bad = check_pages(node)
        rows.append(("page scripts", "FAIL" if bad else "ok", "%d scripts" % n, bad))
    else:
        rows.append(("page scripts", "SKIP", "needs Node.js", []))

    print("\n  ark-node checks, Python %s on %s\n" % (sys.version.split()[0], sys.platform))
    for name, state, note, _ in rows:
        print("  %-5s %-22s %s" % (state, name, note))
    for name, state, _, detail in rows:
        if state == "FAIL" and detail:
            print("\n  --- %s ---" % name)
            for line in detail:
                print("    " + line)
    fails = sum(1 for r in rows if r[1] == "FAIL")
    skips = sum(1 for r in rows if r[1] == "SKIP")
    print("\n  %d checks: %d failed, %d skipped%s" % (
        len(rows), fails, skips,
        " (--strict: a skip fails)" if a.strict and skips else ""))
    print("  Not run here, because they need the archive: serve.py --selftest,\n"
          "  index-query.py, index-gazetteer.py --selftest.")
    return 1 if fails or (a.strict and skips) else 0


if __name__ == "__main__":
    sys.exit(main())
