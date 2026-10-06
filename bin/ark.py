#!/usr/bin/env python3
"""
ark.py - start the node's servers, say what is running, and stop them.

    python bin/ark.py --help          every verb and flag, and what each does

THE USAGE LIST USED TO BE WRITTEN OUT HERE AND IS NOT ANY MORE, deliberately.
A hand-kept list of verbs and flags in a comment is a true statement nobody
re-checks: rename `--no-models` and this docstring keeps advertising the old
flag with nothing to catch it. `--help` is generated from the parser at the
bottom of this file and cannot say something the code does not do, and the
component names and ports in its epilog are read out of `components()` for the
same reason. Same lesson as the size floor in backup-cold.sh, which carried
`RAISE THIS as the archive grows` for nine days: see DECISIONS.md 2026-09-08.

IT IS A CONVENIENCE AND MUST NEVER BE LOAD-BEARING. `RUNBOOK.md` stays the
source of truth and `up` prints the exact command it runs for every component,
so an operator whose Python is broken, or who does not trust this file, can copy
the line and run it by hand. Spec 8.10 asks that the node be recoverable from
printed instructions; a launcher that becomes the only way in would break that
promise while appearing to help.

WHY A SCRIPT AND NOT AN APPLICATION. This build rejected Docker ("a large
dependency to stand up BEFORE the thing you actually need") and rejected
TileServer-GL to avoid vendoring Node ("the one component nobody could rebuild
from printed instructions"). A compiled launcher is the same argument with fewer
excuses: it needs a toolchain the archive does not hold and produces a binary
nobody can audit or rebuild on the node. Standard library, one file, readable on
paper. See DECISIONS.md 2026-08-31 and 2026-09-07.

AND IT IS CROSS-PLATFORM ON PURPOSE. 8.10's rebuild promise is Linux-only; the
Linux secondary is the guaranteed-recoverable node and Windows is best-effort.
A Windows-only launcher would be polishing the path the promise does not cover.

RUNTIME STATE GOES IN THE OS TEMP DIRECTORY, never inside the archive. Every
artifact folder is checksummed, and a pid file written next to the payload makes
`verify.sh` report a mismatch that is indistinguishable from corruption. That
lesson is already recorded twice: library.xml and __pycache__ in rehash.sh.
"""

import argparse
import csv
import glob
import ctypes
import io
import json
import os
import platform
import shutil
import socket
import subprocess
import textwrap
import sys
import tempfile
import re
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
WINDOWS = os.name == "nt"
STATE = os.path.join(tempfile.gettempdir(), "ark-node-run.json")


# ---------------------------------------------------------------------------
# SETTINGS: ONE OPTIONAL FILE, AND NEVER A SURPRISE
# ---------------------------------------------------------------------------
#
# Every path, port and model this script used to carry as a constant has a name,
# a default and, where one already existed, the environment variable that set
# it. An optional ark.toml can change any of them. Highest wins:
#
#     1. the environment variable, e.g. ARK_LLAMA - what worked before still
#        works, unchanged
#     2. ark.toml, at $ARK_CONFIG or <archive root>/ark.toml
#     3. the default below, which is exactly what this script did before the
#        file existed
#
# WITH NO FILE, NOTHING CHANGES. That is the test this was written against on
# 2026-09-28: `commands` printed byte for byte what it printed before. A settings
# layer that quietly moved a default would be the launcher choosing for the
# operator, which is the 2026-09-07 interpreter lesson in a new costume.
#
# A KEY THIS SCRIPT DOES NOT KNOW IS REFUSED BY NAME, NOT IGNORED. `gpu_layer =
# 20` skipped in silence would leave the model on its default and the operator
# certain they had changed it. `up` starts nothing while any setting is wrong;
# `status`, `commands` and `config` still run and say what is wrong.
#
# STANDARD LIBRARY STILL. tomllib ships with Python 3.11 and later. With no
# ark.toml nothing is parsed, so an older Python runs every verb; with one, an
# older Python is told plainly what it lacks rather than half-reading the file.
#
# Paths in the file may be relative, and are then relative to the archive root.
# On Windows write them in 'single quotes', so TOML leaves the backslashes alone.
#
# THE DEFAULTS ARE RELATIVE TO THE ARCHIVE ROOT, 2026-10-01: llama-server in
# <root>/llama, the Python environment in <root>/.venv. Until then they named
# the folders one machine used, outside its archive; that machine now says so
# in its own ark.toml, and a fresh install needs no file at all. The llama.cpp
# zip is vendored in 09-software/llamacpp-bin. Setting paths.llama, or ARK_LLAMA.
#
# THE INTERPRETER DECIDES WHETHER THE NODE HAS A SEMANTIC HALF, and until
# 2026-09-07 this script did not know that. It started serve.py with
# `sys.executable` - whatever Python ran the launcher - which on this machine is
# the plain 3.12 without chromadb or sentence-transformers. The node then
# reported, correctly and unprompted, "Dense retrieval is not loaded, so this ran
# as KEYWORD only", and the operator had no way to know that a launcher had made
# that choice for them. RUNBOOK.md offers both interpreters and expects a person
# to pick; a tool that picks silently, and picks the lesser one, is worse than no
# tool. Setting paths.venv, or ARK_VENV.

SETTINGS = [
    # key, environment variable, default, kind, what it is
    ("paths.root", "ARK_ROOT", os.path.dirname(HERE), "path",
     "the archive: the folder holding 01-models to 13-ark-node"),
    ("paths.llama", "ARK_LLAMA", "llama", "path",
     "the folder holding llama-server"),
    ("paths.venv", "ARK_VENV", ".venv",
     "path", "the Python environment with the embedding stack; without it the "
             "node searches by keyword only"),
    ("paths.kiwix_serve", "ARK_KIWIX_SERVE",
     "09-software/kiwix-tools/bin/kiwix-serve", "path",
     "the kiwix-serve binary (.exe is added on Windows)"),
    ("paths.pmtiles", "ARK_PMTILES", "09-software/pmtiles-cli/pmtiles", "path",
     "the pmtiles binary (.exe is added on Windows)"),
    ("paths.library", "ARK_LIBRARY", "library.xml", "path",
     "the Kiwix library, built by bin/kiwix-library.py"),
    ("paths.catalog", "ARK_CATALOG", "13-ark-node/catalog/catalog.csv", "path",
     "the download catalog `fetch` reads, generated by bin/catalog-build.py"),
    ("paths.maps", "ARK_MAPS", "08-maps", "path",
     "the folder pmtiles serves"),
    ("ports.archive", "ARK_PORT_ARCHIVE", 8080, "port", "kiwix-serve"),
    ("ports.tiles", "ARK_PORT_TILES", 8081, "port", "pmtiles, the map tiles"),
    ("ports.node", "ARK_PORT_NODE", 8090, "port", "the surface"),
    ("ports.primary", "ARK_PORT_PRIMARY", 8091, "port", "the primary model"),
    ("ports.crosscheck", "ARK_PORT_CROSSCHECK", 8092, "port",
     "the cross-check model"),
    ("node.answer_prompt", "ARK_ANSWER_PROMPT", "knowledge",
     "choice:knowledge,reader",
     "knowledge: the model answers, the archive checks; reader: the older "
     "prompt, answers only from the passages"),
    ("node.crosscheck_thinking", "ARK_CROSSCHECK_THINKING", "off",
     "choice:on,off", "let the cross-check model think before answering "
                      "(off: measured 15 s against 74 s, same verdicts)"),
    ("node.rerank_depth", "ARK_RERANK_DEPTH", 400, "int",
     "dense candidates re-scored exactly per query"),
    ("models.primary.enabled", None, True, "bool",
     "start the primary model at all"),
    ("models.primary.name", None, "Qwen3.8-27B IQ4_XS", "str",
     "what status calls it"),
    ("models.primary.file", "ARK_PRIMARY_MODEL",
     "01-models/tier1-reasoning/Qwen3.8-27B-IQ4_XS.gguf", "path",
     "the GGUF file"),
    ("models.primary.alias", None, "qwen-primary", "str",
     "the name llama-server reports; also how status tells the two apart"),
    ("models.primary.gpu_layers", None, 64, "int",
     "layers on the GPU; 0 runs it in system RAM"),
    ("models.primary.context", None, 8192, "int", "context window, tokens"),
    ("models.primary.threads", None, 0, "int",
     "CPU threads; 0 lets llama.cpp decide"),
    ("models.primary.extra_args", None,
     ["--reasoning-format", "deepseek", "--reasoning-budget", "512"], "list",
     "added to the llama-server command. --reasoning-format deepseek is NOT "
     "optional for a thinking model: without it the thought trace renders as "
     "the answer"),
    ("models.primary.vram_mib", None, 14801, "int",
     "measured GPU footprint; up declines to start it where this does not fit"),
    ("models.primary.host_mib", None, 1135, "int",
     "measured system RAM footprint"),
    ("models.crosscheck.enabled", None, True, "bool",
     "start the cross-check model at all"),
    ("models.crosscheck.name", None, "Gemma 4 31B IQ4_XS", "str",
     "what status calls it"),
    ("models.crosscheck.file", "ARK_CROSSCHECK_MODEL",
     "01-models/tier1-reasoning/google_gemma-4-31B-it-IQ4_XS.gguf", "path",
     "the GGUF file; a different model FAMILY from the primary, or the "
     "cross-check checks nothing"),
    ("models.crosscheck.alias", None, "gemma-crosscheck", "str",
     "the name llama-server reports"),
    ("models.crosscheck.gpu_layers", None, 0, "int",
     "0 keeps it in system RAM, which is what lets it share a machine with "
     "the primary"),
    ("models.crosscheck.context", None, 8192, "int", "context window, tokens"),
    ("models.crosscheck.threads", None, 16, "int",
     "CPU threads; 0 lets llama.cpp decide"),
    ("models.crosscheck.extra_args", None, [], "list",
     "added to the llama-server command"),
    ("models.crosscheck.vram_mib", None, 212, "int",
     "measured GPU footprint, even at gpu_layers 0: its compute buffer"),
    ("models.crosscheck.host_mib", None, 20629, "int",
     "measured system RAM footprint; up declines to start it where this does "
     "not fit"),
]

_TRUE = ("1", "true", "yes", "on")
_FALSE = ("0", "false", "no", "off")


def _coerce(kind, raw):
    """One value, checked against its kind. Raises ValueError with a sentence."""
    if kind in ("path", "str"):
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError("expected text")
        return raw
    if kind in ("int", "port"):
        if isinstance(raw, bool):
            raise ValueError("expected a whole number, not true or false")
        try:
            v = int(raw)
        except (TypeError, ValueError):
            raise ValueError("expected a whole number")
        if kind == "port" and not 1 <= v <= 65535:
            raise ValueError("a port is between 1 and 65535")
        if kind == "int" and v < 0:
            raise ValueError("expected zero or more")
        return v
    if kind == "bool":
        if isinstance(raw, bool):
            return raw
        if str(raw).strip().lower() in _TRUE:
            return True
        if str(raw).strip().lower() in _FALSE:
            return False
        raise ValueError("expected true or false")
    if kind == "list":
        if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
            raise ValueError('expected a list of text, e.g. ["--flag", "value"]')
        return list(raw)
    if kind.startswith("choice:"):
        allowed = kind.split(":", 1)[1].split(",")
        v = str(raw).strip().lower()
        if v not in allowed:
            raise ValueError("expected one of %s" % ", ".join(allowed))
        return v
    raise ValueError("unknown kind %s" % kind)                # pragma: no cover


def _flatten(d, prefix=""):
    out = {}
    for k, v in d.items():
        if isinstance(v, dict):
            out.update(_flatten(v, prefix + k + "."))
        else:
            out[prefix + k] = v
    return out


class Settings:
    """Resolved values, where each came from, and what was wrong."""

    def __init__(self):
        self.values, self.sources, self.problems, self.file = {}, {}, [], None

    def __getitem__(self, key):
        return self.values[key]


def load_settings(environ=None, path=None):
    env = os.environ if environ is None else environ
    s = Settings()
    guess_root = env.get("ARK_ROOT") or os.path.dirname(HERE)
    cfg = path or env.get("ARK_CONFIG") or os.path.join(guess_root, "ark.toml")
    data = {}
    if os.path.isfile(cfg):
        s.file = cfg
        toml = None
        try:
            import tomllib as toml                            # Python 3.11+
        except ImportError:
            try:
                import tomli as toml                          # same API, a pip away
            except ImportError:
                s.problems.append(
                    "%s exists and this Python (%s) cannot read TOML. Use Python "
                    "3.11 or later, or remove the file to run on the defaults"
                    % (cfg, platform.python_version()))
        if toml is not None:
            try:
                with open(cfg, "rb") as fh:
                    data = _flatten(toml.load(fh))
            except Exception as e:                            # noqa: BLE001
                s.problems.append("%s could not be read: %s" % (cfg, e))
    elif path or env.get("ARK_CONFIG"):
        s.problems.append("the settings file %s does not exist" % cfg)
    known = set(k for k, _v, _d, _k, _h in SETTINGS)
    for k in sorted(set(data) - known):
        s.problems.append("%s: unknown setting %r - refused, not ignored. "
                          "`python bin/ark.py config --example` lists every "
                          "setting there is" % (os.path.basename(cfg), k))
    for key, var, default, kind, _help in SETTINGS:
        if var and env.get(var, "") != "":
            raw, src = env[var], "env " + var
        elif key in data:
            raw, src = data[key], os.path.basename(cfg)
        else:
            raw, src = default, "default"
        try:
            val = _coerce(kind, raw)
        except ValueError as e:
            s.problems.append("%s = %r (from %s): %s" % (key, raw, src, e))
            val, src = _coerce(kind, default), "default"
        s.values[key], s.sources[key] = val, src
    root = s.values["paths.root"]
    for key, _var, _default, kind, _help in SETTINGS:
        if kind == "path" and key != "paths.root":
            v = s.values[key]
            if not os.path.isabs(v):
                s.values[key] = os.path.join(root, *v.replace("\\", "/").split("/"))
    return s


CFG = load_settings()
ROOT = CFG["paths.root"]
LLAMA = CFG["paths.llama"]
VENV = CFG["paths.venv"]


def settings_problems(announce=False):
    """The list of what is wrong with the settings. Printed once, at the top of
    every verb, so a wrong ark.toml is seen before anything it affects."""
    if announce and CFG.problems:
        print("\n  SETTINGS - %d problem(s). A wrong value is replaced by its "
              "default; `up` starts nothing until each is fixed:" % len(CFG.problems))
        for pr in CFG.problems:
            print("    %s" % pr)
    return CFG.problems


def _toml_value(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, list):
        return "[" + ", ".join(json.dumps(x) for x in v) + "]"
    if "\\" in v and "'" not in v:
        return "'%s'" % v
    return json.dumps(v)


def example_toml():
    """ark.toml with every setting present, commented out, at its default.

    GENERATED, NOT KEPT. A hand-kept example drifts from the code the first time
    a setting is added - the same argument as the epilog below, which reads its
    ports out of components() rather than listing them."""
    out = ["# ark.toml - settings for bin/ark.py. Every line is commented out, so",
           "# this file as written changes nothing: remove the # from a line to",
           "# change that one setting. Relative paths are relative to the archive",
           "# root. On Windows write paths in 'single quotes'. An environment",
           "# variable, where one is named, still wins over this file.",
           "# Generated by: python bin/ark.py config --example"]
    section = None
    for key, var, default, kind, help_ in SETTINGS:
        sec, name = key.rsplit(".", 1)
        if sec != section:
            out += ["", "[%s]" % sec]
            section = sec
        note = help_ + ("  (env %s)" % var if var else "")
        out += textwrap.wrap(note, 76, initial_indent="# ",
                             subsequent_indent="# ")
        out.append("# %s = %s" % (name, _toml_value(default)))
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# MODEL PROFILES BY GPU MEMORY, 2026-09-30. The defaults above are one machine:
# the M18, an RTX 4090 Laptop with 16 GB (15,046 MiB free to a process), where
# every footprint in SETTINGS was read off llama-server's own log on 2026-09-07.
# Someone with an 8 or 12 GB card, or a 24 GB one, needs a different pair, and
# needs to know which numbers were measured and which were not.
#
# THE PAIRS KEEP THE RULE THE NODE DEPENDS ON: the cross-check is a different
# model FAMILY from the primary, or it checks nothing. Qwen with Gemma at 16 and
# 24 GB; Gemma 4 12B with Phi-4-mini at 12 GB; Phi-4-mini with Gemma 4 12B at
# 8 GB. The cross-check runs in system RAM in every profile, as it does on the
# M18, so the card holds only the primary.
#
# A PROFILE IS NOT A SETTING. It is a way to write the models section of
# ark.toml: `config --example --profile 12gb` prints a complete ark.toml with
# that section filled in, and ark.toml stays the only thing ark.py reads. What
# runs is what the file says.
#
# FOOTPRINTS carry their basis. `measured` is llama-server's log on a real load;
# `computed` adds a measured model's per-context parts (KV, recurrent state,
# compute buffer) to another quantisation's file size; `not measured` is 0, which
# `up` treats as "cannot judge" rather than guessing. `ark.py measure` turns
# any of them into a measurement: it loads the model, reads the buffer sizes
# llama-server prints, and stops it.

# MEASURED 2026-09-30 on the M18 with `ark.py measure`, which first reproduced
# the 09-07 figures for the 16gb pair buffer for buffer (14,801 / 1,135 and
# 212 / 20,629). Gemma 4 12B wholly on the GPU needs 8,478 MiB, 6,777 of it
# weights and 1,568 its KV cache at 8,192 tokens: more than an 8 GB card
# (8,188 MiB) holds, which is why the 8gb profile puts Phi-4-mini (3,461 MiB) on
# the card and Gemma in system RAM instead. The 24gb primary is the one figure
# left computed: that model cannot load whole on a 16 GB card to be measured.
_QWEN_ARGS = ["--reasoning-format", "deepseek", "--reasoning-budget", "512"]
_T1, _T2 = "01-models/tier1-reasoning/", "01-models/tier2-fallback/"
PROFILES = [
    {"name": "8gb", "gib": 8,
     "summary": "Phi-4-mini on the GPU, Gemma 4 12B checking it from system RAM",
     "fetch": ["microsoft_phi-4-mini-instruct-q4_k_m", "gemma-4-12b-it-q4_k_m"],
     "primary": {"name": "Phi-4-mini Q4_K_M", "alias": "phi-primary",
                 "file": _T2 + "microsoft_Phi-4-mini-instruct-Q4_K_M.gguf",
                 "gpu_layers": 99, "context": 8192, "threads": 0,
                 "extra_args": ["--reasoning-format", "deepseek"],
                 "vram_mib": 3461, "host_mib": 504, "basis": "measured 2026-09-30, M18"},
     "crosscheck": {"name": "Gemma 4 12B Q4_K_M", "alias": "gemma-crosscheck",
                    "file": _T2 + "gemma-4-12b-it-Q4_K_M.gguf",
                    "gpu_layers": 0, "context": 8192, "threads": 0, "extra_args": [],
                    "vram_mib": 131, "host_mib": 8377, "basis": "measured 2026-09-30, M18"}},
    {"name": "12gb", "gib": 12,
     "summary": "Gemma 4 12B on the GPU, Phi-4-mini checking it from system RAM",
     "fetch": ["gemma-4-12b-it-q4_k_m", "microsoft_phi-4-mini-instruct-q4_k_m"],
     "primary": {"name": "Gemma 4 12B Q4_K_M", "alias": "gemma-primary",
                 "file": _T2 + "gemma-4-12b-it-Q4_K_M.gguf",
                 "gpu_layers": 99, "context": 8192, "threads": 0,
                 "extra_args": ["--reasoning-format", "deepseek"],
                 "vram_mib": 8478, "host_mib": 572, "basis": "measured 2026-09-30, M18"},
     "crosscheck": {"name": "Phi-4-mini Q4_K_M", "alias": "phi-crosscheck",
                    "file": _T2 + "microsoft_Phi-4-mini-instruct-Q4_K_M.gguf",
                    "gpu_layers": 0, "context": 8192, "threads": 0, "extra_args": [],
                    "vram_mib": 111, "host_mib": 3422, "basis": "measured 2026-09-30, M18"}},
    {"name": "16gb", "gib": 16,
     "summary": "the M18's pair: Qwen3.8-27B IQ4_XS on the GPU, Gemma 4 31B in RAM",
     "fetch": ["qwen3.8-27b-iq4_xs", "google_gemma-4-31b-it-iq4_xs"],
     "primary": {"name": "Qwen3.8-27B IQ4_XS", "alias": "qwen-primary",
                 "file": _T1 + "Qwen3.8-27B-IQ4_XS.gguf",
                 "gpu_layers": 64, "context": 8192, "threads": 0,
                 "extra_args": _QWEN_ARGS, "vram_mib": 14801, "host_mib": 1135,
                 "basis": "measured 2026-09-07, M18"},
     "crosscheck": {"name": "Gemma 4 31B IQ4_XS", "alias": "gemma-crosscheck",
                    "file": _T1 + "google_gemma-4-31B-it-IQ4_XS.gguf",
                    "gpu_layers": 0, "context": 8192, "threads": 16, "extra_args": [],
                    "vram_mib": 212, "host_mib": 20629,
                    "basis": "measured 2026-09-07, M18"}},
    {"name": "24gb", "gib": 24,
     "summary": "Qwen3.8-27B Q4_K_M wholly on the GPU, Gemma 4 31B in RAM",
     "fetch": ["qwen3.8-27b-q4_k_m", "google_gemma-4-31b-it-iq4_xs"],
     # 16,949 MiB of weights (the file) + 512 KV + 598 recurrent + 175 compute,
     # the last three measured on the same model at the same context.
     "primary": {"name": "Qwen3.8-27B Q4_K_M", "alias": "qwen-primary",
                 "file": _T1 + "Qwen3.8-27B-Q4_K_M.gguf",
                 "gpu_layers": 99, "context": 8192, "threads": 0,
                 "extra_args": _QWEN_ARGS, "vram_mib": 18235, "host_mib": 0,
                 "basis": "computed from the IQ4_XS measurement"},
     "crosscheck": {"name": "Gemma 4 31B IQ4_XS", "alias": "gemma-crosscheck",
                    "file": _T1 + "google_gemma-4-31B-it-IQ4_XS.gguf",
                    "gpu_layers": 0, "context": 8192, "threads": 0, "extra_args": [],
                    "vram_mib": 212, "host_mib": 20629,
                    "basis": "measured 2026-09-07, M18"}},
]
_ROLE_KEYS = ("name", "file", "alias", "gpu_layers", "context", "threads",
              "extra_args", "vram_mib", "host_mib")


def profile(name):
    for pr in PROFILES:
        if pr["name"] == name.lower():
            return pr
    return None


def total_vram_mib():
    smi = shutil.which("nvidia-smi")
    if not smi:
        return None
    try:
        out = subprocess.run([smi, "--query-gpu=memory.total",
                              "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=10)
        return int(out.stdout.strip().splitlines()[0].strip())
    except Exception:                                        # noqa: BLE001
        return None


# A CARD SMALLER THAN THE SMALLEST PROFILE'S NAME, 2026-10-03. The first test on
# a second machine was an RTX 2060 with 6 GB (6,144 MiB). Profiles are named for
# card sizes, so nothing matched, and setup said "no NVIDIA card found" about a
# card nvidia-smi had just reported. Yet the 8gb profile's model is Phi-4-mini at
# 3,461 MiB measured, which a 6 GB card holds with room for the embedding model
# the index build and the node load beside it. So below the smallest name, the
# smallest profile is still chosen when its measured footprint and this headroom
# fit; and when even that does not fit, setup says the card is too small, not
# absent.
EMBED_HEADROOM_MIB = 1024


def pick_profile(total_mib):
    """The largest profile whose card this is. A card reports a little under its
    nominal size (the M18's 16 GB is 16,375 MiB), so 95 percent of it. Below the
    smallest, that profile if its measured model and EMBED_HEADROOM_MIB fit."""
    best = None
    for pr in PROFILES:
        if total_mib is not None and total_mib >= pr["gib"] * 1024 * 0.95:
            best = pr
    if best is None and total_mib is not None:
        need = smallest_need_mib()
        if need and total_mib >= need:
            best = PROFILES[0]
    return best


def smallest_need_mib():
    """GPU memory the smallest profile needs: its measured model plus headroom."""
    v = PROFILES[0]["primary"]["vram_mib"]
    return v + EMBED_HEADROOM_MIB if v else None


def profile_toml(pr):
    """A complete ark.toml: the models section set to the profile, every other
    setting commented out at its default, as `config --example` writes it."""
    lines = example_toml().splitlines()
    body = lines[lines.index("# Generated by: python bin/ark.py config --example") + 1:]
    out, role = [], None
    for ln in body:
        m = re.match(r"^\[models\.(primary|crosscheck)\]$", ln)
        if m:
            role = m.group(1)
        elif ln.startswith("["):
            role = None
        m2 = re.match(r"^# (\w+) = ", ln)
        if role and m2 and m2.group(1) in _ROLE_KEYS:
            out.append("%s = %s" % (m2.group(1), _toml_value(pr[role][m2.group(1)])))
            continue
        out.append(ln)
    head = ["# ark.toml - settings for bin/ark.py, for a card with %d GB." % pr["gib"],
            "# PROFILE %s: %s." % (pr["name"], pr["summary"]),
            "# The two [models] sections below are set; every other line is",
            "# commented out at its default, so it changes nothing until the # is",
            "# removed. Relative paths are relative to the archive root. An",
            "# environment variable, where one is named, still wins over this file.",
            "#",
            "# Footprints: primary %s; cross-check %s." % (pr["primary"]["basis"],
                                                         pr["crosscheck"]["basis"]),
            "# A footprint of 0 is not measured, and `up` cannot judge whether it",
            "# fits. `python bin/ark.py measure` loads each model, reads what it",
            "# reserves, and prints the two numbers to put here.",
            "# The models: python bin/ark.py fetch %s" % " ".join(pr["fetch"]),
            "# Generated by: python bin/ark.py config --example --profile %s" % pr["name"]]
    return "\n".join(head + out) + "\n"


def do_profiles(args):
    tv, ram = total_vram_mib(), free_ram_mib()
    pick = pick_profile(tv)
    print("\n  this machine: %s of GPU memory, %s of system RAM free\n"
          % ("unknown (no nvidia-smi)" if tv is None else "{:,} MiB".format(tv),
             "unknown" if ram is None else "{:,} MiB".format(ram)))
    for pr in PROFILES:
        mark = "  <- this card" if pick is pr else ""
        print("  %-5s %s%s" % (pr["name"], pr["summary"], mark))
        for role in ("primary", "crosscheck"):
            r = pr[role]
            fp = ("VRAM %s, RAM %s" % tuple(
                "%s MiB" % format(v, ",") if v else "?" for v in (r["vram_mib"], r["host_mib"]))
                  if r["vram_mib"] or r["host_mib"] else "footprint not measured")
            print("        %-10s %-22s %-14s %s (%s)" % (
                role, r["name"], "GPU, %s layers" % r["gpu_layers"] if r["gpu_layers"]
                else "system RAM", fp, r["basis"]))
        print("        fetch: python bin/ark.py fetch %s\n" % " ".join(pr["fetch"]))
    print("  Only the 16gb pair has been through the node's acceptance questions.\n"
          "  python bin/ark.py config --example --profile NAME > ark.toml   writes one.\n")
    return 0


_BUF = re.compile(r"(\S+)\s+(?:model|KV|RS|compute|output)\s+buffer size\s*=\s*"
                  r"([0-9.]+)\s*MiB", re.I)


def parse_buffers(lines):
    """(VRAM MiB, host MiB, [(device, MiB)]) from llama-server's load log. A
    device named for a GPU backend is VRAM; CPU and pinned host buffers
    (CUDA_Host) are system memory. On 2026-09-07 the CUDA0 lines of the primary
    summed to 14,800.99 MiB, the figure SETTINGS carries."""
    vram = host = 0.0
    seen = []
    for ln in lines:
        m = _BUF.search(ln)
        if not m:
            continue
        dev, mib = m.group(1).rstrip(":"), float(m.group(2))
        seen.append((dev, mib))
        u = dev.upper()
        if "HOST" in u or u.startswith("CPU"):
            host += mib
        else:
            vram += mib
    return vram, host, seen


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def measure_role(settings, timeout=900, quiet=False):
    """Load one model with these settings on a spare port, read what it reserves,
    stop it. Returns (ok, vram, host, seen, tail)."""
    import threading
    lserver = os.path.join(LLAMA, exe("llama-server"))
    port = _free_port()
    cmd = [lserver, "-m", settings["file"], "-ngl", str(settings["gpu_layers"])]
    if settings["threads"]:
        cmd += ["-t", str(settings["threads"])]
    # -lv 4, AND ONLY HERE. llama-server passes the library's INFO lines through
    # at TRACE (common/log.cpp: GGML_LOG_LEVEL_INFO -> LOG_LEVEL_TRACE, 4), and
    # its default threshold is 3, so the buffer sizes are not printed at all by
    # default. The first `measure` on the M18 loaded four models and read
    # nothing. `up` keeps the quiet default.
    cmd += ["-c", str(settings["context"]), "--host", "127.0.0.1", "--port", str(port),
            "-a", settings["alias"]] + list(settings["extra_args"]) + ["-lv", "4"]
    if not quiet:
        print("    %s" % " ".join(cmd))
    lines = []
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, errors="replace")
    except OSError as e:
        return False, 0, 0, [], ["could not start llama-server: %s" % e]
    ready = threading.Event()

    def reader():
        for ln in proc.stdout:
            lines.append(ln.rstrip("\n"))
            if "server is listening" in ln or "all slots are idle" in ln \
                    or "model loaded" in ln:
                ready.set()
        ready.set()
    threading.Thread(target=reader, daemon=True).start()
    ok = ready.wait(timeout) and proc.poll() is None
    try:
        proc.terminate()
        proc.wait(timeout=30)
    except Exception:                                        # noqa: BLE001
        proc.kill()
    vram, host, seen = parse_buffers(lines)
    if ok and not seen:
        lines.append("(the model loaded, and printed no buffer sizes to read)")
    return ok and bool(seen), vram, host, seen, lines[-12:]


def do_measure(args):
    pr = profile(args.profile) if args.profile else None
    if args.profile and not pr:
        print("\n  no profile %r: %s\n" % (args.profile, ", ".join(p2["name"] for p2 in PROFILES)))
        return 2
    roles = ["primary", "crosscheck"] if args.role == "both" else [args.role]
    up = [r for r in roles if listening(CFG["ports." + r])]
    if up:
        print("\n  REFUSED: %s is running and holds its memory, so a second copy would\n"
              "  measure the card with one model already on it. python bin/ark.py down\n"
              "  first.\n" % ", ".join(up))
        return 2
    print("\n  loading each model once, reading what llama-server reserves, stopping it"
          "\n  (%s)\n" % ("profile " + pr["name"] if pr else "the settings in use"))
    code = 0
    for role in roles:
        if pr:
            s = dict(pr[role])
            s["file"] = os.path.join(ROOT, *s["file"].split("/"))
        else:
            k = "models.%s." % role
            s = dict((key, CFG[k + key]) for key in _ROLE_KEYS)
        print("  %s  %s" % (role, s["name"]))
        if not os.path.exists(s["file"]):
            print("    not on this drive: %s\n" % s["file"])
            code = 1
            continue
        t0 = time.time()
        ok, vram, host, seen, tail = measure_role(s, args.timeout)
        if not ok:
            print("    did not load (%.0f s). The last lines it printed:" % (time.time() - t0))
            for ln in tail:
                print("      " + ln)
            print("")
            code = 1
            continue
        print("    loaded in %.0f s; buffers: %s" % (time.time() - t0, ", ".join(
            "%s %.2f" % (d, m) for d, m in seen)))
        print("    VRAM %s MiB, system RAM %s MiB. For ark.toml [models.%s]:\n"
              "      vram_mib = %d\n      host_mib = %d\n"
              % (format(round(vram), ","), format(round(host), ","), role,
                 round(vram), round(host)))
    return code


def do_config(args):
    if getattr(args, "profiles", False):
        return do_profiles(args)
    if getattr(args, "profile", None):
        pr = profile(args.profile)
        if not pr:
            print("\n  no profile %r: %s\n" % (args.profile,
                  ", ".join(p2["name"] for p2 in PROFILES)))
            return 2
        sys.stdout.write(profile_toml(pr))
        return 0
    if args.example:
        sys.stdout.write(example_toml())
        return 0
    print("\n  settings file  %s" % (CFG.file or
          "none - every value below is an environment variable or a default"))
    if not CFG.file:
        print("  (looked for %s)" % (os.environ.get("ARK_CONFIG") or
                                     os.path.join(ROOT, "ark.toml")))
    print("")
    w = max(len(k) for k, _v, _d, _k, _h in SETTINGS)
    for key, _var, _default, _kind, _help in SETTINGS:
        v = CFG[key]
        shown_v = " ".join(v) if isinstance(v, list) else str(v)
        print("  %-*s  %-9s %s" % (w, key, CFG.sources[key].split(" ")[0]
                                   if CFG.sources[key] == "default" else
                                   ("env" if CFG.sources[key].startswith("env")
                                    else "file"), shown_v))
    envs = [(k, CFG.sources[k][4:]) for k, *_ in SETTINGS
            if CFG.sources[k].startswith("env ")]
    if envs:
        print("\n  from the environment: %s"
              % ", ".join("%s (%s)" % (var, k) for k, var in envs))
    moved = [k for k, *_ in SETTINGS if k.startswith("ports.")
             and CFG.sources[k] != "default"]
    if moved:
        print("\n  NOTE: %s moved. The node hands its pages every port through"
              "\n  /api/ports.js, so links, the map and the Services page follow."
              "\n  A node started by hand, without ark.py, needs the `set` lines"
              "\n  `commands` prints." % ", ".join(moved))
    if CFG.problems:
        settings_problems(announce=True)
        print("")
        return 2
    print("\n  no problems\n")
    return 0


def node_python():
    """The keeper venv if it is there, else whatever is running this.

    Returns (path, note). The note is printed by `up`, by `commands` and by
    `status` for a node that is DOWN - the three places where nothing is
    running to ask - because the difference between these two interpreters is
    the difference between hybrid retrieval and keyword retrieval and it is
    invisible from the outside.

    THE TWO NOTES ARE NOT SYMMETRIC, AND THAT IS THE WHOLE CORRECTION OF
    2026-09-17. Absence of the venv ENTAILS keyword only: no venv, no embedding
    stack, no dense half, and the filesystem is a sufficient answer. Presence
    entails nothing. The positive note used to read `keeper venv: dense
    retrieval available` and was printed, truthfully about the directory, over a
    node that had served keyword only for three hours on 2026-09-16 because
    numpy raised inside that very venv.

    `status` was fixed the same day by asking `/api/health` instead. **These
    three callers cannot do that** - at `up` time the node has not started, and
    `commands` starts nothing at all - so the honest repair here is not a better
    measurement but a smaller claim: say what was FOUND and name the thing that
    reports what actually happened. Fixing the check and leaving its twin was
    the miss; Juan caught it in the `up` output the same evening."""
    cand = os.path.join(VENV, "Scripts", "python.exe") if WINDOWS \
        else os.path.join(VENV, "bin", "python")
    if os.path.exists(cand):
        return cand, "keeper venv found - `status` reports whether dense loaded"
    return sys.executable, ("no keeper venv at %s, so the node runs KEYWORD "
                            "ONLY - hybrid and dense will fall back and say so"
                            % VENV)


def p(*parts):
    return os.path.join(ROOT, *parts)


def exe(path):
    """The binary, with .exe where the platform wants one - once. A path
    written in ark.toml may already carry it."""
    if WINDOWS and not path.lower().endswith(".exe"):
        return path + ".exe"
    return path


# ---------------------------------------------------------------------------
# measurements, not guesses

# MEASURED 2026-09-07 at -c 8192, from llama-server -lv 6. BUILD-LOG has the
# full table. These are the numbers this script refuses on, so they are here in
# the open rather than as bare constants: Qwen occupies 14,801 MiB of VRAM and
# 1,135 MiB of host memory; Gemma occupies 20,629 MiB of host memory and, at
# -ngl 0, still 212 MiB of VRAM for its compute buffer. Free VRAM at the time
# was 15,046 MiB, so the two together leave about 33 MiB - which is arithmetic
# rather than an observation, and is why this script checks before starting
# rather than trusting the pair to fit.
# THE FIGURES NOW LIVE IN SETTINGS, per model: models.primary.vram_mib 14801
# and host_mib 1135, models.crosscheck.vram_mib 212 and host_mib 20629. They
# are the defaults there because they are what was measured here; a different
# model or card needs its own measurement, and ark.toml is where it goes.
MARGIN_MIB = 250


def free_vram_mib():
    """What the GPU says it has, or None if there is no nvidia-smi to ask.

    None is not zero. A machine with no NVIDIA GPU, or with the tool missing, is
    a machine this script cannot judge - and refusing to start on the strength
    of a measurement it could not take would be worse than starting."""
    smi = shutil.which("nvidia-smi")
    if not smi:
        return None
    try:
        out = subprocess.run(
            [smi, "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10)
        return int(out.stdout.strip().splitlines()[0].strip())
    except Exception:                                        # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# PROCESSES, NOT ONLY PORTS
# ---------------------------------------------------------------------------
#
# This script has judged the node by asking whether a port answers, and that is
# right for "is it up". It is blind to the thing that actually broke the node on
# 2026-09-10: THREE `serve.py` processes were alive, one served 8090, and the
# others held memory while answering nothing. Killing one stray took free RAM
# from 1,611 MiB to 25,360 MiB - a single dead node holding about 24 GB - and
# that pressure is what let Windows evict the primary's weights off the GPU.
#
# A port check cannot see any of that. `status` showed one healthy node and
# `down` stopped only the pid it had recorded. So the script learns to look at
# processes, and the rule it applies to them is the same one it already applies
# to pids: REPORT WHAT IT DID NOT START, NEVER KILL IT UNASKED. `down --strays`
# is the explicit ask.
#
# Standard library only, and it must degrade rather than fail: None means "could
# not look", which is different from "nothing there" and is printed differently.


def process_table():
    """[(pid, ppid, command line), ...] for every process, or None if blind.

    THE PARENT IS PART OF THE OBSERVATION, because without it two processes
    running the same file are indistinguishable from one process and its own
    child. On 2026-09-12, a clean `up` produced this:

        ProcessId : 44320  ParentProcessId : 23108  C:\\ark\\.venv\\...\\serve.py
        ProcessId : 6896   ParentProcessId : 44320  C:\\Python312\\...\\serve.py

    One node. 44320 is the node this script started under the keeper venv;
    6896 is a child it spawns under the base interpreter. Reading only the
    pids, `status` reported two servers, the interpreter warning named the
    CHILD's interpreter as the node's, and `down --strays` killed the child of
    the healthy running node - every session. Same shape as the 09-10 note
    below it: a check that is true about its own question (`two processes
    match`) and misleading about the one the operator is asking (`is a second
    server holding memory`)."""
    try:
        if WINDOWS:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-CimInstance Win32_Process | "
                 "Select-Object ProcessId,ParentProcessId,CommandLine | "
                 "ConvertTo-Csv -NoTypeInformation"],
                capture_output=True, text=True, timeout=25)
            if out.returncode != 0:
                return None
            rows = list(csv.reader(io.StringIO(out.stdout)))
            table = []
            for r in rows[1:]:
                if len(r) >= 3 and r[0].strip().isdigit():
                    ppid = int(r[1]) if r[1].strip().isdigit() else 0
                    table.append((int(r[0]), ppid, r[2] or ""))
            return table or None
        table = []
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                with open("/proc/%s/cmdline" % entry, "rb") as fh:
                    cmd = fh.read().replace(b"\0", b" ").decode("utf-8", "replace")
                # Field 4 of stat is the ppid, but field 2 is the executable
                # name IN PARENTHESES AND MAY CONTAIN SPACES OR PARENS, so the
                # fields are counted from after the LAST ')', never split().
                with open("/proc/%s/stat" % entry) as fh:
                    rest = fh.read().rsplit(")", 1)[-1].split()
                ppid = int(rest[1]) if len(rest) > 1 else 0
            except (OSError, ValueError, IndexError):
                continue
            if cmd.strip():
                table.append((int(entry), ppid, cmd))
        return table or None
    except Exception:                                        # noqa: BLE001
        return None


def _fingerprint(c):
    """What a running instance of this component looks like on a command line.

    The model servers share one binary and are told apart by the `-a` alias they
    were started with, which is also what they report as their model name - so
    the surface, the logs and this check all key on the same string."""
    cmd = c.get("cmd") or []
    if "-a" in cmd:
        return cmd[cmd.index("-a") + 1]
    if c["name"] == "node":
        return os.path.join("ark-api", "serve.py")
    return os.path.basename(cmd[0]) if cmd else c["name"]


def instances(c, table):
    """Every process that looks like this component. [] if none, None if blind.

    MATCHES, NOT SERVERS - a match may be a child of another match. Anything
    that wants to count servers, name strays, or say what the node is running
    must pass this through `roots()` first."""
    if table is None:
        return None
    fp = _fingerprint(c).replace("\\", "/").lower()
    hits = []
    for pid, ppid, cmd in table:
        if fp and fp in cmd.replace("\\", "/").lower():
            hits.append((pid, ppid, cmd))
    return hits


def _descendant_of(pid, owners, table):
    """Is `pid` below any pid in `owners`? Walks up the parent chain, with a
    depth cap so a corrupt or recycled table cannot loop forever."""
    parent = {p: pp for p, pp, _c in (table or [])}
    seen, cur = set(), pid
    for _ in range(32):
        cur = parent.get(cur)
        if not cur or cur in seen:
            return False
        if cur in owners:
            return True
        seen.add(cur)
    return False


def roots(hits, table):
    """Of the processes matching a component, the ones that are NOT below
    another match.

    A server that forks a worker running the same file is one server. Three
    servers started separately are three - none is the other's child, so all
    three survive this and the 09-10 duplicate warning still fires. Blind stays
    blind: None in, None out."""
    if hits is None:
        return None
    pids = {h[0] for h in hits}
    return [h for h in hits
            if not _descendant_of(h[0], pids - {h[0]}, table)]


def free_ram_mib():
    """Free physical memory. ctypes on Windows, /proc/meminfo elsewhere; both
    are standard library, which is the whole point of this file."""
    try:
        if WINDOWS:
            class MS(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong),
                            ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong),
                            ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong),
                            ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong),
                            ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            m = MS()
            m.dwLength = ctypes.sizeof(MS)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
            return int(m.ullAvailPhys // (1024 * 1024))
        with open("/proc/meminfo") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except Exception:                                        # noqa: BLE001
        pass
    return None


def listening(port, host="127.0.0.1", timeout=0.4):
    """Is something answering on this port? The only honest way to know whether
    a component is up, because a pid file survives a crash and a port does not."""
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def node_health(port, timeout=4.0):
    """What the node says about ITSELF, or None if it will not say.

    ADDED 2026-09-17, AND THE REASON IS THE ENTRY. The node's row in `status`
    took its note from `node_python()[1]`, which reads the FILESYSTEM: if the
    keeper venv exists on disk, the note said `keeper venv: dense retrieval
    available`. On 2026-09-16 that line was printed, accurately, about a node
    that had been serving KEYWORD ONLY for three hours because numpy raised
    inside that very venv. The note was true about the venv and false about
    dense retrieval - this build's most-recorded defect shape, arriving at the
    one line an operator reads to decide whether the node is working.

    The node already answers the real question. `dense_backend` and
    `dense_coverage` were added to /api/health on 2026-09-15 for exactly this
    purpose: so that WHICH HALF ANSWERED stops being something anyone infers.
    This asks it instead of deducing it.

    None IS NOT "unhealthy". It means the port accepted a connection and the
    health endpoint did not answer in time - a fourth state, reported as itself
    rather than folded into one of the other three.

    THE TIMEOUT IS 4 s AND NOT 1. /api/health calls llm.status(), which probes
    two model servers at 2.5 s each behind a 3 s cache, so a cold call can
    legitimately take several seconds. A timeout shorter than the endpoint's own
    worst case would report a working node as silent, which is the failure this
    function exists to stop, reintroduced by its own impatience."""
    try:
        with urllib.request.urlopen(
                "http://127.0.0.1:%d/api/health" % port, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception:                                        # noqa: BLE001
        return None


def dense_note(h):
    """(note, warning) describing what the dense half is ACTUALLY doing.

    Returns (None, None) when there is no health payload, so the caller can say
    so rather than guess.

    FOUR STATES, AND THE THIRD IS THE ONE THAT GETS MISREAD. Dense loading is
    LAZY by design: a node that has answered no dense query has not failed, it
    has not been asked. `status` run seconds after `up` therefore reports NOT
    LOADED YET, and that is the correct answer rather than a fault. Reading it
    as one would be the 2026-09-15 mistake in mirror image - that day a pq.state
    was read 7.7 s after startup and a lazy loader's silence was taken for a
    verdict, twice."""
    if h is None:
        return None, None
    be, cov = h.get("dense_backend"), h.get("dense_coverage")
    if be:
        return "dense: %s over %s vectors" % (be, "{:,}".format(cov or 0)), None
    if h.get("dense_attempted"):
        return ("DENSE FAILED TO LOAD - keyword only, see the warning below",
                "the node tried to load dense retrieval and could not, so every "
                "hybrid query is running as keyword and saying so. The node "
                "reports: %s" % (h.get("dense_error") or "no cause recorded, "
                                 "which is itself worth reporting"))
    return ("dense not loaded yet - it is lazy, it loads on the first query",
            None)


# ---------------------------------------------------------------------------
# the components, in the order the RUNBOOK starts them

def components():
    kiwix = exe(CFG["paths.kiwix_serve"])
    # THE LIBRARY MOVED TO THE ARCHIVE ROOT, 2026-09-12. It used to live beside
    # the ZIMs because a book's key was derived from the path stored relative to
    # it; the paths are absolute now, the key is the basename, and the location
    # no longer decides anything. What it buys is that ONE server can span
    # shelves: 67 ZIMs instead of 25, and 686 GB that nothing could reach.
    # A stale 07-corpora-supplemental/library.xml left over from before will
    # still start and will still serve only 25 books, so it is named here rather
    # than left to be discovered.
    lib = CFG["paths.library"]
    stale_lib = p("07-corpora-supplemental", "library.xml")
    pmt = exe(CFG["paths.pmtiles"])
    serve = p("13-ark-node", "ark-api", "serve.py")
    lserver = os.path.join(LLAMA, exe("llama-server"))
    port = dict((k, CFG["ports." + k]) for k in
                ("archive", "tiles", "node", "primary", "crosscheck"))

    node_cmd = [node_python()[0], serve]
    # --port ONLY WHEN IT MOVED, so that with no ark.toml the command printed
    # here is the command the RUNBOOK prints, character for character.
    if port["node"] != 8090:
        node_cmd += ["--port", str(port["node"])]

    cs = [
        {"name": "archive", "port": port["archive"], "group": "search",
         "what": "kiwix-serve, the ZIM shelf and every citation link",
         # THE HINT NAMES THE ARCHIVE THAT MATCHES THIS MACHINE. A Linux
         # secondary told to extract a Windows zip is a instruction that cannot
         # be followed, and 8.10's promise is the Linux node's.
         "needs": [(kiwix, "extract 09-software/kiwix-tools/%s into bin/"
                    % ("kiwix-tools_win-x86_64-3.8.1.zip" if WINDOWS
                       else "kiwix-tools_linux-x86_64-3.8.2.tar.gz")),
                   (lib, "build it: python bin/kiwix-library.py"
                         + (" - and DELETE the stale %s, which serves only "
                            "the 25 books of the old single-shelf library"
                            % os.path.relpath(stale_lib, ROOT)
                            if os.path.exists(stale_lib) else ""))],
         "cmd": [kiwix, "--port", str(port["archive"]), "--library", lib]},

        {"name": "tiles", "port": port["tiles"], "group": "search",
         "what": "pmtiles serve, 843 GB of basemap and terrain",
         # --cors IS LOAD-BEARING. The tiles are on 8081 and the page is on
         # 8090, so every tile request is cross-origin; without it the map draws
         # nothing and the browser reports a security error rather than a
         # missing file. RUNBOOK says so; this script cannot forget it.
         "needs": [(pmt, "vendored as pmtiles.exe for Windows; on Linux "
                         "extract 09-software/pmtiles-cli/"
                         "go-pmtiles_1.31.2_Linux_x86_64.tar.gz")],
         "cmd": [pmt, "serve", CFG["paths.maps"], "--port", str(port["tiles"]),
                 "--cors=*"],
         # AN ARCHIVE WITH NO MAPS IS NOT MISSING A COMPONENT. The starter kit
         # has no 08-maps, and `status` listed tiles as MISSING after every
         # successful setup. Without the folder there is nothing to serve.
         "absent_ok": (CFG["paths.maps"],
                       "no maps in this archive (%s); the map pane needs them"
                       % os.path.basename(os.path.normpath(CFG["paths.maps"])))},

        {"name": "node", "port": port["node"], "group": "search",
         "what": "ark-api, the surface, search and the map pane",
         "needs": [(serve, "the node is missing from the archive")],
         "note": node_python()[1],
         "env": node_env(port),
         "cmd": node_cmd},
    ]
    llama_hint = ("extract 09-software/llamacpp-bin/%s into %s"
                  % ("llama-b10566-bin-win-cuda-12.4-x64.zip" if WINDOWS
                     else "llama-b10566-bin-macos-arm64.tar.gz or a Linux build",
                     LLAMA))
    if CFG["models.primary.enabled"]:
        cs.append(model_component("primary", port["primary"], lserver,
                                  llama_hint, "the answer pane"))
    if CFG["models.crosscheck.enabled"]:
        cs.append(model_component(
            "crosscheck", port["crosscheck"], lserver,
            "see primary" if CFG["models.primary.enabled"] else llama_hint,
            "the 9.4 second opinion"))
    return cs


def model_component(role, port, lserver, llama_hint, job):
    """One llama-server, from its settings.

    THE ARGUMENT ORDER IS THE ORDER THE RUNBOOK PRINTS, so a default install
    shows exactly the command a person would copy from the page:
    -m, -ngl, [-t], -c, --host, --port, -a, then the extra arguments.
    --host 127.0.0.1 IS NOT A SETTING. An uncited answer engine on the room's
    WiFi is the thing DECISIONS 2026-09-27 declined to build."""
    k = "models.%s." % role
    f = CFG[k + "file"]
    cmd = [lserver, "-m", f, "-ngl", str(CFG[k + "gpu_layers"])]
    if CFG[k + "threads"]:
        cmd += ["-t", str(CFG[k + "threads"])]
    cmd += ["-c", str(CFG[k + "context"]), "--host", "127.0.0.1",
            "--port", str(port), "-a", CFG[k + "alias"]]
    cmd += CFG[k + "extra_args"]
    try:
        where = os.path.relpath(os.path.dirname(f), ROOT).replace(os.sep, "/")
    except ValueError:                        # another drive, on Windows
        where = os.path.dirname(f)
    return {"name": role, "port": port, "group": "models",
            "what": "%s %s, %s" % (CFG[k + "name"],
                                   "on the GPU" if CFG[k + "gpu_layers"]
                                   else "in system RAM", job),
            "needs": [(lserver, llama_hint),
                      (f, "model missing from %s" % where)],
            "vram": CFG[k + "vram_mib"], "ram": CFG[k + "host_mib"],
            "cmd": cmd}


# The node reads these itself, with these same defaults. They are passed only so
# a port or prompt moved in ark.toml reaches the node; an environment variable
# the operator set is passed through untouched, because it already won above.
_NODE_DEFAULTS = {"ARK_KIWIX": "http://localhost:8080",
                  "ARK_PRIMARY_URL": "http://127.0.0.1:8091",
                  "ARK_CROSSCHECK_URL": "http://127.0.0.1:8092",
                  "ARK_ANSWER_PROMPT": "knowledge",
                  "ARK_CROSSCHECK_THINKING": "off",
                  "ARK_RERANK_DEPTH": "400",
                  "ARK_TILES_PORT": "8081"}


def not_installed(cfg=None, exists=os.path.exists):
    """The parts this kit leaves out, for the node to tell apart from faults.

    The 2026-10-03 new-account test: a correct starter install drew Map and the
    cross-check model red and counted three things as degraded. A model turned
    off in ark.toml, or a maps folder that is not there, was never installed;
    a model that is on and not answering still is a fault, and stays one."""
    cfg = CFG if cfg is None else cfg
    out = [r for r in ("primary", "crosscheck")
           if not cfg["models.%s.enabled" % r]]
    if not exists(cfg["paths.maps"]):
        out.append("map")
    return out


def node_env(port):
    want = {"ARK_KIWIX": "http://localhost:%d" % port["archive"],
            "ARK_PRIMARY_URL": "http://127.0.0.1:%d" % port["primary"],
            "ARK_CROSSCHECK_URL": "http://127.0.0.1:%d" % port["crosscheck"],
            "ARK_ANSWER_PROMPT": CFG["node.answer_prompt"],
            "ARK_CROSSCHECK_THINKING": CFG["node.crosscheck_thinking"],
            "ARK_RERANK_DEPTH": str(CFG["node.rerank_depth"]),
            "ARK_TILES_PORT": str(port["tiles"]),
            # NOT IN _NODE_DEFAULTS, so `commands` prints what it always did:
            # a node started by hand simply reports strictly, as before.
            "ARK_NOT_INSTALLED": ",".join(not_installed()),
            "ARK_PRIMARY_NAME": CFG["models.primary.name"],
            "ARK_CROSSCHECK_NAME": CFG["models.crosscheck.name"]}
    env = dict(os.environ)
    for var, val in want.items():
        env[var] = os.environ.get(var) or val
    env["ARK_ROOT"] = ROOT
    return env


def shown_env(c):
    """The environment a component gets that differs from what it would assume
    on its own, as lines a person can type before the command. Empty with no
    ark.toml, which keeps `commands` identical to the RUNBOOK."""
    env = c.get("env")
    if not env:
        return []
    fmt = "set %s=%s" if WINDOWS else "export %s=%s"
    return [fmt % (var, env[var]) for var in sorted(_NODE_DEFAULTS)
            if env.get(var) != _NODE_DEFAULTS[var]]


def shown(cmd):
    """The command as a human should type it, quoted for this shell."""
    if WINDOWS:
        return subprocess.list2cmdline(cmd)
    try:
        import shlex
        return shlex.join(cmd)
    except AttributeError:                                   # pragma: no cover
        return " ".join(cmd)


def missing(c):
    return [(path, fix) for path, fix in c["needs"] if not os.path.exists(path)]


def affordable(c, vram, ram, running_names):
    """Can this machine hold it? Returns (ok, reason).

    THE NUMBERS ARE FROM 2026-09-07 AND ARE NOT MARGINAL. Qwen wants 14,801 MiB
    of a 15,046 MiB card. Refusing here is the difference between a clear
    sentence and a CUDA out-of-memory forty seconds into a load - or worse, a
    silent partial offload that answers slowly and correctly and hides the
    reason."""
    need_v = c.get("vram", 0)
    need_r = c.get("ram", 0)
    # THE MEASURED FIGURE IS ALREADY THE WHOLE FOOTPRINT, so the test is
    # `less than`, not `less than plus a margin`. Qwen occupies 14,801 MiB of a
    # 15,046 MiB card: it fits, with 245 MiB spare, and an earlier version of
    # this function added 250 and would have DECLINED the configuration the node
    # has been running all week. A check that refuses the working case is worse
    # than no check - it is the CORPUS.md audit script again. The margin is now
    # a warning about how tight it is, and nothing more.
    if need_v and vram is not None and vram < need_v:
        return False, ("needs %s MiB of VRAM and %s MiB is free%s"
                       % ("{:,}".format(need_v), "{:,}".format(vram),
                          "; the primary is already holding it"
                          if "primary" in running_names else ""))
    if need_r and ram is not None and ram < need_r:
        return False, ("needs %s MiB of system RAM and %s MiB is free"
                       % ("{:,}".format(need_r), "{:,}".format(ram)))
    if need_v and vram is not None and vram - need_v < MARGIN_MIB:
        return True, ("fits with %s MiB to spare, which is the whole margin"
                      % "{:,}".format(vram - need_v))
    if need_v and vram is None:
        return True, "no nvidia-smi, so VRAM was not checked"
    return True, ""


# ---------------------------------------------------------------------------
# state, kept outside the archive

def read_state():
    try:
        with open(STATE) as fh:
            return json.load(fh)
    except Exception:                                        # noqa: BLE001
        return {}


def write_state(d):
    try:
        with open(STATE, "w") as fh:
            json.dump(d, fh, indent=1)
    except Exception as e:                                   # noqa: BLE001
        print("  (could not record pids in %s: %s)" % (STATE, e))


# ---------------------------------------------------------------------------
# the verbs

# HOW LONG A MODEL'S VRAM READING IS NOT YET THE TRUTH. llama-server binds its
# port when the weights are loaded, and the driver accounts for the allocation
# later: on 2026-09-07 `up` read 15,576 MiB free with both models listening and
# a `status` moments later read 472. On 2026-10-01 the first starter run printed
# the eviction warning below about a Qwen it had started seconds earlier, and
# then answered both questions normally. A stranger reads `!!` as a failure.
SETTLE_S = 120


def evicted(cs, up_names, vram, fresh=()):
    """Components whose weights cannot be on the card, given the free VRAM.
    `fresh` names components started less than SETTLE_S ago, whose VRAM the
    driver has not counted yet: they are not judged.

    THE SIGNATURE IS COUNTERINTUITIVE AND THAT IS WHY IT NEEDS SAYING. On
    2026-09-10 this script printed `VRAM free 16,050 MiB` with both models UP,
    which reads as a node with headroom and WAS the primary paged off the GPU by
    Windows under memory pressure. The port kept answering, `status` kept saying
    UP, and the next answer timed out at 240s. A probe pulled the weights back
    and nvidia-smi then read 15,437 MiB used.

    The inference is arithmetic rather than a guess: if a component is running
    and claims 14,801 MiB of VRAM, then 14,801 MiB CANNOT also be free. When it
    is, the weights are somewhere else."""
    if vram is None:
        return []
    out = []
    for c in cs:
        need = c.get("vram") or 0
        if c["name"] in up_names and c["name"] not in fresh and need > 1000 \
                and vram >= need:
            out.append((c["name"], need))
    return out


def do_status(args):
    cs = components()
    vram, ram = free_vram_mib(), free_ram_mib()
    table = process_table()
    print("\nark node   %s" % ROOT)
    print("           VRAM free %s   RAM free %s\n"
          % ("unknown" if vram is None else "{:,} MiB".format(vram),
             "unknown" if ram is None else "{:,} MiB".format(ram)))
    print("  %-11s %-6s %-9s %s" % ("COMPONENT", "PORT", "STATE", "NOTE"))
    up, warn = [], []
    for c in cs:
        live = listening(c["port"])
        if live:
            up.append(c["name"])
            state, note = "UP", c.get("note") or c["what"]
            # THE INTERPRETER THAT IS RUNNING, NOT THE ONE THIS SCRIPT WOULD
            # PICK. `node_python()` answers "what would I choose", and the note
            # built from it was printed about a process this script may not have
            # started. On 2026-09-10 the node had run NINE HOURS under system
            # Python while this line said `keeper venv`. Same lesson as the
            # 09-07 note about a launcher picking silently, from the other side:
            # reporting an intention as an observation.
            if c["name"] == "node":
                # WHAT THE NODE SAYS, ASKED OF THE NODE. This replaces
                # `node_python()[1]`, which described a directory on disk and
                # was printed in a column an operator reads as a description of
                # retrieval. See node_health().
                dn, dwhy = dense_note(node_health(c["port"]))
                if dn:
                    note = dn
                    if dwhy:
                        warn.append(dwhy)
                else:
                    note = ("port answers but /api/health did not - this line "
                            "cannot say what dense is doing")
                # THE ROOT, NOT THE FIRST MATCH. The node spawns a child under
                # the base interpreter; reading the first row of the table read
                # the CHILD's interpreter and accused the healthy node of
                # running under the wrong Python. The root is a proxy for "the
                # process that owns the port" and is exactly right whenever
                # there is one root - and when there is more than one, the
                # duplicate warning below is the thing to read.
                inst = roots(instances(c, table) or [], table)
                if inst:
                    cmd = inst[0][2]
                    want = node_python()[0].replace("\\", "/").lower()
                    got = cmd.replace("\\", "/").lower()
                    if want.split("/")[-3:] and want not in got:
                        # THE STATE, NOT THE NOTE - CHANGED 2026-09-17. The note
                        # now carries an OBSERVATION of dense retrieval, and the
                        # interpreter was only ever a proxy for that same
                        # question. When a proxy and a measurement disagree the
                        # measurement wins, so the mismatch keeps its flag and
                        # its warning below and stops overwriting the answer.
                        state = "UP*"
        elif c.get("absent_ok") and not os.path.exists(c["absent_ok"][0]):
            state, note = "none", c["absent_ok"][1]
        else:
            miss = missing(c)
            if miss:
                state = "MISSING"
                note = "%s not found - %s" % (os.path.basename(miss[0][0]),
                                              miss[0][1])
            else:
                ok, why = affordable(c, vram, ram, up)
                # THE NOTE SHOWS WHETHER IT IS UP OR DOWN, because "what you
                # will get if you start this" matters as much as "what you got".
                state, note = ("down", why or c.get("note") or c["what"]) \
                    if ok else ("WILL NOT FIT", why)
        print("  %-11s %-6s %-9s %s" % (c["name"], c["port"], state, note[:78]))

    # DUPLICATES. A port check sees one server; three processes can be alive.
    if table is None:
        warn.append("could not read the process list, so duplicate servers "
                    "would not be seen. Ports only.")
    else:
        for c in cs:
            inst = roots(instances(c, table) or [], table)
            if len(inst) > 1:
                warn.append(
                    "%d processes are running `%s` (pids %s). One answers on "
                    "%s; the others hold memory, serve nothing, and `down` "
                    "cannot see them. A single stray node was holding 24 GB on "
                    "2026-09-10. Stop them with: ark.py down --strays"
                    % (len(inst), _fingerprint(c),
                       ", ".join(str(i[0]) for i in inst), c["port"]))
        node = [c for c in cs if c["name"] == "node"][0]
        inst = roots(instances(node, table) or [], table)
        want = node_python()[0]
        for pid, _ppid, cmd in inst:
            if want.replace("\\", "/").lower() not in cmd.replace("\\", "/").lower():
                warn.append(
                    "the node (pid %s) is NOT running the interpreter this "
                    "script would choose. Running: %s. Would choose: %s. If the "
                    "running one lacks the embedding stack the node is keyword "
                    "only, whatever the note above says."
                    % (pid, cmd.strip()[:90], want))

    now = time.time()
    fresh = dict((n, now - s.get("started", 0)) for n, s in read_state().items()
                 if isinstance(s, dict) and now - s.get("started", 0) < SETTLE_S)
    settling = [n for n in fresh if n in up and
                any(c["name"] == n and (c.get("vram") or 0) > 1000 for c in cs)]
    if settling:
        print("\n  %s started %s ago; the driver has not counted its VRAM yet,\n"
              "  so the free figure above is not the truth. `status` in a minute is."
              % (", ".join(settling), ", ".join("%d s" % fresh[n] for n in settling)))
    for name, need in evicted(cs, up, vram, fresh):
        warn.append(
            "%s is UP and %s MiB of VRAM is free - but it needs %s MiB, so its "
            "weights are NOT on the card. Windows evicts them under memory "
            "pressure and the port keeps answering. The next answer will be "
            "slow or time out. Ask it anything to pull it back, or restart the "
            "models. HIGH FREE VRAM WITH A MODEL UP IS A FAULT, NOT HEADROOM."
            % (name, "{:,}".format(vram), "{:,}".format(need)))

    for w in warn:
        print("\n  !! " + textwrap.fill(w, 74, subsequent_indent="     "))
    if warn:
        print("")

    _n = CFG["ports.node"]
    print("\n  http://localhost:%d/   the surface"
          "\n  http://localhost:%d/home/   every service, and whether it is up"
          "\n  http://localhost:%d/map/   the map\n" % (_n, _n, _n))
    return 0


def do_commands(args):
    print("\nEvery command this script would run. Copy any of them; nothing\n"
          "here needs this file. RUNBOOK.md explains what each one is for.\n")
    for c in components():
        print("  # %s - %s" % (c["name"], c["what"]))
        if c.get("note"):
            print("  # %s" % c["note"])
        for line in shown_env(c):
            print("  %s" % line)
        print("  %s\n" % shown(c["cmd"]))
    return 0


def do_up(args):
    if settings_problems():
        print("  Nothing started. Fix the settings above, or run with no ark.toml\n"
              "  to use the defaults: `python bin/ark.py config` shows every value.\n")
        return 2
    cs = [c for c in components()
          if not (args.no_models and c["group"] == "models")]
    if args.only:
        cs = [c for c in cs if c["name"] in args.only]
    vram, ram = free_vram_mib(), free_ram_mib()
    state = read_state()
    started, skipped = 0, 0
    print("")
    for c in cs:
        if listening(c["port"]):
            print("  %-11s already up on %s" % (c["name"], c["port"]))
            continue
        if c.get("absent_ok") and not os.path.exists(c["absent_ok"][0]):
            print("  %-11s not started - %s" % (c["name"], c["absent_ok"][1]))
            continue
        miss = missing(c)
        if miss:
            print("  %-11s CANNOT START - %s is not there"
                  % (c["name"], miss[0][0]))
            print("  %-11s %s" % ("", miss[0][1]))
            skipped += 1
            continue
        running = [x["name"] for x in cs if listening(x["port"])]
        ok, why = affordable(c, vram, ram, running)
        if not ok and not args.force:
            print("  %-11s DECLINED - %s" % (c["name"], why))
            print("  %-11s run with --force to start it anyway" % "")
            skipped += 1
            continue
        if why:
            print("  %-11s note: %s" % (c["name"], why))
        if c.get("note"):
            print("  %-11s %s" % ("", c["note"]))
        # EACH SERVER GETS ITS OWN CONSOLE ON WINDOWS, because the manual
        # teaches the operator to read those windows and because a server whose
        # output goes nowhere is a server that fails silently.
        for line in shown_env(c):
            print("  %-11s with: %s" % (c["name"], line))
        print("  %-11s starting: %s" % (c["name"], shown(c["cmd"])))
        try:
            if WINDOWS:
                proc = subprocess.Popen(
                    c["cmd"], creationflags=subprocess.CREATE_NEW_CONSOLE,
                    env=c.get("env"))
            else:
                log = os.path.join(tempfile.gettempdir(),
                                   "ark-%s.log" % c["name"])
                fh = open(log, "ab")
                proc = subprocess.Popen(c["cmd"], stdout=fh, stderr=fh,
                                        start_new_session=True,
                                        env=c.get("env"))
                print("  %-11s log: %s" % ("", log))
        except Exception as e:                               # noqa: BLE001
            print("  %-11s FAILED to start: %s" % (c["name"], e))
            skipped += 1
            continue
        state[c["name"]] = {"pid": proc.pid, "port": c["port"],
                            "started": time.time()}
        started += 1
        # The model servers read 15 to 17 GB off disk before they listen, so a
        # wait here is the difference between a useful report and a false one.
        wait = 90 if c["group"] == "models" else 20
        for _ in range(wait * 2):
            if listening(c["port"]):
                break
            time.sleep(0.5)
        print("  %-11s %s" % ("", "listening on %s" % c["port"]
                              if listening(c["port"]) else
                              "started, not answering yet - watch its window"))
    write_state(state)
    print("")
    do_status(args)
    # A VRAM READING TAKEN THE INSTANT A PORT ANSWERS IS NOT THE TRUTH.
    # Measured 2026-09-07: `up` reported 15,576 MiB free with both models
    # already listening, and a `status` moments later read 472. llama-server
    # binds its port as soon as the model is loaded, but the driver has not
    # finished accounting for the allocation, so the number above is a snapshot
    # of a machine mid-breath. Saying so is cheaper than sleeping for a guessed
    # interval, and far cheaper than letting the tool built to prevent confident
    # wrong output produce some.
    # (A model started in this run is named by `status` itself now, which says
    # its VRAM is not counted yet; see SETTLE_S.)
    if skipped:
        print("  %d component(s) did not start. Nothing above is fatal to the\n"
              "  rest: search works without the models, and the surface says so.\n"
              % skipped)
    return 0


def do_down(args):
    """Stop what THIS script started, and only that.

    BY RECORDED PID AND ONLY IF THE PORT IS STILL ANSWERING. A pid file outlives
    the process it names and operating systems reuse pids, so terminating on the
    strength of a stale file is how a launcher kills something it never started.
    Anything not started from here is left alone and said so."""
    state = read_state()
    cs = components()
    table = process_table()
    if not state and not getattr(args, "strays", False):
        print("\n  nothing recorded in %s. Servers started by hand are not\n"
              "  this script's to stop - close their windows.\n" % STATE)
        _report_strays(cs, table, set())
        return 0
    print("")
    for name, rec in sorted(state.items()):
        if not listening(rec["port"]):
            print("  %-11s not answering on %s, nothing to stop"
                  % (name, rec["port"]))
            continue
        try:
            if WINDOWS:
                subprocess.run(["taskkill", "/PID", str(rec["pid"]), "/T", "/F"],
                               capture_output=True)
            else:
                os.kill(rec["pid"], 15)
            print("  %-11s stopped (pid %s)" % (name, rec["pid"]))
        except Exception as e:                               # noqa: BLE001
            print("  %-11s could not stop pid %s: %s" % (name, rec["pid"], e))
    stopped = {rec["pid"] for rec in state.values() if isinstance(rec, dict)
               and "pid" in rec}
    try:
        os.remove(STATE)
    except OSError:
        pass
    if getattr(args, "strays", False):
        _stop_strays(cs, table, stopped)
    else:
        _report_strays(cs, table, stopped)
    print("")
    return 0


def _strays(cs, table, stopped):
    """Processes that look like a component, are not ones we just stopped, and
    are not somebody else's child.

    THE CHILD OF A RUNNING NODE IS NOT A STRAY. Until 2026-09-12 this listed
    every match, and the node's own base-interpreter child matched - so
    `down --strays` terminated part of a healthy running node every time it was
    used, which is precisely the harm the report-don't-kill rule below exists to
    avoid. A descendant of a pid we just stopped is skipped for the same reason:
    it is being torn down with its parent, not left behind. An ORPHAN whose
    parent is already gone from the table has no parent to hide behind and is
    still named, which is right - that is the 24 GB case."""
    out = []
    if table is None:
        return None
    owners = set(stopped) | {os.getpid()}
    for c in cs:
        for pid, _ppid, cmd in roots(instances(c, table) or [], table):
            if pid in owners or _descendant_of(pid, owners, table):
                continue
            out.append((c["name"], pid, cmd))
    return out


def _report_strays(cs, table, stopped):
    """Name them, never kill them. THE RULE IS THE SAME ONE do_down ALREADY HAS.

    `down` stops what this script recorded, on the reasoning that terminating
    something it never started is how a launcher kills a process someone else is
    using. That reasoning is still right, and it left the gap that mattered: a
    second `serve.py` which never got the port survives `down`, answers nothing,
    and holds gigabytes. So the gap is closed by TELLING the operator, and
    `--strays` is the explicit ask that closes it for them."""
    found = _strays(cs, table, stopped)
    if found is None:
        print("  (could not read the process list, so strays were not checked)")
        return
    if not found:
        return
    print("\n  %d process(es) still alive that look like node components and\n"
          "  were NOT started by this script:" % len(found))
    for name, pid, cmd in found:
        print("    %-11s pid %-7s %s" % (name, pid, cmd.strip()[:74]))
    print("\n  They answer nothing and hold memory - one was holding 24 GB on\n"
          "  2026-09-10. Stop them with:  ark.py down --strays")


def _stop_strays(cs, table, stopped):
    found = _strays(cs, table, stopped)
    if found is None:
        print("  (could not read the process list, so no strays were stopped)")
        return
    if not found:
        print("  no strays")
        return
    print("")
    for name, pid, cmd in found:
        try:
            if WINDOWS:
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                               capture_output=True)
            else:
                os.kill(pid, 15)
            print("  %-11s stray stopped (pid %s)" % (name, pid))
        except Exception as e:                               # noqa: BLE001
            print("  %-11s could not stop stray pid %s: %s" % (name, pid, e))


def selftest_settings(check):
    """The settings layer: precedence, refusal, fallback, and the example.

    Run against a temporary root, never the real one, so an ark.toml the
    operator has written cannot make these pass or fail."""
    import shutil as _sh
    tmp = tempfile.mkdtemp(prefix="ark-settings-")
    try:
        base = {"ARK_ROOT": tmp}
        s = load_settings(environ=base)
        check("no ark.toml: every value is a default",
              s.file is None and not s.problems and
              all(src == "default" for k, src in s.sources.items()
                  if k != "paths.root"))
        check("no ark.toml: the models are the measured pair",
              s["models.primary.gpu_layers"] == 64 and
              s["models.crosscheck.host_mib"] == 20629 and
              s["ports.node"] == 8090)
        try:
            import tomllib  # noqa: F401
            have_toml = True
        except ImportError:
            try:
                import tomli  # noqa: F401
                have_toml = True
            except ImportError:
                have_toml = False
        f = os.path.join(tmp, "ark.toml")

        def write(text):
            with open(f, "w", encoding="utf-8") as fh:
                fh.write(text)

        if not have_toml:
            check("TOML cases skipped: this Python has no tomllib", True,
                  platform.python_version())
            write("[ports]\nnode = 9000\n")
            s = load_settings(environ=base)
            check("a file this Python cannot read is a problem, not a guess",
                  s.problems and s["ports.node"] == 8090)
            return
        write("[ports]\nnode = 9000\n[paths]\nllama = '/from/file'\n"
              "[models.primary]\ngpu_layers = 20\n")
        s = load_settings(environ=base)
        check("the file sets what it names",
              s["ports.node"] == 9000 and s["models.primary.gpu_layers"] == 20
              and s.sources["ports.node"] == "ark.toml")
        s = load_settings(environ=dict(base, ARK_LLAMA="/from/env"))
        check("an environment variable beats the file",
              s["paths.llama"] == "/from/env"
              and s.sources["paths.llama"] == "env ARK_LLAMA")
        write("[models.primary]\ngpu_layer = 20\n")
        s = load_settings(environ=base)
        check("an unknown key is refused by name",
              any("models.primary.gpu_layer'" in pr for pr in s.problems)
              and s["models.primary.gpu_layers"] == 64)
        write("[models.primary]\ncontext = 'big'\n[ports]\ntiles = 70000\n")
        s = load_settings(environ=base)
        check("a wrong value falls back and says so",
              len(s.problems) == 2 and s["models.primary.context"] == 8192
              and s["ports.tiles"] == 8081)
        write("[paths]\nmaps = 'elsewhere/maps'\n")
        s = load_settings(environ=base)
        check("a relative path is relative to the root",
              s["paths.maps"] == os.path.join(tmp, "elsewhere", "maps"))
        # THE EXAMPLE, UNCOMMENTED IN FULL, MUST MEAN EXACTLY THE DEFAULTS.
        # If it did not, a user who uncommented one section to edit one line
        # would silently change the rest.
        lines = []
        for ln in example_toml().splitlines():
            if ln.startswith("# ") and " = " in ln and not ln.startswith("#  "):
                lines.append(ln[2:])
            elif ln.startswith("["):
                lines.append(ln)
        write("\n".join(lines) + "\n")
        s = load_settings(environ=base)
        d = load_settings(environ=dict(base, ARK_CONFIG=os.path.join(tmp, "none.toml")))
        same = [k for k in s.values if k != "paths.root"
                and s.values[k] != d.values[k]]
        check("the generated example, uncommented, is the defaults",
              not s.problems and not same,
              "; ".join(s.problems + same)[:120])
    finally:
        _sh.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# FETCH: THE CATALOG, DOWNLOADED AND CHECKED
# ---------------------------------------------------------------------------
#
# `ark.py fetch` turns catalog.csv into files on disk, and it is built around one
# rule: A FILE THAT DOES NOT MATCH ITS CHECKSUM IS NOT THE FILE. Everything this
# node was tested with is pinned by sha256 in the catalog, so a download that
# differs - a truncated transfer, a mirror serving a different edition, a model
# re-uploaded under the same name - is refused and kept aside as .bad, never
# moved into place. The same rule runs the other way: a file already at the
# destination with a different size is NOT overwritten. It may be someone's
# newer copy; deciding that is a person's job.
#
# Standard library only, like the rest of this file. Downloads resume from a
# .part file with an HTTP Range request, retry three times, and print progress.
#
# THE KIWIX PROBLEM, HANDLED OUT LOUD. download.kiwix.org keeps recent editions
# and drops older ones, so a catalog pinned to zimgit-water_en_2024-08.zim will
# one day get a 404. When it does, fetch reads the folder listing, names the
# newer edition, and stops. With --accept-newer it downloads that edition,
# checks it against the .sha256 Kiwix publishes beside it, saves it under ITS
# OWN name, and appends the substitution to fetch-substitutions.csv at the root.
# A newer edition is a different file, and the record says so.
#
# HUGGING FACE REPOSITORIES are fetched file by file through the public API at
# the catalog's pinned revision, and each file is checked against the sha256
# (large files) or the git blob id (small files) the API reports for that
# revision. By default the ONNX export, image folders and the pytorch_model.bin
# a safetensors file duplicates are skipped: for BGE-M3 that is 2.3 GB of
# what the node reads instead of 6.9 GB. --all-files takes the whole repository.
# An ONNX folder is an EXPORT only beside a .safetensors file: in a repository
# that ships nothing else (Kokoro-82M-v1.0-ONNX) it is the model, and is kept.
# HF_ENDPOINT, the variable Hugging Face's own tools read, moves the host.

FETCH_CHUNK = 1 << 20
FETCH_RETRIES = 3
FETCH_UA = "ark-node-fetch/1 (+https://github.com/ArkOfNoahledge)"
HF_DEFAULT = "https://huggingface.co"
# Skipped from a Hugging Face repository unless --all-files. A folder prefix, or
# a file name. The ONNX export and *.bin / *.pt are skipped only when a
# .safetensors file carries the same weights; see hf_keep_names().
HF_SKIP_PREFIX = ("imgs/", ".gitattributes")
HF_SKIP_EXT = (".jpg", ".jpeg", ".png", ".gif")
HF_EXPORT_PREFIX = ("onnx/",)


def human(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1000 or unit == "TB":
            return ("%d %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1000.0


def load_catalog(path):
    if not os.path.isfile(path):
        raise FileNotFoundError("no catalog at %s (paths.catalog)" % path)
    with open(path, encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def fetch_dest(row, root):
    parts = row["shelf"].split("/") + row["file"].rstrip("/").split("/")
    return os.path.join(root, *parts)


def _sha256(path, quiet=True):
    import hashlib
    h = hashlib.sha256()
    total = os.path.getsize(path)
    done, last = 0, time.time()
    with open(path, "rb") as fh:
        while True:
            b = fh.read(FETCH_CHUNK * 8)
            if not b:
                break
            h.update(b)
            done += len(b)
            if not quiet and time.time() - last > 2:
                last = time.time()
                sys.stdout.write("\r    checking %s of %s   " % (human(done), human(total)))
                sys.stdout.flush()
    if not quiet and total > FETCH_CHUNK * 64:
        sys.stdout.write("\r" + " " * 50 + "\r")
    return h.hexdigest()


def _git_blob_sha1(path):
    """The id git gives a small file: sha1 of 'blob <size>\\0' + content. It is
    what the Hugging Face API reports for files that are not in LFS."""
    import hashlib
    h = hashlib.sha1()
    h.update(b"blob %d\0" % os.path.getsize(path))
    with open(path, "rb") as fh:
        for b in iter(lambda: fh.read(FETCH_CHUNK), b""):
            h.update(b)
    return h.hexdigest()


class Changed(Exception):
    """The host states a length for the whole file that is not the catalog's:
    the file changed upstream. Raised before a byte is written."""
    def __init__(self, total):
        Exception.__init__(self, total)
        self.total = total


def _whole(r, start):
    """The whole file's length as the server states it, or None. Content-Range
    ('bytes 0-99/188', or 'bytes */188' on a 416) first, then Content-Length."""
    m = re.search(r"/(\d+)\s*$", r.headers.get("Content-Range") or "")
    if m:
        return int(m.group(1))
    cl = r.headers.get("Content-Length") or ""
    if cl.isdigit() and r.getcode() in (200, 206):
        return int(cl) + (start if r.getcode() == 206 else 0)
    return None


# CERTIFICATES, 2026-10-03. On a fresh Windows laptop every download failed with
# CERTIFICATE_VERIFY_FAILED, "unable to get local issuer certificate", while Edge
# and PowerShell opened the same sites. The servers' certificates were ordinary
# Let's Encrypt ones (issuer YR1). Windows fetches a root certificate the first
# time its own components need it, and Python reads only the roots already in
# the store, so a new machine can lack one that every browser has. Python, given
# the Mozilla list that pip carries (certifi), verified the same site at once.
# So downloads trust both: the Windows store, which also holds any certificate a
# company network installs, and that list. Verification is never turned off.
_SSL = []


def ssl_context():
    if not _SSL:
        import ssl
        ctx = ssl.create_default_context()
        for mod in ("certifi", "pip._vendor.certifi"):
            try:
                ctx.load_verify_locations(__import__(mod, fromlist=["where"]).where())
                break
            except Exception:                                # noqa: BLE001
                continue
        _SSL.append(ctx)
    return _SSL[0]


class CertificateRefused(OSError):
    """A server's certificate could not be verified. Retrying cannot help."""


def _cert_refused(e):
    import ssl
    r = getattr(e, "reason", None)
    return isinstance(e, ssl.SSLCertVerificationError) or \
        isinstance(r, ssl.SSLCertVerificationError)


CERT_HELP = ("the server's certificate could not be verified (%s). Retrying will "
             "not help. If this computer's network inspects encrypted traffic (some "
             "companies, schools and antivirus products do), point Python at that "
             "network's certificate: set SSL_CERT_FILE to its file and run again. "
             "See the README, Troubleshooting")


def _open(url, start=0, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": FETCH_UA})
    # A gated Hugging Face repository needs a token. It goes to the Hugging Face
    # host only, and not on to the storage a download redirects to.
    tok = os.environ.get("HF_TOKEN")
    if tok and urllib.parse.urlparse(url).netloc == urllib.parse.urlparse(hf_endpoint()).netloc:
        req.add_unredirected_header("Authorization", "Bearer " + tok)
    if start:
        req.add_header("Range", "bytes=%d-" % start)
    try:
        return urllib.request.urlopen(req, timeout=timeout, context=ssl_context())
    except urllib.error.HTTPError:
        raise
    except (OSError, ValueError) as e:
        if _cert_refused(e):
            raise CertificateRefused(CERT_HELP % urllib.parse.urlparse(url).netloc)
        raise


def download(url, part, size=None, quiet=False):
    """Fill `part` from `url`, resuming what is already there. Returns None, or
    raises urllib.error.HTTPError / OSError after FETCH_RETRIES tries."""
    for attempt in range(1, FETCH_RETRIES + 1):
        start = os.path.getsize(part) if os.path.exists(part) else 0
        if size and start == size:
            return
        if size and start > size:
            start = 0                      # longer than the file: not a resume, start over
        try:
            try:
                r = _open(url, start)
            except urllib.error.HTTPError as e:
                # 416: the Range starts at or past the end of the host's file.
                # Real hosts (Hugging Face's CDN) say how long the file is.
                whole = _whole(e, start) if (e.code == 416 and start) else None
                if whole is None:
                    raise
                if size and whole != size:
                    raise Changed(whole)
                return
            with r:
                code = r.getcode()
                whole = _whole(r, start)
                if size and whole is not None and whole != size:
                    raise Changed(whole)
                # A server that ignores Range answers 200 with the whole file:
                # start over rather than append a second copy to the first.
                mode = "ab" if (start and code == 206) else "wb"
                done = start if mode == "ab" else 0
                total = size or (done + int(r.headers.get("Content-Length") or 0))
                t0, last, since = time.time(), time.time(), done
                with open(part, mode) as out:
                    for b in iter(lambda: r.read(FETCH_CHUNK), b""):
                        out.write(b)
                        done += len(b)
                        if not quiet and time.time() - last > 2:
                            rate = (done - since) / max(time.time() - t0, 1e-6)
                            pct = (" %5.1f%%" % (100.0 * done / total)) if total else ""
                            sys.stdout.write("\r    %s of %s%s  %s/s   "
                                             % (human(done), human(total), pct, human(rate)))
                            sys.stdout.flush()
                            last = time.time()
                if not quiet:
                    sys.stdout.write("\r" + " " * 60 + "\r")
            if size and os.path.getsize(part) < size and attempt < FETCH_RETRIES:
                continue
            return
        except (urllib.error.HTTPError, CertificateRefused):
            raise
        except (OSError, ValueError) as e:
            if attempt == FETCH_RETRIES:
                raise
            if not quiet:
                print("    interrupted (%s), resuming, try %d of %d"
                      % (e, attempt + 1, FETCH_RETRIES))
            time.sleep(2 * attempt)


def fetch_one_file(url, dest, sha256=None, size=None, verify=False, quiet=False,
                   check=None):
    """One file into place. Returns (state, detail). `check` is an optional
    (kind, value) pair for files whose id is not a sha256 (git blob ids)."""
    size = int(size) if str(size or "").isdigit() else None
    want = ("sha256", sha256) if sha256 else (check or (None, None))

    def matches(path):
        kind, value = want
        if not kind:
            return None
        got = _sha256(path, quiet) if kind == "sha256" else _git_blob_sha1(path)
        return got == value

    if os.path.exists(dest):
        if size is not None and os.path.getsize(dest) != size:
            return ("CONFLICT", "already there at %s, the catalog says %s - left "
                    "untouched" % (human(os.path.getsize(dest)), human(size)))
        if verify:
            ok = matches(dest)
            if ok is False:
                return ("MISMATCH", "the file already there is not the catalog's "
                        "(checksum differs) - left untouched")
            return ("present", "checked" if ok else "present, no checksum to check")
        return ("present", "size matches" if size is not None else "present")

    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    part = dest + ".part"
    try:
        download(url, part, size, quiet)
    except Changed as c:
        # "Changed upstream" was the first wording, and for `params` it was
        # wrong: upstream had not changed, the archive's copy had CRLF line
        # endings. The size says the two differ, not which one moved.
        return ("CHANGED", "the host serves %s bytes, the catalog says %s: the host's "
                "file is not the catalog's%s. Nothing was downloaded or moved into place"
                % (format(c.total, ","), format(size, ","),
                   " (the catalog follows the moving revision 'main')"
                   if "/resolve/main/" in url else ""))
    if size is not None and os.path.getsize(part) != size:
        return ("FAILED", "downloaded %s, expected %s - kept as .part, run again "
                "to resume" % (human(os.path.getsize(part)), human(size)))
    ok = matches(part)
    if ok is False:
        bad = dest + ".bad"
        os.replace(part, bad)
        return ("MISMATCH", "downloaded, and the checksum is NOT the catalog's. "
                "Kept aside as %s, not moved into place" % os.path.basename(bad))
    os.replace(part, dest)
    return ("fetched", "checked" if ok else "UNCHECKED - the catalog has no checksum")


def hf_endpoint():
    return (os.environ.get("HF_ENDPOINT") or HF_DEFAULT).rstrip("/")


def hf_repo_id(url):
    """'BAAI/bge-m3' from https://huggingface.co/BAAI/bge-m3 (any host)."""
    path = urllib.parse.urlparse(url).path.strip("/").split("/")
    return "/".join(path[:2])


def hf_plan(repo, rev, all_files=False):
    """[(path, size, kind, id)] for one repository at one revision, less what
    the node does not read unless all_files."""
    api = "%s/api/models/%s/revision/%s?blobs=true" % (hf_endpoint(), repo, rev)
    with _open(api) as r:
        info = json.loads(r.read().decode("utf-8"))
    return hf_select(hf_siblings(info), all_files=all_files)[:2]


def hf_siblings(info):
    """[(path, size, kind, id)] from an API revision reply: kind 'sha256' for
    LFS files, 'git' (a git blob id) for the rest."""
    files = []
    for s in info.get("siblings", []):
        name = s["rfilename"]
        lfs = s.get("lfs") or {}
        if lfs.get("sha256"):
            files.append((name, lfs.get("size") or s.get("size"), "sha256", lfs["sha256"]))
        else:
            files.append((name, s.get("size"), "git", s.get("blobId") or ""))
    return files


def hf_select(files, include=None, all_files=False):
    """(keep, skipped, gone): what a fetch of one repository row takes, from
    [(path, size, kind, id)] at its revision. ONE RULE, used by the fetch and by
    `--pin`, which records what it adds up to (pins.csv fetch_files and
    fetch_bytes, the catalog's `bytes`), so the size a person is shown is the
    size of what arrives. `include`, when set, is the list; a name the commit
    does not have is `gone`."""
    include = [x for x in (include or []) if x]
    if include and not all_files:
        want = set(include)
        have = set(f[0] for f in files)
        return ([f for f in files if f[0] in want], [f for f in files if f[0] not in want],
                [n for n in include if n not in have])
    if all_files:
        return list(files), [], []
    kept = hf_keep_names(f[0] for f in files)
    return ([f for f in files if f[0] in kept], [f for f in files if f[0] not in kept], [])


def hf_keep_names(names):
    """The names, of a repository's files, that the node reads: less images,
    and, when a .safetensors carries the weights, the ONNX export and the
    .bin/.pt copies of them."""
    names = list(names)
    st = any(n.endswith(".safetensors") for n in names)
    keep = set()
    for name in names:
        low = name.lower()
        if not (name.startswith(HF_SKIP_PREFIX) or low.endswith(HF_SKIP_EXT)
                or (st and (low.endswith((".bin", ".pt"))
                            or name.startswith(HF_EXPORT_PREFIX)))):
            keep.add(name)
    return keep


_KIWIX_EDITION = re.compile(r"^(?P<stem>.+)_(?P<date>\d{4}-\d{2})(?P<rev>[a-z]?)\.zim$")


def newer_zim(url):
    """For a .zim URL that 404s: the newest edition of the same book in the same
    folder, as (url, filename), or None. Reads the plain folder listing Kiwix and
    its mirrors serve."""
    base = url.rsplit("/", 1)
    m = _KIWIX_EDITION.match(base[-1])
    if len(base) != 2 or not m:
        return None
    try:
        with _open(base[0] + "/") as r:
            listing = r.read().decode("utf-8", "replace")
    except (OSError, urllib.error.HTTPError):
        return None
    best = None
    for name in set(re.findall(r'href="([^"/?]+\.zim)"', listing)):
        n = _KIWIX_EDITION.match(name)
        if n and n.group("stem") == m.group("stem") \
                and (n.group("date"), n.group("rev")) > (m.group("date"), m.group("rev")):
            if best is None or (n.group("date"), n.group("rev")) > best[0]:
                best = ((n.group("date"), n.group("rev")), name)
    return (base[0] + "/" + best[1], best[1]) if best else None


def record_substitution(root, row, new_file, sha):
    path = os.path.join(root, "fetch-substitutions.csv")
    new = not os.path.exists(path)
    with open(path, "a", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, lineterminator="\n")
        if new:
            w.writerow(["date", "id", "catalog_file", "fetched_file", "sha256", "why"])
        w.writerow([time.strftime("%Y-%m-%d"), row["id"], row["file"], new_file, sha,
                    "catalog edition no longer served; newer edition accepted"])
    return path


def fetch_row(row, root, args, quiet=False):
    """One catalog row. Returns (state, detail)."""
    how = row["fetch"]
    dest = fetch_dest(row, root)
    if how == "manual":
        return ("manual", "get it from %s and put it at %s"
                % (row["source_page"] or "the source named in MANIFEST.csv", dest))
    if how == "hf-repo":
        repo = hf_repo_id(row["fetch_url"])
        rev = row["revision"] or "main"
        include = [x for x in (row.get("include") or "").split(";") if x]
        # A repository the archive keeps only part of (piper-voices: 10 of
        # 3,301 files): the catalog's `include` is the list, and a name the
        # commit does not have is a failure, not a silent skip.
        files, _none = hf_plan(repo, rev, True)
        keep, skipped, gone = hf_select(files, include, args.all_files)
        if gone:
            return ("FAILED", "the catalog includes %s, and commit %s has no such "
                    "file" % (", ".join(gone[:3]), rev[:10]))
        states = []
        for name, size, kind, ident in keep:
            url = "%s/%s/resolve/%s/%s" % (hf_endpoint(), repo, rev, name)
            if not quiet:
                print("    %s" % name)
            st = fetch_one_file(url, os.path.join(dest, *name.split("/")),
                                sha256=ident if kind == "sha256" else None,
                                size=size, verify=args.verify, quiet=quiet,
                                check=None if kind == "sha256" else ("git", ident))
            states.append((name, st))
        bad = [(n, s) for n, s in states if s[0] not in ("fetched", "present")]
        if bad:
            return (bad[0][1][0], "%s: %s" % (bad[0][0], bad[0][1][1]))
        return ("fetched" if any(s[0] == "fetched" for _n, s in states) else "present",
                "%d files at %s, %d skipped (%s)" % (len(keep), rev[:10], len(skipped),
                ("not in the catalog's include list" if include and not args.all_files
                 else "--all-files takes them") if skipped else "none"))
    url = row["fetch_url"]
    if how == "hf-file" and os.environ.get("HF_ENDPOINT"):
        url = hf_endpoint() + url[len(HF_DEFAULT):] if url.startswith(HF_DEFAULT) else url
    try:
        return fetch_one_file(url, dest, row["sha256"] or None, row["bytes"] or None,
                              args.verify, quiet)
    except urllib.error.HTTPError as e:
        if e.code != 404 or not row["file"].endswith(".zim"):
            raise
        newer = newer_zim(url)
        if not newer:
            return ("GONE", "404, and no newer edition of this book was found beside it")
        if not args.accept_newer:
            return ("GONE", "404: this edition is no longer served. The newest is %s. "
                    "Run again with --accept-newer to fetch it, checked against "
                    "Kiwix's own .sha256, and record the substitution" % newer[1])
        with _open(newer[0] + ".sha256") as r:
            sha = r.read().decode("utf-8", "replace").split()[0].lower()
        ndest = os.path.join(os.path.dirname(dest), newer[1])
        st = fetch_one_file(newer[0], ndest, sha, None, args.verify, quiet)
        if st[0] in ("fetched", "present"):
            where = record_substitution(root, row, newer[1], sha)
            return ("SUBSTITUTED", "fetched %s instead, checked against Kiwix's "
                    ".sha256, recorded in %s" % (newer[1], os.path.basename(where)))
        return st


def do_fetch(args):
    try:
        cat = load_catalog(args.catalog or CFG["paths.catalog"])
    except FileNotFoundError as e:
        print("\n  %s\n" % e)
        return 2
    root = os.path.abspath(args.dest) if args.dest else ROOT
    by_id = dict((r["id"], r) for r in cat)
    rows = []
    for i in args.ids:
        if i not in by_id:
            near = [k for k in by_id if i.lower() in k.lower()][:6]
            print("\n  no catalog row %r%s\n" % (i, ("; did you mean: " + ", ".join(near))
                                                if near else ""))
            return 2
        rows.append(by_id[i])
    if args.profile:
        rows += [r for r in cat if args.profile == "full" or r["profile"] == args.profile]
    if args.pin:
        return do_pin(args, rows or cat, root, args.catalog or CFG["paths.catalog"])
    if args.lf:
        return do_lf(args, rows or cat, root)
    if not rows:
        print("\n  name catalog ids, or --profile starter (or full). `fetch --list "
              "--profile starter` shows what that is.\n")
        return 2
    seen, uniq = set(), []
    for r in rows:
        if r["id"] not in seen:
            seen.add(r["id"])
            uniq.append(r)
    rows = uniq

    def local(r):
        d = fetch_dest(r, root)
        if r["file"].endswith("/"):
            return "present" if os.path.isdir(d) and os.listdir(d) else "missing"
        if os.path.exists(d):
            want = int(r["bytes"]) if r["bytes"].isdigit() else None
            return "present" if want in (None, os.path.getsize(d)) else "CONFLICT"
        return "partial" if os.path.exists(d + ".part") else "missing"

    # ONLY WHAT WILL ACTUALLY BE DOWNLOADED. A CONFLICT is refused, not replaced,
    # so counting it would ask for disk space the run will never use.
    need = sum(int(r["bytes"] or 0) for r in rows
               if local(r) in ("missing", "partial") and r["fetch"] != "manual")
    print("\n  catalog  %s\n  into     %s\n" % (args.catalog or CFG["paths.catalog"], root))
    for r in rows:
        print("  %-44s %-12s %9s  %-8s %s" % (r["id"][:44], r["fetch"],
              human(r["bytes"]) if r["bytes"] else "?", local(r),
              " ".join(x for x in (
                  "" if r["license_status"] == "recorded" else "(licence to verify)",
                  "(follows main)" if r["fetch"].startswith("hf")
                  and not HEX40.match(r["revision"] or "") else "") if x)))
    try:
        free = shutil.disk_usage(root if os.path.isdir(root)
                                 else os.path.dirname(root) or ".").free
    except OSError:
        free = None
    print("\n  to download: %s%s" % (human(need), "" if free is None else
                                     ", free here: %s" % human(free)))
    if args.list or args.dry_run:
        print("")
        return 0
    if free is not None and need > free:
        print("  REFUSED: not enough free space. Nothing was downloaded.\n")
        return 2
    results, shelves = [], set()
    for r in rows:
        print("\n  %s" % r["id"])
        try:
            st = fetch_row(r, root, args)
        except urllib.error.HTTPError as e:
            st = ("FAILED", "HTTP %d from %s%s" % (e.code, e.url, (
                ": a gated repository; accept its licence on the model page, then "
                "set HF_TOKEN") if e.code in (401, 403) and r["fetch"].startswith("hf")
                else ""))
        except CertificateRefused as e:
            st = ("FAILED", str(e))
            print("    %s - %s" % st)
            results.append((r, st))
            print("\n  Stopped: every other download would be refused the same way.")
            break
        except (OSError, ValueError) as e:
            st = ("FAILED", "%s: %s" % (type(e).__name__, e))
        print("    %s - %s" % st)
        results.append((r, st))
        if st[0] in ("fetched", "SUBSTITUTED"):
            shelves.add(r["shelf"].split("/")[0])
    print("\n  %-44s %s" % ("ROW", "RESULT"))
    for r, st in results:
        print("  %-44s %s" % (r["id"][:44], st[0]))
    bad = [st for _r, st in results
           if st[0] in ("FAILED", "MISMATCH", "CONFLICT", "GONE", "CHANGED")]
    if shelves and os.path.isfile(os.path.join(root, "MANIFEST.csv")):
        print("\n  Files were added to %s. In an archive with checksummed shelves,\n"
              "  rehash each one: python bin/ark.py rehash <shelf>" % ", ".join(sorted(shelves)))
    print("")
    return 1 if bad else 0


# PINNING, 2026-09-30. The catalog is generated from MANIFEST.csv, which recorded
# Hugging Face repository URLs and not commits, so 45 of its 46 Hugging Face rows
# followed `main`. The first real fetch showed what that costs: `params`, 188
# bytes when it was filed, is 178 bytes at `main` today. For a single file the
# catalog's sha256 still refuses the change. For a whole repository the only
# checks are the ones the API reports for today's `main`, so an updated model
# would arrive "checked" and still not be the weights the node was measured with.
#
# `fetch --pin` asks which commit the archive's copy IS, and answers it from the
# archive. For each row it walks the repository's commits, newest first, and
# takes the newest one whose files match what is on disk: for a single file the
# catalog's sha256 (the API reports sha256 for LFS files, and a git blob id for
# small ones, computed here from the local copy); for a repository every file in
# the archive's folder, by the sha256s its CHECKSUMS.sha256 files already record.
# A row the catalog already pins is only checked against that commit. Nothing is
# downloaded. The answers go to pins.csv beside the catalog, which
# catalog-build.py reads into the `revision` column. A row no commit matches is
# written as UNPINNED with the closest commit and what differs; it stays on
# `main` in the catalog, and the build names it.

HF_PIN_DEPTH = 200
HEX40 = re.compile(r"^[0-9a-f]{40}$")
PIN_COLUMNS = ["id", "state", "repo", "path", "revision", "commit_date",
               "commit_title", "checked", "upstream_sha256", "upstream_bytes",
               "fetch_files", "fetch_bytes", "pinned_on", "note"]


# WHAT A FETCH TAKES, 2026-10-01. The catalog gave a repository row its archive
# folder's size: BGE-M3 6.9 GB, of which `fetch` takes 2.3 GB (the ONNX export
# and the .bin copies are skipped), so `fetch --list --profile starter` said
# 24.5 GB for about 19.9 GB. The honest figure is the commit's, not the
# folder's, and `--pin` already reads the commit: it adds up what hf_select()
# takes there into fetch_files and fetch_bytes, which catalog-build.py puts in
# `bytes`. Checking that rule against the archive found Kokoro-82M-v1.0-ONNX,
# whose model IS its onnx/ folder: the old rule would have fetched 59 voice
# files and no model.
def pin_take(row, tree):
    """{'fetch_files', 'fetch_bytes'} for a repository row at a commit's tree
    ({path: (kind, id, size)}), or {} for a single-file row."""
    if row["fetch"] != "hf-repo":
        return {}
    files = [(n, t[2], t[0], t[1]) for n, t in tree.items()]
    keep, _skipped, gone = hf_select(
        files, (row.get("include") or "").split(";"))
    if gone or any(f[1] is None for f in keep):
        return {}
    return {"fetch_files": str(len(keep)),
            "fetch_bytes": str(sum(int(f[1]) for f in keep))}
PIN_RETRIES = 5

# LINE ENDINGS, found by the first real --pin on 2026-09-30. Every small text
# file in the archive's Hugging Face copies (configs, READMEs, .gitattributes,
# chat templates, piper's .onnx.json, the Ollama `params` and `template`) has
# CRLF line endings; upstream has LF. The byte counts say so exactly: `params`
# is 188 bytes with 10 CRLFs and 178 upstream, `template` 1,531 with 49 and
# 1,482. The files were converted on the way in (a git checkout with
# core.autocrlf on Windows does this; git-lfs files are not touched, which is
# why every weight file matched). So a text file whose git blob id differs is
# compared again with CRLF read as LF. A match that way is still a match, the
# commit is the right one, and pins.csv records the upstream sha256 and size so
# the public catalog lists the bytes a download will actually produce.


def crlf_to_lf(path):
    with open(path, "rb") as fh:
        return fh.read().replace(b"\r\n", b"\n")


def _blob_of(data):
    import hashlib
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def hf_json(url, quiet=False):
    """(parsed JSON, the next page's URL or None) from a Hugging Face API URL. A
    429 (too many requests) is waited out, as long as the host asks, up to
    PIN_RETRIES times: the first --pin run hit one 32 commits into piper-voices."""
    for attempt in range(1, PIN_RETRIES + 1):
        try:
            with _open(url) as r:
                data = json.loads(r.read().decode("utf-8"))
                link = r.headers.get("Link") or ""
            break
        except urllib.error.HTTPError as e:
            if e.code != 429 or attempt == PIN_RETRIES:
                raise
            wait = e.headers.get("Retry-After") or ""
            m = re.search(r"\bt=(\d+)", e.headers.get("RateLimit") or "")
            wait = int(wait) if wait.isdigit() else (int(m.group(1)) if m else 60)
            wait = min(max(wait, 0), 300)
            if not quiet:
                sys.stdout.write("\r    rate limited by the host: waiting %d s (%d of %d)%s\n"
                                 % (wait, attempt, PIN_RETRIES - 1,
                                    "" if os.environ.get("HF_TOKEN") else
                                    "; an HF_TOKEN raises the limit"))
                sys.stdout.flush()
            time.sleep(wait)
    m = re.search(r'<([^>]+)>\s*;\s*rel="?next"?', link)
    nxt = m.group(1) if m else None
    if nxt and nxt.startswith("/"):
        nxt = hf_endpoint() + nxt
    return data, nxt


def hf_commits(repo, depth, quiet=False):
    """The repository's commits on main, newest first, at most `depth`."""
    url, n = "%s/api/models/%s/commits/main" % (hf_endpoint(), repo), 0
    while url and n < depth:
        page, url = hf_json(url, quiet)
        for c in page:
            yield c
            n += 1
            if n >= depth:
                return


def hf_tree(repo, rev, quiet=False):
    """(commit, {path: (kind, id, size)}) at one revision. kind is 'sha256' for
    LFS files and 'git' (a git blob id) for the rest."""
    info, _nxt = hf_json("%s/api/models/%s/revision/%s?blobs=true"
                         % (hf_endpoint(), repo, rev), quiet)
    tree = {}
    for s in info.get("siblings", []):
        lfs = s.get("lfs") or {}
        if lfs.get("sha256"):
            tree[s["rfilename"]] = ("sha256", lfs["sha256"], lfs.get("size") or s.get("size"))
        else:
            tree[s["rfilename"]] = ("git", s.get("blobId") or "", s.get("size"))
    return info.get("sha") or rev, tree


def local_files(folder):
    """What the archive holds in a repository folder, less its own records."""
    out = []
    for r, ds, fs in os.walk(folder):
        ds[:] = [x for x in ds if x not in ("__pycache__", ".cache")]
        for f in fs:
            if f.startswith("CHECKSUMS") or f == ".no-index" or f.endswith((".part", ".bad")):
                continue
            out.append(os.path.relpath(os.path.join(r, f), folder).replace(os.sep, "/"))
    return sorted(out)


def local_sums(folder, root):
    """{path relative to folder: sha256} from every CHECKSUMS.sha256 between the
    folder and the root, the nearest one first."""
    out = {}
    folder, root = os.path.abspath(folder), os.path.abspath(root)
    d = folder
    while True:
        p = os.path.join(d, "CHECKSUMS.sha256")
        if os.path.isfile(p):
            for line in open(p, encoding="utf-8", errors="replace"):
                parts = line.strip().split(None, 1)
                if len(parts) != 2 or not re.fullmatch(r"[0-9a-f]{64}", parts[0]):
                    continue
                rel = parts[1].lstrip("*").replace("\\", "/")
                while rel.startswith("./"):
                    rel = rel[2:]
                full = os.path.normpath(os.path.join(d, *rel.split("/")))
                mine = os.path.relpath(full, folder).replace(os.sep, "/")
                if not mine.startswith("..") and mine not in out:
                    out[mine] = parts[0]
        up = os.path.dirname(d)
        if d == root or up == d or not up.startswith(root):
            break
        d = up
    return out


class PinLocal(object):
    """One archive folder's files, hashed only when a recorded sum is missing."""

    def __init__(self, folder, root):
        self.folder = folder
        self.files = local_files(folder)
        self.sums = local_sums(folder, root)
        self.blobs = {}

    def path(self, rel):
        return os.path.join(self.folder, *rel.split("/"))

    def size(self, rel):
        return os.path.getsize(self.path(rel))

    def sha(self, rel):
        if rel not in self.sums:
            self.sums[rel] = _sha256(self.path(rel), quiet=False)
        return self.sums[rel]

    def blob(self, rel):
        if rel not in self.blobs:
            self.blobs[rel] = _git_blob_sha1(self.path(rel))
        return self.blobs[rel]

    def blob_lf(self, rel):
        k = rel + "\0lf"
        if k not in self.blobs:
            self.blobs[k] = _blob_of(crlf_to_lf(self.path(rel)))
        return self.blobs[k]


def _pin_test(w, tree):
    """(matches, what was checked or what differs, score, extra) for one row at
    one commit. `w` is ('file', row, repo path, local path) or ('repo', row,
    PinLocal). `extra` carries a file's path in the repository when the archive
    renamed it, and the upstream sha256 and size when only line endings differ."""
    import hashlib
    if w[0] == "file":
        _k, row, rpath, lpath = w
        t = tree.get(rpath)
        if t is None or (t[0] == "sha256" and t[1] != row["sha256"]):
            # RENAMED ON THE WAY IN. unsloth calls a file mmproj-BF16.gguf and
            # the archive calls it mmproj-gemma-4-12b-it-BF16.gguf; the catalog
            # URL was built from the archive's name. An LFS file is found by
            # its sha256 wherever it sits in the repository.
            hits = sorted(p for p, v in tree.items()
                          if v[0] == "sha256" and v[1] == row["sha256"])
            if hits:
                return True, "sha256, found as %s" % hits[0], 1, {"path": hits[0]}
        if t is None:
            return False, "%s is not in this commit" % rpath, 0, {}
        kind, ident, size = t
        if kind == "sha256":
            ok = ident == row["sha256"]
            return ok, "sha256" if ok else "sha256 differs", int(ok), {}
        if not os.path.isfile(lpath):
            return False, "no archive copy to compare its git blob id with", 0, {}
        if (size is None or os.path.getsize(lpath) == size) and _git_blob_sha1(lpath) == ident:
            return True, "git blob id", 1, {}
        lf = crlf_to_lf(lpath)
        if (size is None or len(lf) == size) and _blob_of(lf) == ident:
            return True, "git blob id, with the archive's CRLF read as LF", 1, {
                "upstream_sha256": hashlib.sha256(lf).hexdigest(),
                "upstream_bytes": str(len(lf))}
        return False, "%s bytes at this commit, %s in the archive (%s as LF)" % (
            format(size or 0, ","), format(os.path.getsize(lpath), ","),
            format(len(lf), ",")), 0, {}
    loc = w[2]
    bad, crlf = [], 0
    for rel in loc.files:
        t = tree.get(rel)
        if not t:
            bad.append(rel + " (not in this commit)")
            continue
        kind, ident, size = t
        if kind == "sha256":
            if (size is not None and loc.size(rel) != size) or loc.sha(rel) != ident:
                bad.append(rel)
            continue
        if (size is None or loc.size(rel) == size) and loc.blob(rel) == ident:
            continue
        if loc.blob_lf(rel) == ident:
            crlf += 1
            continue
        bad.append(rel)
    extra = len(set(tree) - set(loc.files))
    n = len(loc.files)
    if not bad:
        return True, "%d files%s%s" % (
            n, (", %d of them text saved with CRLF (LF upstream)" % crlf) if crlf else "",
            (", %d more at this commit that the archive does not hold" % extra)
            if extra else ""), n, {}
    return False, "%d of %d files differ, first: %s" % (
        len(bad), n, ", ".join(bad[:3])), n - len(bad), {}


def pin_rows(rows, root, depth=HF_PIN_DEPTH, quiet=False):
    """{id: pins.csv row} for the Hugging Face rows among `rows`."""
    today = time.strftime("%Y-%m-%d")
    results, by_repo = {}, []
    index = {}
    for r in rows:
        if r["fetch"] not in ("hf-file", "hf-repo"):
            continue
        repo = hf_repo_id(r["fetch_url"])
        if repo not in index:
            index[repo] = []
            by_repo.append((repo, index[repo]))
        index[repo].append(r)

    def res(r, state, repo, path="", commit=None, checked="", note="", extra=None):
        commit, extra = commit or {}, extra or {}
        return {"id": r["id"], "state": state, "repo": repo,
                "path": extra.get("path") or path,
                "upstream_sha256": extra.get("upstream_sha256", ""),
                "upstream_bytes": extra.get("upstream_bytes", ""),
                "fetch_files": extra.get("fetch_files", ""),
                "fetch_bytes": extra.get("fetch_bytes", ""),
                "revision": commit.get("id", "") if state in ("PINNED", "CONFIRMED") else "",
                "commit_date": (commit.get("date") or "")[:10],
                "commit_title": (commit.get("title") or "").replace("\n", " ")[:80],
                "checked": checked, "pinned_on": today, "note": note}

    for repo, rs in by_repo:
        if not quiet:
            print("\n  %s  (%d row%s)" % (repo, len(rs), "" if len(rs) == 1 else "s"))
        want = {}
        for r in rs:
            dest = fetch_dest(r, root)
            if r["fetch"] == "hf-repo":
                if not os.path.isdir(dest):
                    results[r["id"]] = res(r, "UNPINNED", repo, note="no archive copy at %s "
                                           "to compare with" % dest)
                    continue
                want[r["id"]] = ("repo", r, PinLocal(dest, root))
            else:
                rpath = urllib.parse.unquote(
                    r["fetch_url"].split("/resolve/", 1)[1].split("/", 1)[1])
                if not r["sha256"] and not os.path.isfile(dest):
                    results[r["id"]] = res(r, "UNPINNED", repo, rpath,
                                           note="no sha256 and no archive copy")
                    continue
                want[r["id"]] = ("file", r, rpath, dest)
        path_of = dict((i, (w[2] if w[0] == "file" else "")) for i, w in want.items())
        pending, best, walked = dict(want), {}, 0
        try:
            for i, w in list(pending.items()):
                rev = w[1]["revision"]
                if HEX40.match(rev or ""):
                    sha, tree = hf_tree(repo, rev, quiet)
                    ok, detail, _s, ex = _pin_test(w, tree)
                    if not ok and w[0] == "repo":
                        # A FOLDER THAT MIXES COMMITS. BGE-M3's safetensors is
                        # absent from its HEAD, so it was fetched from the
                        # pinned commit and the rest of the folder from HEAD
                        # (MANIFEST.csv says so). No single commit is the whole
                        # folder. What matters at the pinned commit is what the
                        # node reads, less documentation (a model card edited
                        # upstream changes no weight); if those match, the pin
                        # holds, and the other files are named.
                        import copy
                        keep = set(k for k in hf_keep_names(tree)
                                   if not (k.lower().endswith(".md")
                                           or k.split("/")[-1].upper().startswith("LICENSE")))
                        sub = copy.copy(w[2])
                        sub.files = [f for f in w[2].files if f in keep]
                        ok2, d2, _s2, _e2 = _pin_test(
                            ("repo", w[1], sub), dict((k, tree[k]) for k in keep))
                        if ok2 and not (keep - set(w[2].files)):
                            ok, ex = True, {}
                            detail = ("what the node reads matches (%s); %d other "
                                      "file(s) in the archive's folder are from another "
                                      "commit (%s)"
                                      % (d2, len(w[2].files) - len(sub.files), detail))
                    if ok:
                        ex = dict(ex, **pin_take(w[1], tree))
                    results[i] = res(w[1], "CONFIRMED" if ok else "DISAGREES", repo,
                                     path_of[i], {"id": rev}, detail if ok else "",
                                     "" if ok else "the catalog's commit %s: %s"
                                     % (rev[:10], detail), ex)
                    del pending[i]
            for c in (hf_commits(repo, depth, quiet) if pending else []):
                walked += 1
                if not quiet:
                    sys.stdout.write("\r    commit %d  %s %s   " % (walked, c["id"][:10],
                                                                  (c.get("date") or "")[:10]))
                    sys.stdout.flush()
                _sha, tree = hf_tree(repo, c["id"], quiet)
                for i, w in list(pending.items()):
                    ok, detail, score, ex = _pin_test(w, tree)
                    if ok:
                        results[i] = res(w[1], "PINNED", repo, path_of[i], c, detail,
                                         extra=dict(ex, **pin_take(w[1], tree)))
                        del pending[i]
                    elif score > best.get(i, (-1,))[0]:
                        best[i] = (score, c, detail)
                if not pending:
                    break
            if not quiet:
                sys.stdout.write("\r" + " " * 60 + "\r")
            for i, w in pending.items():
                b = best.get(i)
                results[i] = res(w[1], "UNPINNED", repo, path_of[i], note=(
                    "no commit among the newest %d matches the archive's copy%s"
                    % (walked, "; closest %s (%s): %s" % (b[1]["id"][:10],
                       (b[1].get("date") or "")[:10], b[2]) if b else "")))
        except urllib.error.HTTPError as e:
            why = "HTTP %d from the Hugging Face API" % e.code
            if e.code in (401, 403):
                why += ": a gated repository; accept its licence on the model page, then set HF_TOKEN"
            for i, w in pending.items():
                results[i] = res(w[1], "UNPINNED", repo, path_of[i], note=why)
        except (OSError, ValueError) as e:
            for i, w in pending.items():
                results[i] = res(w[1], "UNPINNED", repo, path_of[i],
                                 note="%s: %s" % (type(e).__name__, e))
        if not quiet:
            for r in rs:
                p = results.get(r["id"])
                if p:
                    print("    %-40s %-9s %s" % (r["id"][:40], p["state"],
                          ("%s %s  %s" % (p["revision"][:10], p["commit_date"], p["checked"]))
                          if p["revision"] else p["note"]))
                    if p.get("fetch_bytes"):
                        print("    %-40s %-9s fetch takes %s files, %s"
                              % ("", "", p["fetch_files"], human(p["fetch_bytes"])))
    return results


def write_pins(path, results):
    """pins.csv, merged: rows pinned this run replace their old line."""
    old = {}
    if os.path.isfile(path):
        with open(path, encoding="utf-8", newline="") as fh:
            old = dict((r["id"], r) for r in csv.DictReader(fh))
    old.update(results)
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=PIN_COLUMNS, lineterminator="\n",
                       extrasaction="ignore")
    w.writeheader()
    for i in sorted(old):
        w.writerow(old[i])
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(buf.getvalue())
    return path


def do_pin(args, rows, root, catpath):
    hf = [r for r in rows if r["fetch"] in ("hf-file", "hf-repo")]
    if not hf:
        print("\n  none of those rows is a Hugging Face row; nothing to pin\n")
        return 2
    print("\n  pinning %d Hugging Face row%s against the archive at %s\n"
          "  nothing is downloaded; each repository's commits are read newest first"
          % (len(hf), "" if len(hf) == 1 else "s", root))
    results = pin_rows(hf, root, args.pin_depth)
    where = write_pins(os.path.join(os.path.dirname(os.path.abspath(catpath)),
                                    "pins.csv"), results)
    n = {}
    for p in results.values():
        n[p["state"]] = n.get(p["state"], 0) + 1
    print("\n  %s" % ", ".join("%s %d" % kv for kv in sorted(n.items())))
    print("  written to %s" % where)
    print("  next: python bin/catalog-build.py   (reads pins.csv into the catalog)\n")
    return 1 if n.get("DISAGREES") else 0


# LINE ENDINGS BACK TO LF, 2026-09-30. `--pin` found that every small text file
# in the archive's Hugging Face copies has CRLF line endings where upstream has
# LF: 188-byte `params` is upstream's 178 bytes plus ten carriage returns. The
# weights were untouched, and nothing the node does depends on the difference
# (JSON parsers take either). But "a file that does not match its checksum is not
# the file" is this archive's rule, and a copy that is not byte-identical to its
# source can only be verified by explaining it away.
#
# `fetch --lf` lists every such file and PROVES each one before touching it: the
# file, with CRLF read as LF, must have exactly the git blob id Hugging Face
# reports for that path at the row's pinned commit (or, for BGE-M3's folder,
# which mixes commits, at `main`). A file that is not proven is named and left.
# `fetch --lf --apply` then, for each proven file:
#   1. copies the original, byte for byte, to _incoming/crlf-originals-<date>/;
#   2. writes the LF bytes beside it and moves them into place;
#   3. reads the result back and checks its blob id against upstream again;
#   4. replaces the file's sha256 in EVERY CHECKSUMS.sha256 that lists it, from
#      its own folder up to the root (the same hash, only the one line), so the
#      01-models shelf, about 450 GB, needs no rehash;
#   5. updates MANIFEST.csv: a file row's size and sha256, and a folder row's
#      total when that total was exact before;
# and writes every change to converted.csv beside the originals.

LF_LOG_COLUMNS = ["path", "old_sha256", "new_sha256", "old_bytes", "new_bytes",
                  "repo", "revision", "checksums_updated"]


def _lf_files(row, root):
    """[(absolute path, path in the repository)] for one Hugging Face row."""
    dest = fetch_dest(row, root)
    if row["fetch"] == "hf-repo":
        if not os.path.isdir(dest):
            return []
        return [(os.path.join(dest, *rel.split("/")), rel) for rel in local_files(dest)]
    if not os.path.isfile(dest):
        return []
    rpath = urllib.parse.unquote(row["fetch_url"].split("/resolve/", 1)[1].split("/", 1)[1])
    return [(dest, rpath)]


def lf_plan(rows, root, quiet=False):
    """(proven, unproven, same). proven: [(path, rpath, blob id, repo, revision)]
    for files whose only difference from upstream is CRLF. unproven: [(path,
    why)]. same: [path] for files with CRLF that upstream has too, byte for byte:
    the first --lf run listed five as NOT PROVEN (Qwen3.8-27B's LICENSE and
    README, three Phi-4-mini documents) because it only tried the LF reading."""
    proven, unproven, same = [], [], []
    by_repo = {}
    for r in rows:
        if r["fetch"] in ("hf-file", "hf-repo"):
            by_repo.setdefault(hf_repo_id(r["fetch_url"]), []).append(r)
    for repo in sorted(by_repo):
        trees = {}

        def tree(rev):
            if rev not in trees:
                trees[rev] = hf_tree(repo, rev, quiet)[1]
            return trees[rev]
        for r in by_repo[repo]:
            rev = r["revision"]
            if not HEX40.match(rev or ""):
                for p, _rp in _lf_files(r, root):
                    unproven.append((p, "the row is not pinned to a commit"))
                continue
            for path, rpath in _lf_files(r, root):
                # Only a file the repository keeps in git proper can have been
                # converted: git-lfs files (weights, voices, images) never are, and
                # binary files hold CR LF byte pairs by chance - 237 files in these
                # folders contain one, and about 90 are text.
                t = tree(rev).get(rpath)
                if t is None:
                    try:
                        t = tree("main").get(rpath)
                    except urllib.error.HTTPError:
                        t = None
                if t is not None and t[0] != "git":
                    continue
                if os.path.getsize(path) > 64 << 20:
                    continue
                with open(path, "rb") as fh:
                    data = fh.read()
                if b"\r\n" not in data or b"\0" in data:
                    continue
                lf = data.replace(b"\r\n", b"\n")
                as_is = _blob_of(data)
                if any(t2 and t2[0] == "git" and t2[1] == as_is
                       for t2 in (tree(rev).get(rpath), t)):
                    same.append(path)
                    continue
                found = None
                for label in (rev, "main"):
                    try:
                        t = tree(label).get(rpath)
                    except urllib.error.HTTPError:
                        t = None
                    if t and t[0] == "git" and _blob_of(lf) == t[1]:
                        found = (t[1], label if label == "main" else rev)
                        break
                if found:
                    proven.append((path, rpath, found[0], repo, found[1]))
                else:
                    unproven.append((path, "its LF bytes are not upstream's at %s or "
                                     "main" % rev[:10]))
    return proven, unproven, same


def _sums_update(path, old, new, root):
    """Replace `old` with `new` on the line for `path` in every CHECKSUMS.sha256
    from its folder up to the root. Returns how many files were changed."""
    path, root = os.path.abspath(path), os.path.abspath(root)
    d, n = os.path.dirname(path), 0
    while True:
        cs = os.path.join(d, "CHECKSUMS.sha256")
        if os.path.isfile(cs):
            with open(cs, "rb") as fh:
                lines = fh.read().split(b"\n")
            hit = False
            for i, line in enumerate(lines):
                parts = line.strip().split(None, 1)
                if len(parts) != 2 or parts[0].decode("ascii", "replace") != old:
                    continue
                rel = parts[1].decode("utf-8", "replace").lstrip("*").replace("\\", "/")
                while rel.startswith("./"):
                    rel = rel[2:]
                if os.path.normpath(os.path.join(d, *rel.split("/"))) == path:
                    lines[i] = line.replace(old.encode(), new.encode(), 1)
                    hit = True
            if hit:
                tmp = cs + ".lf-tmp"
                with open(tmp, "wb") as fh:
                    fh.write(b"\n".join(lines))
                os.replace(tmp, cs)
                n += 1
        up = os.path.dirname(d)
        if d == root or up == d or not up.startswith(root):
            break
        d = up
    return n


def _folder_total(d):
    return sum(os.path.getsize(os.path.join(a, f)) for a, _ds, fs in os.walk(d) for f in fs)


def lf_apply(proven, root, stamp=None, quiet=False):
    """Convert the proven files. Returns (log rows, manifest notes)."""
    import hashlib
    stamp = stamp or time.strftime("%Y-%m-%d")
    keep = os.path.join(root, "_incoming", "crlf-originals-" + stamp)
    man = os.path.join(root, "MANIFEST.csv")
    folders = []
    if os.path.isfile(man):
        with open(man, encoding="utf-8", newline="") as fh:
            for r in csv.DictReader(fh):
                if r["filename"].endswith("/") and r["category"]:
                    d = os.path.join(root, *r["category"].split("/"),
                                     *r["filename"].rstrip("/").split("/"))
                    if os.path.isdir(d):
                        folders.append((r, os.path.abspath(d), _folder_total(d)))
    log, notes = [], []
    for path, rpath, blob, repo, rev in proven:
        with open(path, "rb") as fh:
            data = fh.read()
        lf = data.replace(b"\r\n", b"\n")
        if _blob_of(lf) != blob:
            notes.append("SKIPPED, changed since the plan: %s" % path)
            continue
        rel = os.path.relpath(path, root)
        orig = os.path.join(keep, rel)
        os.makedirs(os.path.dirname(orig), exist_ok=True)
        with open(orig, "wb") as fh:
            fh.write(data)
        tmp = path + ".lf-tmp"
        with open(tmp, "wb") as fh:
            fh.write(lf)
        os.replace(tmp, path)
        if _git_blob_sha1(path) != blob:
            raise RuntimeError("%s did not read back as upstream's blob; the original "
                               "is at %s" % (path, orig))
        old_sha, new_sha = hashlib.sha256(data).hexdigest(), hashlib.sha256(lf).hexdigest()
        n = _sums_update(path, old_sha, new_sha, root)
        log.append({"path": rel.replace(os.sep, "/"), "old_sha256": old_sha,
                    "new_sha256": new_sha, "old_bytes": len(data), "new_bytes": len(lf),
                    "repo": repo, "revision": rev, "checksums_updated": n})
        if not quiet:
            print("    %-64s %8s -> %-8s  %d CHECKSUMS" % (rel[-64:], format(len(data), ","),
                                                         format(len(lf), ","), n))
    # MANIFEST.csv, edited as text so nothing else in it moves.
    if log and os.path.isfile(man):
        with open(man, encoding="utf-8", newline="") as fh:
            text = fh.read()
        for e in log:
            shelf, base = os.path.dirname(e["path"]).replace("\\", "/"), os.path.basename(e["path"])
            old = ",%s,%s," % (e["old_bytes"], e["old_sha256"])
            hits = [ln for ln in text.split("\n")
                    if ln.startswith(shelf + "," + base + ",") and old in ln]
            if len(hits) == 1:
                text = text.replace(hits[0], hits[0].replace(
                    old, ",%s,%s," % (e["new_bytes"], e["new_sha256"]), 1), 1)
                notes.append("MANIFEST.csv row %s: %s -> %s bytes, sha256 updated"
                             % (base, e["old_bytes"], e["new_bytes"]))
        for r, d, before in folders:
            if not any(os.path.abspath(os.path.join(root, e["path"])).startswith(d + os.sep)
                       for e in log):
                continue
            after = _folder_total(d)
            lead = "%s,%s,%s," % (r["category"], r["filename"], r["source_url"])
            hits = [ln for ln in text.split("\n") if ln.startswith(lead)]
            if str(before) == r["expected_size_bytes"] and len(hits) == 1:
                text = text.replace(hits[0], hits[0].replace(
                    lead + str(before) + ",", lead + str(after) + ",", 1), 1)
                notes.append("MANIFEST.csv folder %s: %s -> %s bytes"
                             % (r["filename"], format(before, ","), format(after, ",")))
            else:
                notes.append("MANIFEST.csv folder %s left at %s: it was not the folder's "
                             "exact total (%s)" % (r["filename"], r["expected_size_bytes"],
                                                   format(before, ",")))
        tmp = man + ".lf-tmp"
        with open(tmp, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        os.replace(tmp, man)
    if log:
        os.makedirs(keep, exist_ok=True)
        where = os.path.join(keep, "converted.csv")
        new = not os.path.isfile(where)
        with open(where, "a", encoding="utf-8", newline="\n") as fh:
            w = csv.DictWriter(fh, fieldnames=LF_LOG_COLUMNS, lineterminator="\n")
            if new:
                w.writeheader()
            w.writerows(log)
        notes.append("originals and converted.csv in %s" % keep)
    return log, notes


def do_lf(args, rows, root):
    hf = [r for r in rows if r["fetch"] in ("hf-file", "hf-repo")]
    print("\n  text files in the archive's Hugging Face copies whose only difference "
          "from\n  upstream is CRLF line endings, proven against the pinned commit\n")
    proven, unproven, same = lf_plan(hf, root)
    for p, rp, _b, repo, rev in proven:
        print("  %-66s %s" % (os.path.relpath(p, root)[-66:], rev[:10]))
    for p, why in unproven:
        print("  NOT PROVEN  %s: %s" % (os.path.relpath(p, root), why))
    total = sum(os.path.getsize(p) for p, *_x in proven)
    print("\n  %d file(s) proven, %s; %d not proven, left as they are; %d with CRLF "
          "that upstream has too, already right" % (len(proven), human(total),
                                                     len(unproven), len(same)))
    if not args.apply:
        print("  nothing changed. --apply converts the proven files, keeps each original\n"
              "  in _incoming, and updates every CHECKSUMS.sha256 and MANIFEST.csv row\n")
        return 0
    print("\n  converting\n")
    log, notes = lf_apply(proven, root)
    print("")
    for n in notes:
        print("  " + n)
    print("\n  %d converted. Next: python bin/ark.py fetch --lf  (expect 0 proven),\n"
          "  then python bin/ark.py fetch --pin, then python bin/catalog-build.py\n" % len(log))
    return 0


# ---------------------------------------------------------------------------
# INDEX, 2026-09-30. After index-build.py writes new chunks and vectors, five
# more things have to happen before anything is searchable, in an order that
# matters, and until now an operator ran them by hand from the list
# index-build.py prints:
#
#   pq          index-pq-build.py --verify   the dense index     1,160 s at 39.1M
#   bm25        index-bm25.py --build        the keyword index   7,089 s at 39.1M
#   provenance  index-provenance.py          citations           resumes per artifact
#   mirror      index-mirror-set.py          mdwiki vs Wikipedia minutes; keys on provenance
#   rehash      ark.py rehash 10-index       the shelf manifest  last, reads all of it
#
# (times measured 2026-09-25, BUILD-LOG). `ark.py index` runs them, but only
# the ones that are due. Each step is judged the way the thing that reads it
# judges it: the PQ stamp as index-pq-build.py and store.py compare it, the
# provenance offsets as index-provenance.py checks them, the mirror stamp as
# store.py refuses it. Nothing here writes to 10-index to find out: in
# particular it never runs `index-provenance.py --status`, which updates the
# database it reports on and so would invalidate the mirror set's stamp.
#
# THE KEYWORD STORE HAD NO STAMP. It is 109 GB and two hours to rebuild, so
# rebuilding it "to be safe" is not an option and neither is guessing. From
# 2026-09-30 index-bm25.py writes one inside the store; one built before that is
# judged by date, and the plan says so.

INDEX_STEPS = ["pq", "bm25", "provenance", "mirror", "rehash"]
INDEX_ABOUT = {
    "pq": "dense index, 10-index/pq/ (1,160 s at 39.1M chunks)",
    "bm25": "keyword index, bm25.sqlite3 (7,089 s at 39.1M chunks)",
    "provenance": "citations, provenance.sqlite3 (resumes per artifact)",
    "mirror": "mdwiki copies of Wikipedia articles, mirror-mdwiki.json",
    "rehash": "10-index/CHECKSUMS.sha256 (reads the whole shelf)",
}
_IX_SKIP = ("library.xml",)


def _npy_rows(path):
    """Rows of a 2-D .npy, from its header alone: no numpy on this side."""
    import ast
    with open(path, "rb") as fh:
        if fh.read(6) != b"\x93NUMPY":
            raise ValueError("%s is not a .npy file" % path)
        major = fh.read(2)[0]
        n = int.from_bytes(fh.read(2 if major == 1 else 4), "little")
        head = ast.literal_eval(fh.read(n).decode("latin-1"))
    return int(head["shape"][0])


def _ix_registry(ix):
    try:
        with open(os.path.join(ix, "sources.json"), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _ix_ro(path):
    import sqlite3
    return sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"), uri=True)


def pq_state(ix):
    reg = _ix_registry(ix)
    if reg is None:
        return "absent", "no sources.json: nothing has been built"
    arts = []
    for _sid, a in sorted(reg.get("artifacts", {}).items(), key=lambda kv: int(kv[0])):
        slug = re.sub(r"[^A-Za-z0-9._-]", "_", a["file"])
        cf = os.path.join(ix, "chunks", slug + ".jsonl")
        vf = os.path.join(ix, "vectors", slug + ".f16.npy")
        if os.path.exists(cf) and os.path.exists(vf):
            arts.append([slug, _npy_rows(vf), os.path.getsize(vf)])
    want = {"artifacts": arts, "total": sum(a[1] for a in arts)}
    mp = os.path.join(ix, "pq", "manifest.json")
    if not os.path.exists(mp):
        return "absent", "no pq/manifest.json"
    try:
        with open(mp, encoding="utf-8") as fh:
            man = json.load(fh)
    except (OSError, ValueError):
        return "error", "pq/manifest.json is unreadable"
    if not os.path.exists(os.path.join(ix, "pq", man.get("index", "ark-pq64.faiss"))):
        return "absent", "the manifest is there and the index file is not"
    if man.get("stamp") == want:
        return "current", "%s vectors over %d artifacts" % (
            format(want["total"], ","), len(arts))
    have = dict((a[0], a) for a in (man.get("stamp") or {}).get("artifacts", []))
    diff = [a[0] for a in arts if have.get(a[0]) != a] + \
        [s for s in have if s not in set(a[0] for a in arts)]
    return "stale", "built over %s vectors, %s on disk; differs: %s" % (
        format((man.get("stamp") or {}).get("total", 0), ","),
        format(want["total"], ","), ", ".join(diff[:3]) + (" ..." if len(diff) > 3 else ""))


def bm25_stamp(ix):
    """The same stamp index-bm25.py writes; see its stamp()."""
    ef = os.path.join(ix, "excluded-ids.txt")
    return {"chunks": [[os.path.basename(f), os.path.getsize(f)] for f in
                       sorted(glob.glob(os.path.join(ix, "chunks", "*.jsonl")))],
            "excluded": os.path.getsize(ef) if os.path.exists(ef) else None}


def bm25_state(ix):
    db = os.path.join(ix, "bm25.sqlite3")
    if not os.path.exists(db):
        return "absent", "no bm25.sqlite3"
    want = bm25_stamp(ix)
    got = None
    try:
        con = _ix_ro(db)
        try:
            row = con.execute("SELECT v FROM meta WHERE k='stamp'").fetchone()
            got = json.loads(row[0]) if row else None
        finally:
            con.close()
    except Exception:                                    # noqa: BLE001
        got = None
    if got is not None:
        same = got.get("chunks") == want["chunks"] and got.get("excluded") == want["excluded"]
        return ("current" if same else "stale"), (
            "%s rows, stamped %s" % (format(got.get("rows", 0), ","), got.get("built", "?"))
            if same else "its chunk files or exclusion list changed since %s"
            % got.get("built", "?"))
    inputs = [os.path.join(ix, "chunks", c[0]) for c in want["chunks"]]
    ef = os.path.join(ix, "excluded-ids.txt")
    if os.path.exists(ef):
        inputs.append(ef)
    newer = [os.path.basename(f) for f in inputs
             if os.path.getmtime(f) > os.path.getmtime(db)]
    if newer:
        return "stale", "no stamp, and %d input(s) are newer than it: %s" % (
            len(newer), ", ".join(newer[:2]))
    return "current", ("no stamp (built before 2026-09-30), so judged by date: "
                       "newer than every chunk file")


def provenance_state(ix):
    db = os.path.join(ix, "provenance.sqlite3")
    reg = _ix_registry(ix)
    if reg is None:
        return "absent", "no sources.json"
    if not os.path.exists(db):
        return "absent", "no provenance.sqlite3"
    try:
        con = _ix_ro(db)
        try:
            rows = con.execute("SELECT src, done, jsonl FROM artifact").fetchall()
            meta = dict(con.execute("SELECT k, v FROM meta").fetchall())
        finally:
            con.close()
    except Exception as e:                               # noqa: BLE001
        return "error", "provenance.sqlite3 cannot be read: %s" % e
    have = dict((int(r[0]), r) for r in rows)
    pending = [s for s in (int(k) for k in reg.get("artifacts", {}))
               if s not in have or not have[s][1]]
    moved = []
    for src, _done, jl in rows:
        p2 = os.path.join(ix, "chunks", jl or "")
        sig = meta.get("jsonl:%d" % int(src))
        if jl and os.path.exists(p2) and sig is not None:
            st = os.stat(p2)
            if sig != "%d:%d" % (st.st_size, int(st.st_mtime)):
                moved.append(jl)
    if pending or moved:
        return "stale", "%d artifact(s) pending, %d chunk file(s) changed since ingest" % (
            len(pending), len(moved))
    return "current", "%d artifacts ingested" % len(have)


def mirror_state(ix):
    reg = _ix_registry(ix) or {}
    files = [a.get("file", "") for a in reg.get("artifacts", {}).values()]
    if not (any(f.startswith("mdwiki_") for f in files)
            and any(f.startswith("wikipedia_en_medicine") for f in files)):
        return "n/a", "this index has no mdwiki and Wikipedia medicine pair"
    mp = os.path.join(ix, "mirror-mdwiki.json")
    if not os.path.exists(mp):
        return "absent", "no mirror-mdwiki.json"
    try:
        with open(mp, encoding="utf-8") as fh:
            d = json.load(fh)
        here = {"old_jsonl": os.path.join(ix, "chunks", d["old"]["jsonl"]),
                "new_jsonl": os.path.join(ix, "chunks", d["new"]["jsonl"]),
                "provenance": os.path.join(ix, "provenance.sqlite3")}
        for k, path in here.items():
            w = d["stamp"][k]
            if not os.path.exists(path):
                return "stale", "it names %s, which is not here" % os.path.basename(path)
            st = os.stat(path)
            if st.st_size != w["size"] or int(st.st_mtime) != w["mtime"]:
                return "stale", "built against a different %s" % os.path.basename(path)
    except (OSError, ValueError, KeyError) as e:
        return "error", "mirror-mdwiki.json cannot be read: %s" % e
    return "current", "%s articles set aside as copies" % format(len(d.get("dnums", [])), ",")


def rehash_state(ix):
    cs = os.path.join(ix, "CHECKSUMS.sha256")
    if not os.path.exists(cs):
        return "absent", "no CHECKSUMS.sha256"
    listed = set()
    with open(cs, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            parts = line.strip().split(None, 1)
            if len(parts) == 2:
                rel = parts[1].lstrip("*").replace("\\", "/")
                while rel.startswith("./"):
                    rel = rel[2:]
                listed.add(rel)
    when = os.path.getmtime(cs)
    actual, newer = set(), 0
    for a, ds, fs in os.walk(ix):
        ds[:] = [x for x in ds if x not in ("__pycache__", "Claude outputs")]
        for f in fs:
            if f.startswith("CHECKSUMS.sha256") or f in _IX_SKIP or f.endswith(".building"):
                continue
            q = os.path.join(a, f)
            actual.add(os.path.relpath(q, ix).replace(os.sep, "/"))
            if os.path.getmtime(q) > when:
                newer += 1
    new, gone = actual - listed, listed - actual
    if new or gone or newer:
        return "stale", "%d unlisted, %d listed and missing, %d changed since the last rehash" % (
            len(new), len(gone), newer)
    return "current", "%d files" % len(actual)


INDEX_STATE = {"pq": pq_state, "bm25": bm25_state, "provenance": provenance_state,
               "mirror": mirror_state, "rehash": rehash_state}


def index_command(step, py, force=False):
    b = lambda name: os.path.join(ROOT, "bin", name)    # noqa: E731
    return {"pq": [py, b("index-pq-build.py"), "--verify"] + (["--force"] if force else []),
            "bm25": [py, b("index-bm25.py"), "--build"],
            "provenance": [py, b("index-provenance.py")] + (["--rebuild"] if force else []),
            "mirror": [py, b("index-mirror-set.py")],
            "rehash": [sys.executable, os.path.join(ROOT, "bin", "ark.py"),
                       "rehash", "10-index"]}[step]


def index_plan(ix, states=None):
    states = states or INDEX_STATE
    out = []
    for s in INDEX_STEPS:
        try:
            out.append((s,) + states[s](ix))
        except Exception as e:                           # noqa: BLE001
            out.append((s, "error", "%s: %s" % (type(e).__name__, e)))
    return out


def index_run(ix, force=(), no_rehash=False, node_up=False, states=None,
              command=None, run=None, free=None, quiet=False):
    """Run the steps that are due, in order, and stop at the first failure.
    Returns (exit code, [(step, outcome)])."""
    states = states or INDEX_STATE
    say = (lambda *a: None) if quiet else print
    if node_up:
        say("\n  REFUSED: the node is up, and it holds these files open. Rebuilding\n"
            "  under it fails on Windows and misleads everywhere else.\n"
            "  python bin/ark.py down   first, then run this again.\n")
        return 2, []
    done = []
    for step in INDEX_STEPS:
        st, why = states[step](ix)
        if step == "rehash" and no_rehash:
            done.append((step, "skipped (--no-rehash)"))
            say("\n  %-10s skipped: --no-rehash. Run it before trusting the shelf." % step)
            continue
        if st in ("current", "n/a") and step not in force:
            done.append((step, st))
            say("\n  %-10s %s: %s" % (step, st, why))
            continue
        if step == "bm25" and free is not None:
            need = int(os.path.getsize(os.path.join(ix, "bm25.sqlite3")) * 1.1) \
                if os.path.exists(os.path.join(ix, "bm25.sqlite3")) else 0
            if need and free(ix) < need:
                say("\n  REFUSED at bm25: it is built beside the old store and needs "
                    "about %s free here" % human(need))
                done.append((step, "REFUSED"))
                return 1, done
        cmd = command(step, step in force)
        say("\n  %-10s %s: %s\n  running    %s\n" % (step, st, why, " ".join(cmd)))
        t0 = time.time()
        rc = run(cmd)
        secs = time.time() - t0
        if rc != 0:
            done.append((step, "FAILED"))
            say("\n  %s FAILED (exit %s after %.0f s). Nothing after it was run; fix "
                "this and run `ark.py index` again - finished steps are not repeated."
                % (step, rc, secs))
            return 1, done
        st2, why2 = states[step](ix)
        if st2 not in ("current", "n/a"):
            done.append((step, "ran, still " + st2))
            say("\n  %s ran (%.0f s) and is still %s: %s. Stopping here." % (step, secs, st2, why2))
            return 1, done
        done.append((step, "done in %.0f s" % secs))
    return 0, done


# INDEX --ADD, 2026-10-06. One collection from "on the drive" to "citable" in one
# command, because the corpus view (corpus.py, /library/) found that the honest
# way to say it was three: append the path to a scope file, run index-build.py
# --artifact against that scope, then `ark.py index`. The first of the three was
# an `echo ... >> file` that means different things in cmd, PowerShell (which
# appends UTF-16) and Git Bash (which eats the backslashes), so it is done here.
#
# THE SCOPE FILE IS 10-index/scope-local.txt, beside setup's scope-starter.txt:
# it describes this node's index, so it lives with it, and never in bin/, whose
# manifest is the project's. Written UTF-8, LF, one path per line, idempotent.

def corpus_row(rows, rid):
    """The catalog row a collection id names, if it is one that can be indexed."""
    for r in rows:
        if r.get("id") == rid:
            if r.get("kind") not in ("zim", "pdf", "folder"):
                return None, "%s is a %s, not a collection" % (rid, r.get("kind"))
            return r, ""
    return None, "no catalog row has the id %r (python bin/ark.py fetch --list)" % rid


def scope_add(path, line):
    """Append `line` to a scope file unless it is already there. True if added."""
    have = []
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            have = [x.split("#", 1)[0].strip() for x in fh]
    if line in have:
        return False
    os.makedirs(os.path.dirname(path), exist_ok=True)
    new = not os.path.isfile(path)
    with open(path, "a", encoding="utf-8", newline="\n") as fh:
        if new:
            fh.write("# Collections added to this node's index by `ark.py index --add`.\n"
                     "# One path per line, relative to the archive. Read by\n"
                     "# index-build.py --scope <this file> --artifact <name>.\n")
        fh.write(line + "\n")
    return True


def index_add(args, ix, py):
    """Index one collection, then fall through to the usual steps. Returns an
    exit code to stop with, or None to carry on into the steps."""
    try:
        rows = load_catalog(CFG["paths.catalog"])
    except FileNotFoundError as e:
        print("\n  %s\n" % e)
        return 2
    r, why = corpus_row(rows, args.add)
    if not r:
        print("\n  %s\n" % why)
        return 2
    dest = fetch_dest(r, ROOT)
    rel = "%s/%s" % (r["shelf"].strip("/"), r["file"].strip("/"))
    if r["kind"] == "folder":
        rel += "/"
    if not os.path.exists(dest):
        print("\n  %s is not on this drive (%s).\n  Get it first: python bin/ark.py fetch %s\n"
              % (args.add, dest, args.add))
        return 2
    scope = os.path.join(ix, "scope-local.txt")
    name = r["file"].rstrip("/").split("/")[-1]
    build = [py, os.path.join(ROOT, "bin", "index-build.py"), "--scope", scope,
             "--artifact", name, "--resume"]
    print("\n  add       %s\n  path      %s\n  scope     %s\n  build     %s\n"
          % (args.add, rel, scope, " ".join(build)))
    if args.plan:
        print("  plan only; nothing was written or run.\n")
        return 0
    # THE PRIMARY AND THE EMBEDDING MODEL DO NOT FIT ON A 16 GB CARD TOGETHER
    # (13-ark-node/README.md). Refused here rather than discovered as an
    # out-of-memory error an hour into the build.
    if listening(CFG["ports.primary"]) and not args.with_models:
        print("  The primary model is running, and indexing needs the graphics card.\n"
              "  Stop the node first, then run this again:\n"
              "    python bin/ark.py down\n"
              "  (--with-models runs it anyway, on a card with room for both.)\n")
        return 3
    print("  scope     %s" % ("added" if scope_add(scope, rel) else "already listed"))
    code = subprocess.call(build, cwd=ROOT)
    if code:
        print("\n  index-build.py stopped (exit %d). Nothing after it was run.\n" % code)
        return code
    return None


def do_index(args):
    ix = os.path.join(ROOT, "10-index")
    py = node_python()[0]
    if getattr(args, "add", None):
        code = index_add(args, ix, py)
        if code is not None:
            return code
    force = set()
    for f in (args.force or "").split(","):
        f = f.strip()
        if f:
            if f not in INDEX_STEPS:
                print("\n  --force takes %s, not %r\n" % (",".join(INDEX_STEPS), f))
                return 2
            force.add(f)
    print("\n  10-index  %s\n  python    %s\n" % (ix, py))
    print("  %-10s %-8s %s" % ("STEP", "STATE", "WHY"))
    for s, st, why in index_plan(ix):
        print("  %-10s %-8s %s" % (s, st, why))
    if args.plan:
        print("\n  plan only; nothing was run. Each step, when due:")
        for s in INDEX_STEPS:
            print("    %-10s %s" % (s, INDEX_ABOUT[s]))
        print("")
        return 0

    def run(cmd):
        if cmd[0] == "bash" and not shutil.which("bash"):
            print("  no bash on PATH. Run this from Git Bash: %s" % " ".join(cmd))
            return 127
        return subprocess.call(cmd, cwd=ROOT)
    code, done = index_run(
        ix, force, args.no_rehash, listening(CFG["ports.node"]),
        command=lambda s, f: index_command(s, py, f), run=run,
        free=lambda d: shutil.disk_usage(d).free)
    if done:
        print("\n  %-10s %s" % ("STEP", "OUTCOME"))
        for s, o in done:
            print("  %-10s %s" % (s, o))
    if code == 0 and not os.environ.get("ARK_SETUP"):
        print("\n  10-index is consistent. Start the node and read its dense line:\n"
              "    python bin/ark.py up\n    python bin/ark.py status\n")
    return code


# ---------------------------------------------------------------------------
# SETUP, 2026-10-02. From an empty clone to a first cited answer, one command:
#
#     python bin/ark.py setup --profile starter
#
# It is the open-source plan's promise in one verb: a Windows PC with an NVIDIA
# card, nothing installed but Python, and the README. Every step it takes is a
# command that already exists and can be run by hand (`--plan` prints them);
# setup adds the order, a check of each step's result before the next one, and
# the refusals below. Run it again after any failure: a step whose result is
# already there is skipped, so an interrupted 20 GB download or a half-built
# index costs only what was not finished.
#
# THE STEPS, each judged by what is on disk or on a port, never by a record of
# having run:
#   settings  ark.toml for this card's model profile (config --profiles). The
#             starter kit does not download the cross-check model, so the file
#             turns the cross-check off and says how to turn it on.
#   fetch     the starter rows of the catalog, and the model the settings name.
#   binaries  llama.cpp into paths.llama, kiwix-tools beside paths.kiwix_serve,
#             from the zips just fetched. Nothing already there is overwritten.
#   venv      paths.venv: torch from PyTorch's CUDA index first, then
#             13-ark-node/requirements/index.txt. Checked by importing the
#             stack and asking torch whether it sees the GPU.
#   build     bin/index-build.py over the starter's ZIM files, on the GPU.
#   library   bin/kiwix-library.py: library.xml for kiwix-serve.
#   kiwix     ark.py up archive.
#   books     kiwix-library.py --verify-against the running kiwix-serve: the book
#             keys citations link to, read from the server rather than guessed.
#             Before the node starts, because the node reads them once, at start,
#             and before `index`, because they live in 10-index and its rehash
#             should list them.
#   index     ark.py index: dense, keyword and citation indexes, and the rehash.
#   node      ark.py up node primary.
#   ask       the question the site follows, in English and in Spanish, through
#             the node's own /api/answer. Always run: it is the check.
#
# IT REFUSES, before touching anything:
#   - a tree with MANIFEST.csv at its root. That is an established archive with
#     its own settings and history; setup builds a new one in an empty clone.
#   - Python older than 3.11, which cannot read the ark.toml setup writes.
#   - a node, kiwix-serve or model server already on this tree's ports that is
#     not this tree's own. A second node answering on 8090 would make the last
#     step report another archive's answer as this one's.

SETUP_STEPS = ["settings", "fetch", "binaries", "venv", "build", "library",
               "kiwix", "books", "index", "node", "ask"]
SETUP_ABOUT = {
    "settings": "ark.toml for this card's model profile, cross-check off",
    "fetch": "the starter rows and the profile's model, checked by sha256",
    "binaries": "llama.cpp and kiwix-tools, from the zips just fetched",
    "venv": "the Python environment: CUDA torch, then requirements/index.txt",
    "build": "bin/index-build.py over the starter ZIMs (GPU)",
    "index": "python bin/ark.py index",
    "library": "python bin/kiwix-library.py",
    "kiwix": "python bin/ark.py up archive",
    "books": "python bin/kiwix-library.py --verify-against <kiwix-serve>",
    "node": "python bin/ark.py up node primary",
    "ask": "the chlorine question, English and Spanish, through /api/answer",
}
SETUP_QUESTIONS = [("en", "how much chlorine to disinfect drinking water"),
                   ("es", "cuánto cloro para desinfectar el agua de beber")]
# Beyond the download: the environment (about 6 GB with CUDA torch, plus pip's
# cache) and the starter index (well under 5 GB). Generous on purpose: a disk
# that fills during the index build fails an hour in, not at the start.
SETUP_MARGIN = 15 * 1024 ** 3
SETUP_PROBE = r"""
import importlib, json, sys
out = {"missing": [], "python": sys.version.split()[0]}
for m in ("numpy", "torch", "faiss", "sentence_transformers", "transformers",
          "libzim", "fitz", "llama_index.core"):
    try:
        importlib.import_module(m)
    except Exception as e:
        out["missing"].append("%s (%s: %s)" % (m, type(e).__name__, str(e).strip()[:240]))
try:
    import torch
    out["torch"] = torch.__version__
    out["cuda"] = bool(torch.cuda.is_available())
    out["gpu"] = torch.cuda.get_device_name(0) if out["cuda"] else ""
except Exception:
    out["cuda"] = False
print("ARK-PROBE " + json.dumps(out))
"""


def requirements_torch(path):
    """(torch requirement, index URL) from node.txt's `torch==` line and its
    `# torch-index:` comment."""
    req, idx = None, None
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            s = line.strip()
            m = re.match(r"#\s*torch-index:\s*(\S+)", s)
            if m:
                idx = m.group(1)
            elif re.match(r"torch\s*==", s):
                req = s.split("#")[0].replace(" ", "")
    return req, idx


def _zip_target(name):
    """Which folder a starter zip belongs in: 'llama', 'kiwix', or None."""
    b = os.path.basename(name).lower()
    if b.endswith(".zip") and ("llama" in b or "cudart" in b):
        return "llama"
    if b.endswith(".zip") and "kiwix-tools" in b:
        return "kiwix"
    return None


def extract_zip(zpath, dest):
    """Extract into dest, flat as the release zips are, without overwriting.
    Returns (written, kept). A member that would land outside dest is refused,
    and nothing from that zip is written."""
    import zipfile
    dest = os.path.abspath(dest)
    with zipfile.ZipFile(zpath) as z:
        names = [n for n in z.namelist() if not n.endswith("/")]
        for n in names:
            t = os.path.abspath(os.path.join(dest, *n.replace("\\", "/").split("/")))
            if os.path.isabs(n) or not (t == dest or t.startswith(dest + os.sep)):
                raise ValueError("%s: member %r would be written outside %s"
                                 % (os.path.basename(zpath), n, dest))
        written, kept = 0, 0
        for n in names:
            t = os.path.join(dest, *n.replace("\\", "/").split("/"))
            if os.path.exists(t):
                kept += 1
                continue
            os.makedirs(os.path.dirname(t), exist_ok=True)
            with z.open(n) as src, open(t + ".part", "wb") as out:
                shutil.copyfileobj(src, out)
            os.replace(t + ".part", t)
            written += 1
    return written, kept


def disable_crosscheck(toml_text, how_to=""):
    """The [models.crosscheck] section of a profile's ark.toml, turned off."""
    lines = toml_text.split("\n")
    try:
        start = lines.index("[models.crosscheck]")
    except ValueError:
        return toml_text
    for i in range(start + 1, len(lines)):
        if lines[i].startswith("["):
            break
        if lines[i].strip() == "# enabled = true":
            note = ["# THE STARTER KIT DOES NOT DOWNLOAD THE CROSS-CHECK MODEL, so it is",
                    "# off. Answers still cite their passages; what is missing is the",
                    "# second model family reading the same passages." + (
                        " To turn it on:" if how_to else "")]
            if how_to:
                note += ["#   " + how_to, "# then set enabled = true and run ark.py up."]
            lines[i:i + 1] = note + ["enabled = false"]
            break
    return "\n".join(lines)


class Setup:
    """The state of one tree, and what each step needs. Everything that touches
    a process, a port or the network comes in as a function, so the selftest can
    stand in for it."""

    def __init__(self, root, cfg, catalog, models=None, windows=WINDOWS,
                 run=None, capture=None, health=None, busy=None, free=None,
                 vram=total_vram_mib, out=print, python=None, environ=None,
                 sleep=None):
        self.root, self.cfg, self.cat = root, cfg, catalog
        self.environ = os.environ if environ is None else environ
        self.models_arg, self.windows, self.out = models, windows, out
        # ARK_SETUP TELLS THE TOOLS SETUP RUNS THAT SETUP OWNS THE SEQUENCE, so
        # each keeps its "what to run next" advice to itself: on 2026-10-01 the
        # first real run printed index-build's six follow-up commands, a
        # rehash.sh line and a MANIFEST.csv instruction, all of which setup was
        # about to do or which do not apply to a clone.
        self.run = run or (lambda cmd: subprocess.call(
            cmd, cwd=root, env=dict(os.environ, ARK_SETUP="1")))
        self.capture = capture or self._capture
        self.health = health or (lambda port: node_health(port))
        self.busy = busy or listening
        self.free = free or (lambda d: shutil.disk_usage(d).free)
        # NONE MEANS "NO CARD", NOT "ASK THE REAL ONE". The first version fell
        # back to nvidia-smi when given None, so the selftest's no-card case
        # asked the M18's 16 GB card and passed only on machines without one:
        # 113/114 on the M18, 2026-10-02. A number, None, or a function to call.
        self.vram = vram
        self.python = python or sys.executable
        self.sleep = sleep or time.sleep
        self._vram_mib = None
        self.ix = os.path.join(root, "10-index")
        self.ark = os.path.join(root, "bin", "ark.py")
        self.pins = {}
        pp = os.path.join(os.path.dirname(cfg["paths.catalog"]), "pins.csv")
        if os.path.isfile(pp):
            with open(pp, encoding="utf-8", newline="") as fh:
                self.pins = dict((r["id"], r) for r in csv.DictReader(fh))
        self._probe = None

    @staticmethod
    def _capture(cmd):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            return r.returncode, (r.stdout or "") + (r.stderr or "")
        except Exception as e:                               # noqa: BLE001
            return 1, "%s: %s" % (type(e).__name__, e)

    # -- what this tree is, and what it will be ------------------------------

    def refusal(self):
        if os.path.isfile(os.path.join(self.root, "MANIFEST.csv")):
            return ("%s has a MANIFEST.csv: it is an established archive, with its own "
                    "settings and history. setup builds a NEW one: clone the "
                    "repository into an empty folder and run it there" % self.root)
        if sys.version_info < (3, 11):
            return ("Python %s cannot read the ark.toml setup writes. Install Python "
                    "3.12 (python.org) and run setup with it" % platform.python_version())
        return None

    def ports_refusal(self):
        port = lambda k: self.cfg["ports." + k]               # noqa: E731
        h = self.health(port("node")) if self.busy(port("node")) else None
        if h is not None:
            theirs = os.path.normcase(os.path.abspath(str(h.get("archive_root", ""))))
            if theirs != os.path.normcase(os.path.abspath(self.root)):
                return ("a node serving %s is already on port %d. Stop it first "
                        "(python bin/ark.py down, in that tree), or move this tree's "
                        "ports in ark.toml" % (h.get("archive_root") or "another archive",
                                               port("node")))
            return None
        for k in ("archive", "node", "primary"):
            if self.busy(port(k)):
                return ("port %d (%s) is in use, and not by this tree's node. Stop "
                        "whatever holds it, or move this tree's ports in ark.toml"
                        % (port(k), k))
        return None

    def profile(self):
        """The model profile: named, or the largest this card holds. None when
        there is no NVIDIA card to ask."""
        if not hasattr(self, "_profile"):
            mib = self.vram() if callable(self.vram) else self.vram
            self._vram_mib = mib
            if self.models_arg:
                self._profile = profile(self.models_arg)
            else:
                self._profile = pick_profile(mib)
        return self._profile

    def model_files(self):
        """{role: absolute file} the node will load: from ark.toml when there is
        one, else from the profile setup would write (cross-check off)."""
        if self.cfg.file:
            return dict((r, self.cfg["models.%s.file" % r]) for r in ("primary", "crosscheck")
                        if self.cfg["models.%s.enabled" % r])
        pr = self.profile()
        if not pr:
            return {}
        return {"primary": os.path.join(self.root, *pr["primary"]["file"].split("/"))}

    def row_for(self, path):
        want = os.path.normcase(os.path.abspath(path))
        for r in self.cat:
            if os.path.normcase(os.path.abspath(fetch_dest(r, self.root))) == want:
                return r
        return None

    def rows(self):
        """(rows to fetch, problems). The starter rows less its own model file,
        which is the 16 GB card's; the models come from the settings instead."""
        out = [r for r in self.cat if r["profile"] == "starter" and r["kind"] != "gguf"
               and (r["kind"] != "software" or self.windows)]
        bad = []
        for role, f in sorted(self.model_files().items()):
            r = self.row_for(f)
            if r is None:
                bad.append("the %s model %s is not a catalog row; put the file there "
                           "yourself, or name one the catalog has in ark.toml" % (role, f))
            elif r not in out:
                out.append(r)
        return out, bad

    def zim_rows(self):
        return [r for r in self.rows()[0] if r["kind"] == "zim"]

    def here(self, r):
        """present, partial, missing or CONFLICT, for one row."""
        d = fetch_dest(r, self.root)
        if r["file"].endswith("/"):
            if not os.path.isdir(d):
                return "missing"
            n, part = 0, False
            for _a, _ds, fs in os.walk(d):
                for f in fs:
                    if f.endswith(".part"):
                        part = True
                    elif not f.startswith("CHECKSUMS"):
                        n += 1
            want = (self.pins.get(r["id"]) or {}).get("fetch_files", "")
            if part or (want.isdigit() and n < int(want)):
                return "partial"
            return "present" if n else "missing"
        if os.path.exists(d):
            want = int(r["bytes"]) if (r["bytes"] or "").isdigit() else None
            return "present" if want in (None, os.path.getsize(d)) else "CONFLICT"
        return "partial" if os.path.exists(d + ".part") else "missing"

    def venv_python(self):
        v = self.cfg["paths.venv"]
        return os.path.join(v, "Scripts", "python.exe") if self.windows \
            else os.path.join(v, "bin", "python")

    def binaries(self):
        """{what: path} the node runs."""
        e = (lambda p: p + ".exe" if self.windows and not p.lower().endswith(".exe")
             else p)
        ks = e(self.cfg["paths.kiwix_serve"])
        return {"llama-server": os.path.join(self.cfg["paths.llama"], e("llama-server")),
                "kiwix-serve": ks,
                "kiwix-manage": os.path.join(os.path.dirname(ks), e("kiwix-manage"))}

    def scope_path(self):
        return os.path.join(self.ix, "scope-starter.txt")

    # -- the state of each step -----------------------------------------------

    def st_settings(self):
        if self.cfg.file:
            return "current", "%s: primary %s, cross-check %s" % (
                os.path.basename(self.cfg.file), self.cfg["models.primary.name"],
                "on" if self.cfg["models.crosscheck.enabled"] else "off")
        pr = self.profile()
        mib = self._vram_mib
        if not pr and mib is None:
            return "blocked", ("no NVIDIA card found: nvidia-smi did not answer. If "
                               "there is one, install NVIDIA's driver for it; or name "
                               "a profile: --models 8gb, 12gb, 16gb or 24gb")
        if not pr:
            return "blocked", ("this NVIDIA card has %s MiB, and the smallest profile "
                               "(%s, %s) needs about %s MiB on the card. To try anyway: "
                               "--models %s" % (format(mib, ","), PROFILES[0]["name"],
                                                PROFILES[0]["primary"]["name"],
                                                format(smallest_need_mib(), ","),
                                                PROFILES[0]["name"]))
        return "due", "no ark.toml: will write profile %s (%s), cross-check off%s" % (
            pr["name"], pr["primary"]["name"],
            "; this card: %s MiB" % format(mib, ",") if mib else "")

    def st_fetch(self):
        rows, bad = self.rows()
        if bad:
            return "blocked", bad[0]
        if not rows:
            return "blocked", "the catalog has no starter rows"
        todo = [r for r in rows if self.here(r) != "present"]
        conflict = [r for r in todo if self.here(r) == "CONFLICT"]
        if conflict:
            return "blocked", ("%s is there and is not the catalog's file (wrong size). "
                               "Move it aside and run again" % fetch_dest(conflict[0], self.root))
        if not todo:
            return "current", "%d rows, %s" % (
                len(rows), human(sum(int(r["bytes"] or 0) for r in rows)))
        return "due", "%d of %d rows to download, %s" % (
            len(todo), len(rows), human(sum(int(r["bytes"] or 0) for r in todo)))

    def st_binaries(self):
        b = self.binaries()
        miss = [k for k, p in b.items() if not os.path.exists(p)]
        if not miss:
            return "current", "llama-server and kiwix-serve in place"
        if not self.windows:
            return "blocked", ("the starter's binaries are Windows builds. Put %s at %s "
                               "yourself (or set its path in ark.toml)"
                               % (miss[0], b[miss[0]]))
        return "due", "missing: %s" % ", ".join(miss)

    def probe(self, fresh=False):
        if self._probe is None or fresh:
            rc, text = self.capture([self.venv_python(), "-c", SETUP_PROBE])
            m = re.search(r"ARK-PROBE (\{.*\})", text or "")
            try:
                self._probe = json.loads(m.group(1)) if m else {
                    "missing": ["the environment's python did not run (%s)"
                                % (text or "").strip()[-160:]], "cuda": False}
            except ValueError:
                self._probe = {"missing": ["unreadable probe output"], "cuda": False}
        return self._probe

    def st_venv(self, fresh=False):
        if not os.path.exists(self.venv_python()):
            return "due", "no environment at %s" % self.cfg["paths.venv"]
        pb = self.probe(fresh)
        # ONCE MORE BEFORE GIVING UP, 2026-10-03. On a fresh Windows laptop the
        # check right after pip finished said torch would not import; the same
        # import, run by hand a minute later, worked and saw the GPU. A first load
        # of hundreds of new DLLs, while Windows Defender is still scanning them,
        # can fail once. So a failed check after an install waits and asks again,
        # and the message carries the error itself, not only its type.
        if pb.get("missing") and fresh:
            self.sleep(8)
            pb = self.probe(True)
        if pb.get("missing"):
            text = "; ".join(pb["missing"][:2])
            if "DLL load failed" in text or "WinError 126" in text:
                text += (". On Windows this usually means the Microsoft Visual C++ "
                         "Redistributable (x64) is missing: install it from "
                         "https://aka.ms/vs/17/release/vc_redist.x64.exe and run "
                         "setup again")
            return "due", "will not import: %s" % text
        if not pb.get("cuda") and self.windows:
            return "due", ("torch %s does not see an NVIDIA GPU: a CPU build, which "
                           "would embed the index in hours" % pb.get("torch", "?"))
        return "current", "Python %s, torch %s%s" % (
            pb.get("python", "?"), pb.get("torch", "?"),
            ", CUDA on %s" % pb["gpu"] if pb.get("cuda") else ", no CUDA")

    def built(self):
        """(built, wanted) scope paths, from sources.json and the files beside it."""
        want = ["%s/%s" % (r["shelf"], r["file"]) for r in self.zim_rows()]
        reg = _ix_registry(self.ix) or {}
        have = set()
        for a in reg.get("artifacts", {}).values():
            slug = re.sub(r"[^A-Za-z0-9._-]", "_", a.get("file", ""))
            if os.path.exists(os.path.join(self.ix, "chunks", slug + ".jsonl")) and \
                    os.path.exists(os.path.join(self.ix, "vectors", slug + ".f16.npy")):
                have.add("%s/%s" % (a.get("shelf", "").strip("/"), a.get("file", "")))
        return [w for w in want if w in have], want

    def st_build(self):
        done, want = self.built()
        if not want:
            return "blocked", "no ZIM rows in the starter"
        if len(done) == len(want):
            return "current", "%d of %d ZIM files indexed" % (len(done), len(want))
        return "due", "%d of %d ZIM files indexed" % (len(done), len(want))

    def st_index(self):
        if _ix_registry(self.ix) is None:
            return "due", "nothing built yet"
        due = [(s, st) for s, st, _w in index_plan(self.ix) if st not in ("current", "n/a")]
        if not due:
            return "current", "pq, bm25, provenance and rehash current"
        return "due", ", ".join("%s %s" % d for d in due)

    def st_library(self):
        lib = self.cfg["paths.library"]
        if not os.path.exists(lib):
            return "due", "no %s" % os.path.basename(lib)
        with open(lib, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        miss = [r["file"] for r in self.zim_rows() if os.path.basename(r["file"]) not in text]
        if miss:
            return "due", "%s does not list %s" % (os.path.basename(lib), miss[0])
        return "current", "%d books" % text.count("<book ")

    def st_kiwix(self):
        port = self.cfg["ports.archive"]
        return ("current", "listening on %d" % port) if self.busy(port) else \
            ("due", "not running")

    def st_books(self):
        p = os.path.join(self.ix, "kiwix-books.json")
        if not os.path.exists(p):
            return "due", "no kiwix-books.json"
        try:
            with open(p, encoding="utf-8") as fh:
                d = json.load(fh)
        except (OSError, ValueError):
            return "due", "kiwix-books.json unreadable"
        lib = d.get("from_library")
        if not d.get("verified_against"):
            return "due", "keys recorded from the library file, not from a server"
        if lib and os.path.exists(lib) and (
                int(os.stat(lib).st_mtime) != d.get("library_mtime")
                or os.stat(lib).st_size != d.get("library_size")):
            return "due", "library.xml was rebuilt after the keys were read"
        n = sum(1 for a in ((_ix_registry(self.ix) or {}).get("artifacts") or {}).values()
                if a.get("kiwix_book"))
        if len(d.get("books") or {}) < n:
            return "due", "%d of %d books verified" % (len(d.get("books") or {}), n)
        return "current", "%d book keys, read from %s" % (len(d["books"]), d["verified_against"])

    def st_node(self):
        h = self.health(self.cfg["ports.node"]) if self.busy(self.cfg["ports.node"]) else None
        if not h:
            return "due", "the node is not answering"
        if self.cfg["models.primary.enabled"] and not self.busy(self.cfg["ports.primary"]):
            return "due", "the node is up, the primary model is not"
        return "current", "node and primary model answering"

    def st_ask(self):
        return "due", "always asked: it is the check"

    def states(self):
        return dict((s, getattr(self, "st_" + s)) for s in SETUP_STEPS)

    # -- what each step does ---------------------------------------------------

    def do_settings(self):
        pr = self.profile()
        path = self.environ.get("ARK_CONFIG") or os.path.join(self.root, "ark.toml")
        cc = os.path.join(self.root, *pr["crosscheck"]["file"].split("/"))
        r = self.row_for(cc)
        how = ("python bin/ark.py fetch %s   (%s)" % (r["id"], human(r["bytes"]))
               if r else "")
        text = disable_crosscheck(profile_toml(pr), how)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        self.out("  wrote %s: profile %s, %s on the GPU" % (path, pr["name"],
                                                         pr["primary"]["name"]))
        self.reload()
        return 0

    def reload(self):
        self.cfg = load_settings(environ=dict(self.environ, ARK_ROOT=self.root))

    def do_fetch(self):
        rows, _bad = self.rows()
        todo = [r for r in rows if self.here(r) in ("missing", "partial")]
        need = sum(int(r["bytes"] or 0) for r in todo)
        free = self.free(self.root)
        if free is not None and free < need + SETUP_MARGIN:
            self.out("  REFUSED: %s to download and about %s more for the environment "
                     "and the index; %s free here" % (human(need), human(SETUP_MARGIN),
                                                      human(free)))
            return 2
        ns = argparse.Namespace(all_files=False, verify=False, accept_newer=False)
        bad = 0
        for r in todo:
            self.out("\n  %s  (%s)" % (r["id"], human(r["bytes"])))
            try:
                st = fetch_row(r, self.root, ns)
            except urllib.error.HTTPError as e:
                st = ("FAILED", "HTTP %d from %s" % (e.code, e.url))
            except CertificateRefused as e:
                self.out("    FAILED - %s" % e)
                self.out("\n  Stopped: every other download would be refused the same way.")
                return 1
            except (OSError, ValueError) as e:
                st = ("FAILED", "%s: %s" % (type(e).__name__, e))
            self.out("    %s - %s" % st)
            if st[0] not in ("fetched", "present", "SUBSTITUTED"):
                bad += 1
        return 1 if bad else 0

    def do_binaries(self):
        dests = {"llama": self.cfg["paths.llama"],
                 "kiwix": os.path.dirname(self.binaries()["kiwix-serve"])}
        n = 0
        for r in self.rows()[0]:
            t = _zip_target(r["file"])
            if not t:
                continue
            z = fetch_dest(r, self.root)
            w, k = extract_zip(z, dests[t])
            n += 1
            self.out("  %-44s -> %s  (%d written, %d already there)"
                     % (os.path.basename(z), dests[t], w, k))
        return 0 if n else 1

    def do_venv(self):
        vpy = self.venv_python()
        node_req = os.path.join(self.root, "13-ark-node", "requirements", "node.txt")
        idx_req = os.path.join(self.root, "13-ark-node", "requirements", "index.txt")
        treq, tidx = requirements_torch(node_req)
        cmds = []
        if not os.path.exists(vpy):
            cmds.append([self.python, "-m", "venv", self.cfg["paths.venv"]])
        cmds.append([vpy, "-m", "pip", "install", "--upgrade", "pip"])
        if treq and tidx:
            cmds.append([vpy, "-m", "pip", "install", treq, "--index-url", tidx])
        cmds.append([vpy, "-m", "pip", "install", "-r", idx_req])
        for c in cmds:
            self.out("\n  running  %s\n" % shown(c))
            rc = self.run(c)
            if rc != 0:
                return rc
        return 0

    def write_scope(self):
        os.makedirs(self.ix, exist_ok=True)
        lines = ["# The starter kit's index scope, written by `ark.py setup` from the",
                 "# catalog's starter rows. One path per line, relative to the archive.",
                 "# Read by: python bin/index-build.py --all --scope <this file>", ""]
        lines += ["%s/%s" % (r["shelf"], r["file"]) for r in self.zim_rows()]
        with open(self.scope_path(), "w", encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(lines) + "\n")

    def do_build(self):
        self.write_scope()
        return self.run([self.venv_python(), os.path.join(self.root, "bin", "index-build.py"),
                         "--all", "--scope", self.scope_path(), "--resume"])

    def do_index(self):
        return self.run([self.python, self.ark, "index"])

    def do_library(self):
        return self.run([self.venv_python(), os.path.join(self.root, "bin", "kiwix-library.py"),
                         "--kiwix-manage", self.binaries()["kiwix-manage"]])

    def do_kiwix(self):
        rc = self.run([self.python, self.ark, "up", "archive"])
        return rc if rc else self.wait(lambda: self.busy(self.cfg["ports.archive"]), 30)

    def do_books(self):
        return self.run([self.venv_python(), os.path.join(self.root, "bin", "kiwix-library.py"),
                         "--verify-against", "http://localhost:%d" % self.cfg["ports.archive"]])

    def do_node(self):
        only = ["node"] + (["primary"] if self.cfg["models.primary.enabled"] else [])
        rc = self.run([self.python, self.ark, "up"] + only)
        if rc:
            return rc
        # The model reads 15 GB off disk before it listens; `up` waits 90 s.
        return self.wait(lambda: self.st_node()[0] == "current", 600)

    def wait(self, ok, secs, step=2.0):
        t0 = time.time()
        while time.time() - t0 < secs:
            if ok():
                return 0
            time.sleep(step)
        return 0 if ok() else 1

    def ask_one(self, q, timeout=1200):
        url = "http://127.0.0.1:%d/api/answer?%s" % (
            self.cfg["ports.node"], urllib.parse.urlencode({"q": q}))
        t0 = time.time()
        with urllib.request.urlopen(url, timeout=timeout) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
        d["_wall"] = round(time.time() - t0, 1)
        return d

    def do_ask(self):
        bad = 0
        self.answers = []
        for lang, q in SETUP_QUESTIONS:
            self.out("\n  [%s] %s" % (lang, q))
            try:
                d = self.ask_one(q)
            except Exception as e:                           # noqa: BLE001
                self.out("    FAILED: %s: %s" % (type(e).__name__, e))
                bad += 1
                continue
            s = summarize_answer(d)
            self.answers.append((lang, s))
            for line in s["lines"]:
                self.out("    " + line)
            if not s["ok"]:
                bad += 1
        return 1 if bad else 0


def summarize_answer(d):
    """{'ok', 'lines'} from one /api/answer payload: what a person needs to see
    to believe the first answer, and nothing that scrolls the rest away."""
    ret = d.get("retrieval") or {}
    res = ret.get("results") or []
    a = d.get("answer") or {}
    lines = ["%s s; %d passages%s" % (
        d.get("_wall", d.get("seconds", "?")), len(res),
        "; retrieval %s" % ret.get("mode_ran", ret.get("mode", ""))
        if ret.get("mode_ran") or ret.get("mode") else "")]
    if ret.get("degraded"):
        lines.append("degraded: %s" % str(ret["degraded"])[:160])
    if not a:
        lines.append("NO ANSWER: %s" % (d.get("model_down") or "the model did not answer"))
        return {"ok": False, "lines": lines, "cited": 0, "grounding": None}
    cited = a.get("cited") or []
    lines.append("grounding %s; cites %s" % (a.get("grounding"),
                                            ", ".join("[%s]" % c for c in cited) or "nothing"))
    for c in cited[:3]:
        try:
            r = res[int(c) - 1]
            ci = r.get("citation") or {}
            t = ci.get("title") if isinstance(ci, dict) else ci
            lines.append("  [%s] %s" % (c, str(t or r.get("title") or "")[:100]))
        except (ValueError, IndexError, AttributeError):
            pass
    text = " ".join(str(a.get("text") or "").split())
    lines.append(text[:420] + (" ..." if len(text) > 420 else ""))
    return {"ok": bool(res) and bool(text), "lines": lines, "cited": len(cited),
            "grounding": a.get("grounding")}


def setup_run(S, plan=False, out=print, clock=time.time):
    """Each step: judged, run if due, judged again. Stops at the first failure
    or blocked step. Returns (code, [(step, outcome, seconds)])."""
    done = []
    states = S.states()
    for step in SETUP_STEPS:
        st, why = states[step]()
        if plan:
            out("  %-9s %-8s %s" % (step, st, why))
            continue
        if st == "blocked":
            out("\n  %-9s BLOCKED: %s" % (step, why))
            done.append((step, "BLOCKED", 0))
            return 2, done
        if st in ("current", "n/a"):
            out("\n  %-9s %s: %s" % (step, st, why))
            done.append((step, st, 0))
            continue
        out("\n  %-9s %s: %s\n  %-9s %s" % (step, st, why, "", SETUP_ABOUT[step]))
        t0 = clock()
        try:
            rc = getattr(S, "do_" + step)()
        except Exception as e:                               # noqa: BLE001
            out("  %s: %s: %s" % (step, type(e).__name__, e))
            rc = 1
        secs = clock() - t0
        if rc != 0:
            done.append((step, "FAILED", secs))
            out("\n  %s FAILED after %.0f s. Nothing after it was run. Fix what it "
                "says and run setup again: finished steps are not repeated." % (step, secs))
            return 1, done
        if step != "ask":
            st2, why2 = (S.st_venv(fresh=True) if step == "venv"
                         else states[step]())
            if st2 not in ("current", "n/a"):
                done.append((step, "ran, still " + st2, secs))
                out("\n  %s ran (%.0f s) and is still %s: %s. Stopping here."
                    % (step, secs, st2, why2))
                return 1, done
        done.append((step, "done", secs))
    return 0, done


def do_setup(args):
    if args.profile != "starter":
        print("\n  setup knows the starter profile. For the whole catalog, after a "
              "starter setup:\n    python bin/ark.py fetch --list --profile full\n")
        return 2
    cfg = load_settings()
    try:
        cat = load_catalog(cfg["paths.catalog"])
    except FileNotFoundError as e:
        print("\n  %s\n" % e)
        return 2
    S = Setup(ROOT, cfg, cat, models=args.models)
    why = S.refusal() or (None if args.plan else S.ports_refusal())
    if why:
        print("\n  REFUSED: %s.\n  Nothing was changed.\n" % why)
        return 2
    if args.models and not profile(args.models):
        print("\n  --models takes 8gb, 12gb, 16gb or 24gb\n")
        return 2
    print("\n  setup --profile starter  into %s\n" % ROOT)
    t0 = time.time()
    if args.plan:
        setup_run(S, plan=True)
        rows, _bad = S.rows()
        print("\n  rows: %s" % ", ".join(r["id"] for r in rows))
        print("  plan only; nothing was changed. Each step, when due:")
        for s in SETUP_STEPS:
            print("    %-9s %s" % (s, SETUP_ABOUT[s]))
        print("")
        return 0
    code, done = setup_run(S)
    total = time.time() - t0
    lines = ["  %-9s %-22s %s" % ("STEP", "OUTCOME", "TIME")]
    for s, o, secs in done:
        lines.append("  %-9s %-22s %s" % (s, o, "%d min %02d s" % divmod(int(secs), 60)
                                         if secs else ""))
    lines.append("  %-9s %-22s %d min %02d s" % ("total", "", *divmod(int(total), 60)))
    for lang, s in getattr(S, "answers", []):
        lines.append("\n  ask %s: grounding %s, %d cited" % (lang, s["grounding"], s["cited"]))
        lines += ["    " + x for x in s["lines"]]
    print("\n" + "\n".join(lines))
    try:
        with open(os.path.join(ROOT, "setup-report.txt"), "a", encoding="utf-8",
                  newline="\n") as fh:
            fh.write("%s  setup --profile starter  exit %d\n%s\n\n"
                     % (time.strftime("%Y-%m-%d %H:%M:%S"), code, "\n".join(lines)))
    except OSError:
        pass
    if code == 0:
        print("\n  Done. The node is at http://localhost:%d . Ask it anything; the\n"
              "  starter kit covers water, medicine and post-disaster care.\n"
              "  python bin/ark.py status   says what is running\n"
              "  python bin/ark.py down     stops it\n" % S.cfg["ports.node"])
    return code


# ---------------------------------------------------------------------------
# REHASH AND VERIFY WITHOUT BASH, 2026-10-01. bin/rehash.sh and bin/verify.sh
# are the archive's integrity tools, and they need bash and sha256sum: Git Bash
# on Windows, which a person who has only installed Python does not have. These
# do the same jobs in standard-library Python, and the shell scripts stay, for
# the Linux node and for anyone following the printed instructions.
#
# THE SAME FILE, BYTE FOR BYTE. `rehash` writes CHECKSUMS.sha256 exactly as
# rehash.sh does on the same machine, so the two can be used interchangeably and
# switching never churns a manifest:
#   - the same files: everything under the directory except CHECKSUMS.sha256*,
#     library.xml, anything inside __pycache__/ or "Claude outputs"/, and
#     symbolic links (find -type f does not follow them);
#   - in the same order: find's, which is each directory's entries in the order
#     the file system returns them, descending into a subdirectory where it is
#     met. os.walk would list a directory's files before its subdirectories and
#     reorder every shelf with folders in it;
#   - in the same form: `<sha256> *./path` on Windows, where Git Bash's
#     sha256sum works in binary mode, and `<sha256>  ./path` elsewhere, LF
#     line endings either way.
# Then the same report: old -> new file count, listed files that are missing,
# and the MANIFEST.csv (directory) row checked for its file count and bytes.
# `verify` reads both forms, so a manifest written on either system checks on
# the other.

_SUM_SKIP_NAMES = ("library.xml",)
_SUM_SKIP_DIRS = ("__pycache__", "Claude outputs")


def sum_files(d):
    """Relative paths, './'-prefixed and '/'-separated, in find's order."""
    out = []

    def walk(path, rel):
        try:
            entries = list(os.scandir(path))
        except OSError:
            return
        for e in entries:
            r = rel + "/" + e.name
            if e.is_dir(follow_symlinks=False):
                if e.name not in _SUM_SKIP_DIRS:
                    walk(e.path, r)
            elif e.is_file(follow_symlinks=False):
                if e.name.startswith("CHECKSUMS.sha256") or e.name in _SUM_SKIP_NAMES:
                    continue
                out.append(r)
    walk(d, ".")
    return out


def sha256_file(path, progress=None):
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            b = fh.read(8 << 20)
            if not b:
                break
            h.update(b)
            if progress:
                progress(len(b))
    return h.hexdigest()


def sum_line(digest, rel):
    return "%s%s%s\n" % (digest, " *" if WINDOWS else "  ", rel)


def read_sums(path):
    """[(sha256, './rel')] from a CHECKSUMS.sha256 in either form."""
    out = []
    with open(path, "rb") as fh:
        for raw in fh.read().decode("utf-8", "replace").splitlines():
            line = raw.rstrip("\r")
            esc = line.startswith("\\")
            if esc:
                line = line[1:]
            m = re.match(r"^([0-9a-fA-F]{64}) [ *](.+)$", line)
            if not m:
                continue
            rel = m.group(2)
            if esc:
                rel = rel.replace("\\n", "\n").replace("\\\\", "\\")
            out.append((m.group(1).lower(), rel))
    return out


class _Progress(object):
    """One line, rewritten, while a directory is hashed; silent when not a tty."""

    def __init__(self, label, total):
        self.label, self.total, self.done, self.last = label, total, 0, 0.0
        self.on = sys.stdout.isatty() and total > (1 << 30)

    def __call__(self, n):
        self.done += n
        if self.on and time.time() - self.last > 1:
            self.last = time.time()
            sys.stdout.write("\r  %s %s of %s   " % (self.label, human(self.done),
                                                    human(self.total)))
            sys.stdout.flush()

    def clear(self):
        if self.on:
            sys.stdout.write("\r" + " " * 70 + "\r")


def rehash_dir(d, root, out=print):
    """rehash.sh <d>, in Python. Returns its exit code."""
    full = d if os.path.isabs(d) else os.path.join(root, d)
    if not os.path.isdir(full):
        out("no such directory: %s" % d)
        return 1
    cs = os.path.join(full, "CHECKSUMS.sha256")
    old = 0
    if os.path.isfile(cs):
        with open(cs, "rb") as fh:
            old = sum(1 for ln in fh.read().split(b"\n") if ln)
    files = sum_files(full)
    sizes = [os.path.getsize(os.path.join(full, *r[2:].split("/"))) for r in files]
    prog = _Progress("hashing %s:" % d, sum(sizes))
    lines = [sum_line(sha256_file(os.path.join(full, *r[2:].split("/")), prog), r)
             for r in files]
    prog.clear()
    tmp = cs + ".ark-tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("".join(lines))
    os.replace(tmp, cs)
    new = len(lines)
    out("rehashed %s: %d -> %d files%s" % (d, old, new,
                                          " (%+d)" % (new - old) if old else ""))
    missing = [r for _s, r in read_sums(cs)
               if not os.path.exists(os.path.join(full, *r[2:].split("/")))]
    for r in missing:
        out("  MISSING: %s" % r)
    if missing:
        out("  ERROR: %d entr(ies) reference files that do not exist." % len(missing))
        return 1
    out("  all listed files present.")
    total = sum(sizes)
    key = os.path.basename(os.path.normpath(full))
    man = os.path.join(root, "MANIFEST.csv")
    rows = []
    if os.path.isfile(man):
        with open(man, encoding="utf-8", errors="replace") as fh:
            rows = fh.read().split("\n")
    row = next((r for r in rows if r.startswith(key + ",(directory),")), None)
    if row is None:
        # A clone has no MANIFEST.csv at all, and a sentence about its missing
        # row is about an archive that is not this one.
        if rows and not any(r.startswith(key + ",") or r.startswith(key + "/")
                            for r in rows):
            out("\n  MANIFEST.csv has no row of any kind for %s. Measured %d files, "
                "%d bytes." % (key, new, total))
        return 0
    rec_bytes = row.split(",")[3] if len(row.split(",")) > 3 else ""
    m = re.search(r"(\d+) files", row)
    rec_files = m.group(1) if m else ""
    bad_files = rec_files != str(new)
    bad_bytes = total != 0 and rec_bytes != str(total)
    if bad_files or bad_bytes:
        out("\n  MANIFEST.csv row for %s is stale:" % key)
        if bad_files:
            out("    files: %s recorded, %d actual" % (rec_files or "none found", new))
        if bad_bytes:
            out("    bytes: %s recorded, %d actual" % (rec_bytes or "none found", total))
    return 0


def verify_dirs(root, scope=None, out=print):
    """verify.sh, in Python: every CHECKSUMS.sha256 under the root, or under the
    directories named. Returns (exit code, files checked, failed directories)."""
    found = []

    def walk(path):
        try:
            entries = list(os.scandir(path))
        except OSError:
            return
        for e in entries:
            if e.is_dir(follow_symlinks=False):
                walk(e.path)
            elif e.name == "CHECKSUMS.sha256" and e.is_file(follow_symlinks=False):
                found.append(e.path)
    for s in (scope or [root]):
        walk(s if os.path.isabs(s) else os.path.join(root, s))
    checked, failed = 0, []
    out("\n=== CHECKSUM VERIFICATION - %s ===\n" % time.strftime("%Y-%m-%d"))
    for cs in found:
        d = os.path.dirname(cs)
        label = "./" + os.path.relpath(d, root).replace(os.sep, "/")
        label = "." if label == "./." else label
        entries = read_sums(cs)
        total = 0
        for _s, r in entries:
            try:
                total += os.path.getsize(os.path.join(d, *r[2:].split("/") if r.startswith("./")
                                                      else r.split("/")))
            except OSError:
                pass
        prog = _Progress("verifying %s:" % label, total)
        bad = []
        for want, r in entries:
            p2 = os.path.join(d, *(r[2:] if r.startswith("./") else r).split("/"))
            try:
                got = sha256_file(p2, prog)
            except OSError:
                bad.append("%s: FAILED open or read" % r)
                continue
            if got != want:
                bad.append("%s: FAILED" % r)
        prog.clear()
        if bad:
            out("%s ... FAILED" % label)
            for b in bad:
                out("    " + b)
            failed.append(label)
        else:
            out("%s ... OK (%d files)" % (label, len(entries)))
            checked += len(entries)
    out("\n%d files checked across all directories." % checked)
    if failed:
        out("RESULT: %d director(ies) FAILED. Restore from cold copy before proceeding.\n"
            % len(failed))
        return 1, checked, failed
    out("RESULT: all directories passed.\n")
    return 0, checked, failed


def do_rehash(args):
    code = 0
    for d in args.dirs:
        code = max(code, rehash_dir(d.rstrip("/\\"), ROOT))
    return code


def do_verify(args):
    return verify_dirs(ROOT, args.dirs or None)[0]


def selftest_rehash(check):
    """rehash and verify, against a small archive built here, and against the
    real bin/rehash.sh where bash and sha256sum are on PATH."""
    tmp = tempfile.mkdtemp(prefix="ark-rehash-")
    try:
        def put(rel, data):
            q = os.path.join(tmp, *rel.split("/"))
            os.makedirs(os.path.dirname(q), exist_ok=True)
            with open(q, "wb") as fh:
                fh.write(data)
        put("s/a.txt", b"alpha\n")
        put("s/sub/b.txt", b"bravo\n")
        put("s/zz.txt", b"zulu\n")
        put("s/CHECKSUMS.sha256.old", b"stale")
        put("s/library.xml", b"<library/>")
        put("s/__pycache__/x.pyc", b"\0")
        put("s/Claude outputs/y.md", b"scratch")
        linked = False
        try:
            os.symlink(os.path.join(tmp, "s", "a.txt"), os.path.join(tmp, "s", "link.txt"))
            linked = True
        except (OSError, NotImplementedError, AttributeError):
            pass
        put("MANIFEST.csv", b'category,filename,source_url,expected_size_bytes\n'
                            b's,(directory),,999,directory,,,PROJECT,"2 files"\n')
        said = []
        code = rehash_dir("s", tmp, out=said.append)
        got = sorted(r for _h, r in read_sums(os.path.join(tmp, "s", "CHECKSUMS.sha256")))
        check("rehash: lists files only, skipping manifests, library.xml, caches, links",
              code == 0 and got == ["./a.txt", "./sub/b.txt", "./zz.txt"],
              "%d files%s" % (len(got), "" if linked else ", no symlink here"))
        check("rehash: a stale MANIFEST.csv row is named, files and bytes",
              "MANIFEST.csv row for s is stale:" in "\n".join(said)
              and "    files: 2 recorded, 3 actual" in said
              and "    bytes: 999 recorded, 17 actual" in said, "files and bytes")

        bash, sums = shutil.which("bash"), shutil.which("sha256sum")
        if bash and sums:
            os.makedirs(os.path.join(tmp, "bin"))
            shutil.copy(os.path.join(HERE, "rehash.sh"), os.path.join(tmp, "bin", "rehash.sh"))
            mine = open(os.path.join(tmp, "s", "CHECKSUMS.sha256"), "rb").read()
            r = subprocess.run([bash, os.path.join(tmp, "bin", "rehash.sh").replace("\\", "/"),
                                "s"], capture_output=True, text=True)
            theirs = open(os.path.join(tmp, "s", "CHECKSUMS.sha256"), "rb").read()
            check("rehash: writes what rehash.sh writes, byte for byte",
                  r.returncode == 0 and mine == theirs,
                  "identical" if mine == theirs else "differs: %r / %r" % (mine[:40], theirs[:40]))
        else:
            check("rehash: writes what rehash.sh writes, byte for byte", True,
                  "no bash here to compare with")

        said = []
        code, n, failed = verify_dirs(tmp, ["s"], out=said.append)
        check("verify: a fresh manifest passes", code == 0 and n == 3, "%d files" % n)
        put("s/zz.txt", b"zulu!\n")
        os.remove(os.path.join(tmp, "s", "sub", "b.txt"))
        said = []
        code, n, failed = verify_dirs(tmp, ["s"], out=said.append)
        check("verify: a changed byte and a missing file fail, each by name",
              code == 1 and "    ./zz.txt: FAILED" in said
              and "    ./sub/b.txt: FAILED open or read" in said, "%d failed" % len(failed))
        import hashlib
        put("t/c.txt", b"charlie\n")
        h = hashlib.sha256(b"charlie\n").hexdigest()
        put("t/CHECKSUMS.sha256", ("%s  ./c.txt\n" % h).encode())
        put("u/c.txt", b"charlie\n")
        put("u/CHECKSUMS.sha256", ("%s *./c.txt\r\n" % h).encode())
        code, n, _f = verify_dirs(tmp, ["t", "u"], out=lambda *a: None)
        check("verify: reads both forms, written on Linux or on Windows",
              code == 0 and n == 2, "%d files" % n)
        check("index: the rehash step no longer needs bash",
              index_command("rehash", sys.executable)[0] == sys.executable, "python")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def selftest_profiles(check):
    """The profiles, as ark.toml files this script would read, and the log parser
    `measure` depends on."""
    tmp = tempfile.mkdtemp(prefix="ark-prof-")
    try:
        ok_all, bad = True, []
        for pr in PROFILES:
            f = os.path.join(tmp, pr["name"] + ".toml")
            with open(f, "w", encoding="utf-8") as fh:
                fh.write(profile_toml(pr))
            st = load_settings(environ={"ARK_ROOT": ROOT}, path=f)
            if any("cannot read TOML" in x for x in st.problems):
                bad.append("no TOML reader")
                continue
            for role in ("primary", "crosscheck"):
                for k in _ROLE_KEYS:
                    want = pr[role][k]
                    got = st["models.%s.%s" % (role, k)]
                    if k == "file":
                        want = os.path.join(ROOT, *want.split("/"))
                    if got != want:
                        ok_all = False
                        bad.append("%s %s.%s" % (pr["name"], role, k))
            if st.problems:
                ok_all = False
                bad.append(st.problems[0][:40])
        check("profiles: each profile's ark.toml loads clean and sets what it names",
              ok_all, ", ".join(bad[:2]) or "%d profiles" % len(PROFILES))

        def family(r):
            return re.match(r"[a-z]+", r["name"].lower()).group(0)
        check("profiles: every pair is two model families",
              all(family(pr["primary"]) != family(pr["crosscheck"]) for pr in PROFILES),
              " ".join("%s/%s" % (family(pr["primary"]), family(pr["crosscheck"]))
                       for pr in PROFILES))
        d = dict((k, dflt) for k, _v, dflt, _k, _h in SETTINGS)
        p16 = profile("16gb")
        check("profiles: the 16gb profile is exactly the measured defaults",
              all(p16[r][k] == d["models.%s.%s" % (r, k)]
                  for r in ("primary", "crosscheck") for k in _ROLE_KEYS),
              "M18 pair")
        picks = [(m, (pick_profile(m) or {}).get("name")) for m in
                 (16375, 8188, 12282, 24564, 6144, 4096, None)]
        check("profiles: a card picks the largest profile it holds",
              [x[1] for x in picks] == ["16gb", "8gb", "12gb", "24gb", "8gb", None, None],
              " ".join("%s:%s" % x for x in picks[:4]))
        check("profiles: a 6 GB card gets the smallest profile; 4 GB is too small",
              picks[4][1] == "8gb" and picks[5][1] is None,
              "6144:%s 4096:%s" % (picks[4][1], picks[5][1]))
        try:
            cat = dict((r2["id"], r2) for r2 in load_catalog(CFG["paths.catalog"]))
            miss = [(pr["name"], i) for pr in PROFILES for i in pr["fetch"]
                    if i not in cat]
            files = set("%s/%s" % (cat[i]["shelf"], cat[i]["file"])
                        for pr in PROFILES for i in pr["fetch"] if i in cat)
            unfetched = [pr[r]["file"] for pr in PROFILES for r in ("primary", "crosscheck")
                         if pr[r]["file"] not in files]
            check("profiles: every model a profile names is a catalog row it fetches",
                  not miss and not unfetched, (str(miss[:1]) + str(unfetched[:1]))
                  if miss or unfetched else "all in the catalog")
        except FileNotFoundError:
            check("profiles: every model a profile names is a catalog row it fetches",
                  True, "no catalog here")
        log = ["load_tensors:        CUDA0 model buffer size = 13540.27 MiB",
               "load_tensors:   CPU_Mapped model buffer size =  1067.97 MiB",
               "llama_kv_cache:      CUDA0 KV buffer size =   512.00 MiB",
               "llama_memory_recurrent:      CUDA0 RS buffer size =   573.56 MiB",
               "llama_memory_recurrent:        CPU RS buffer size =    24.94 MiB",
               "sched_reserve:      CUDA0 compute buffer size =   175.16 MiB",
               "sched_reserve:  CUDA_Host compute buffer size =    42.29 MiB",
               "main: server is listening on http://127.0.0.1:8091"]
        v, h, seen = parse_buffers(log)
        check("measure: the 09-07 primary log sums to its measured 14,801 MiB",
              round(v) == 14801 and round(h) == 1135 and len(seen) == 7,
              "VRAM %.2f, host %.2f" % (v, h))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def selftest_index(check):
    """ark.py index against a small 10-index built here: real .npy headers, a
    real provenance and mirror stamp, and a keyword store built by the real
    index-bm25.py, so the stamp it writes is the stamp the plan reads."""
    import shutil as _sh
    import sqlite3
    tmp = tempfile.mkdtemp(prefix="ark-index-")
    try:
        ix = os.path.join(tmp, "10-index")
        for d in ("chunks", "vectors", "pq", ):
            os.makedirs(os.path.join(ix, d))
        files = ["mdwiki_en_all_maxi_2025-11.zim", "wikipedia_en_medicine_maxi_2026-04.zim"]
        with open(os.path.join(ix, "sources.json"), "w", encoding="utf-8") as fh:
            json.dump({"artifacts": {"1": {"file": files[0]}, "2": {"file": files[1]}}}, fh)

        def npy(path, rows, dim=4):
            head = "{'descr': '<f2', 'fortran_order': False, 'shape': (%d, %d), }" % (rows, dim)
            pad = 64 - (10 + len(head) + 1) % 64
            head = (head + " " * pad + "\n").encode("latin-1")
            with open(path, "wb") as fh:
                fh.write(b"\x93NUMPY\x01\x00" + len(head).to_bytes(2, "little") + head
                         + b"\x00" * (rows * dim * 2))

        arts = []
        for i, f in enumerate(files, 1):
            cf = os.path.join(ix, "chunks", f + ".jsonl")
            with open(cf, "w", encoding="utf-8") as fh:
                for k in range(3):
                    fh.write(json.dumps({"id": "%d:0:%d" % (i, k), "text": "clean water %d" % k}) + "\n")
            vf = os.path.join(ix, "vectors", f + ".f16.npy")
            npy(vf, 3)
            arts.append([f, 3, os.path.getsize(vf)])
        with open(os.path.join(ix, "pq", "manifest.json"), "w", encoding="utf-8") as fh:
            json.dump({"index": "ark-pq64.faiss", "stamp": {"artifacts": arts, "total": 6}}, fh)
        with open(os.path.join(ix, "pq", "ark-pq64.faiss"), "wb") as fh:
            fh.write(b"x")

        # the real keyword build, from a copy of the real script
        os.makedirs(os.path.join(tmp, "bin"))
        _sh.copy(os.path.join(HERE, "index-bm25.py"), os.path.join(tmp, "bin", "index-bm25.py"))
        r = subprocess.run([sys.executable, "-B", os.path.join(tmp, "bin", "index-bm25.py"),
                            "--build"], capture_output=True, text=True)
        built = r.returncode == 0
        if not built:                                   # no FTS5 in this Python
            con = sqlite3.connect(os.path.join(ix, "bm25.sqlite3"))
            con.execute("CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT)")
            con.execute("INSERT INTO meta VALUES ('stamp', ?)", (json.dumps(
                dict(bm25_stamp(ix), rows=6, built="fixture")),))
            con.commit()
            con.close()

        prov = os.path.join(ix, "provenance.sqlite3")
        con = sqlite3.connect(prov)
        con.execute("CREATE TABLE artifact (src INTEGER PRIMARY KEY, file TEXT, done INT, jsonl TEXT)")
        con.execute("CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT)")
        for i, f in enumerate(files, 1):
            st = os.stat(os.path.join(ix, "chunks", f + ".jsonl"))
            con.execute("INSERT INTO artifact VALUES (?,?,1,?)", (i, f, f + ".jsonl"))
            con.execute("INSERT INTO meta VALUES (?,?)",
                        ("jsonl:%d" % i, "%d:%d" % (st.st_size, int(st.st_mtime))))
        con.commit()
        con.close()

        def stmp(p2):
            st = os.stat(p2)
            return {"size": st.st_size, "mtime": int(st.st_mtime)}
        with open(os.path.join(ix, "mirror-mdwiki.json"), "w", encoding="utf-8") as fh:
            json.dump({"old": {"jsonl": files[0] + ".jsonl"}, "new": {"jsonl": files[1] + ".jsonl"},
                       "stamp": {"old_jsonl": stmp(os.path.join(ix, "chunks", files[0] + ".jsonl")),
                                 "new_jsonl": stmp(os.path.join(ix, "chunks", files[1] + ".jsonl")),
                                 "provenance": stmp(prov)}, "dnums": [7]}, fh)
        listed = []
        for a, _ds, fs in os.walk(ix):
            for f in fs:
                listed.append(os.path.relpath(os.path.join(a, f), ix).replace(os.sep, "/"))
        cs = os.path.join(ix, "CHECKSUMS.sha256")
        with open(cs, "w", encoding="utf-8") as fh:
            for rel in sorted(listed):
                fh.write("%s *./%s\n" % ("0" * 64, rel))
        later = time.time() + 5
        os.utime(cs, (later, later))

        before = dict((f, os.path.getmtime(os.path.join(ix, f)))
                      for f in ("bm25.sqlite3", "provenance.sqlite3"))
        plan = dict((s, st) for s, st, _w in index_plan(ix))
        check("index: a consistent 10-index plans nothing to run",
              all(v == "current" for v in plan.values()), " ".join(sorted(set(plan.values()))))
        check("index: the plan writes nothing, not even to provenance",
              all(os.path.getmtime(os.path.join(ix, f)) == t for f, t in before.items()),
              "untouched")
        check("index: the keyword store's own build writes the stamp the plan reads",
              built and bm25_state(ix)[0] == "current" and "stamped" in bm25_state(ix)[1],
              "real index-bm25.py" if built else "no FTS5 here: %s" % r.stderr[-60:])

        vf2 = os.path.join(ix, "vectors", files[1] + ".f16.npy")
        npy(vf2, 4)
        os.utime(vf2, (later + 5, later + 5))
        plan = dict((s, st) for s, st, _w in index_plan(ix))
        check("index: more vectors make pq stale, not bm25 or provenance",
              plan["pq"] == "stale" and plan["bm25"] == "current"
              and plan["provenance"] == "current", plan["pq"])
        check("index: a file changed after the last rehash makes the shelf stale",
              plan["rehash"] == "stale", plan["rehash"])

        with open(os.path.join(ix, "chunks", files[0] + ".jsonl"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"id": "1:0:3", "text": "boil it"}) + "\n")
        plan = dict((s, st) for s, st, _w in index_plan(ix))
        check("index: a rewritten chunk file makes bm25, provenance and mirror stale",
              plan["bm25"] == plan["provenance"] == plan["mirror"] == "stale",
              "%s %s %s" % (plan["bm25"], plan["provenance"], plan["mirror"]))

        ix2 = os.path.join(tmp, "ix2")
        os.makedirs(os.path.join(ix2, "chunks"))
        with open(os.path.join(ix2, "sources.json"), "w", encoding="utf-8") as fh:
            json.dump({"artifacts": {"1": {"file": "gutenberg_en.zim"}}}, fh)
        cf2 = os.path.join(ix2, "chunks", "gutenberg_en.zim.jsonl")
        with open(cf2, "w", encoding="utf-8") as fh:
            fh.write("{}\n")
        sqlite3.connect(os.path.join(ix2, "bm25.sqlite3")).close()
        old = time.time() - 100
        os.utime(cf2, (old, old))
        a1 = bm25_state(ix2)
        os.utime(cf2, (time.time() + 100, time.time() + 100))
        a2 = bm25_state(ix2)
        check("index: an unstamped keyword store is judged by date, and says so",
              a1[0] == "current" and "no stamp" in a1[1] and a2[0] == "stale", "%s, %s" % (a1[0], a2[0]))
        check("index: without the mdwiki pair the mirror step is not needed",
              mirror_state(ix2)[0] == "n/a", mirror_state(ix2)[0])

        # the runner, on invented steps
        seq = {"pq": ["stale", "current"], "bm25": ["stale", "stale"],
               "provenance": ["stale"], "mirror": ["stale"], "rehash": ["stale"]}
        calls = []

        def fake(step):
            def f(_ix):
                v = seq[step][0] if len(seq[step]) == 1 else seq[step].pop(0)
                return v, "fixture"
            return f
        states = dict((s, fake(s)) for s in INDEX_STEPS)
        code, done = index_run("x", states=states, quiet=True,
                               command=lambda s, f: [s], run=lambda c: calls.append(c[0]) or
                               (1 if c[0] == "bm25" else 0))
        check("index: a failing step stops the run, named, and nothing after it runs",
              code == 1 and calls == ["pq", "bm25"] and done[-1] == ("bm25", "FAILED"),
              " ".join(calls))
        cur = dict((s, (lambda _ix: ("current", "fixture"))) for s in INDEX_STEPS)
        calls[:] = []
        code, done = index_run("x", force={"mirror"}, states=cur, quiet=True,
                               command=lambda s, f: [s], run=lambda c: calls.append(c[0]) or 0)
        check("index: only forced steps run when everything is current",
              code == 0 and calls == ["mirror"], " ".join(calls) or "none")
        code, _d = index_run("x", node_up=True, states=cur, quiet=True,
                             command=lambda s, f: [s], run=lambda c: 0)
        check("index: refuses while the node is up", code == 2, "refused")
    finally:
        _sh.rmtree(tmp, ignore_errors=True)


def selftest_fetch(check):
    """fetch, against a server on 127.0.0.1 that plays Kiwix, Hugging Face and a
    mirror that lies. No network, no real catalog."""
    import hashlib
    import http.server
    import shutil as _sh
    import threading
    import types
    tmp = tempfile.mkdtemp(prefix="ark-fetch-")
    web, dest = os.path.join(tmp, "web"), os.path.join(tmp, "dest")
    ranges = []

    def put(rel, data):
        path = os.path.join(web, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(data)
        return hashlib.sha256(data).hexdigest()

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            rel = urllib.parse.unquote(self.path.split("?", 1)[0]).lstrip("/")
            path = os.path.join(web, *rel.split("/"))
            if rel.endswith("/") and os.path.isdir(path):
                body = "".join('<a href="%s">%s</a>\n' % (n, n)
                               for n in sorted(os.listdir(path))).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if not os.path.isfile(path):
                self.send_error(404)
                return
            if os.path.isfile(path + ".429"):
                os.replace(path + ".429", path + ".429-done")
                self.send_response(429)
                self.send_header("Retry-After", "0")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            data = open(path, "rb").read()
            start = 0
            rng = self.headers.get("Range")
            if rng:
                ranges.append(rng)
                start = int(rng.split("=")[1].split("-")[0])
                if start >= len(data):
                    self.send_response(416)
                    self.send_header("Content-Range", "bytes */%d" % len(data))
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.send_response(206)
                self.send_header("Content-Range", "bytes %d-%d/%d"
                                 % (start, len(data) - 1, len(data)))
            else:
                self.send_response(200)
            if os.path.isfile(path + ".link"):
                self.send_header("Link", open(path + ".link").read().strip())
            body = data[start:]
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % srv.server_address[1]
    old_hf = os.environ.get("HF_ENDPOINT")
    os.environ["HF_ENDPOINT"] = base
    args = types.SimpleNamespace(verify=False, accept_newer=False, all_files=False)

    def row(i, f, how, url="", sha="", size="", rev=""):
        return {"id": i, "shelf": "02-corpora-core", "file": f, "fetch": how,
                "fetch_url": url, "revision": rev, "sha256": sha, "bytes": str(size),
                "source_page": "https://example.org/", "license_status": "recorded",
                "profile": "starter"}
    try:
        a = b"good zim bytes " * 5000
        sha_a = put("files/good.zim", a)
        r = row("good", "good.zim", "direct", base + "/files/good.zim", sha_a, len(a))
        st = fetch_row(r, dest, args, quiet=True)
        check("fetch: a direct file arrives checked", st == ("fetched", "checked"), st[0])
        st = fetch_row(r, dest, args, quiet=True)
        check("fetch: a second run finds it present", st[0] == "present", st[1])

        put("files/lie.zim", b"not what the catalog pinned " * 100)
        r = row("lie", "lie.zim", "direct", base + "/files/lie.zim", sha_a, 2800)
        st = fetch_row(r, dest, args, quiet=True)
        d = fetch_dest(r, dest)
        check("fetch: a wrong checksum is refused",
              st[0] == "MISMATCH" and not os.path.exists(d)
              and os.path.exists(d + ".bad"), st[0])

        c = b"resumable " * 20000
        sha_c = put("files/resume.zim", c)
        r = row("resume", "resume.zim", "direct", base + "/files/resume.zim", sha_c, len(c))
        d = fetch_dest(r, dest)
        os.makedirs(os.path.dirname(d), exist_ok=True)
        with open(d + ".part", "wb") as fh:
            fh.write(c[:len(c) // 2])
        st = fetch_row(r, dest, args, quiet=True)
        check("fetch: a .part resumes with a Range request",
              st == ("fetched", "checked") and any(x.startswith("bytes=%d-" % (len(c) // 2))
                                                   for x in ranges), str(ranges[-1:]))

        put("files/changed.json", b"x" * 150)
        r = row("changed", "changed.json", "direct", base + "/files/changed.json",
                "2" * 64, 188)
        d = fetch_dest(r, dest)
        st = fetch_row(r, dest, args, quiet=True)
        check("fetch: a file changed upstream is named, not downloaded",
              st[0] == "CHANGED" and "150" in st[1] and not os.path.exists(d)
              and not os.path.exists(d + ".part"), st[0])
        with open(d + ".part", "wb") as fh:
            fh.write(b"x" * 150)
        st = fetch_row(r, dest, args, quiet=True)
        check("fetch: a 416 on resume is read, not reported as a failure",
              st[0] == "CHANGED" and ranges[-1] == "bytes=150-", "%s %s" % (st[0], ranges[-1]))

        d = os.path.join(dest, "02-corpora-core", "mine.zim")
        with open(d, "wb") as fh:
            fh.write(b"someone else's copy")
        r = row("mine", "mine.zim", "direct", base + "/files/good.zim", sha_a, len(a))
        st = fetch_row(r, dest, args, quiet=True)
        check("fetch: a different file already there is left alone",
              st[0] == "CONFLICT" and open(d, "rb").read() == b"someone else's copy", st[0])

        cfg = b'{"hidden_size": 1024}'
        wts = b"W" * 4096
        put("org/repo/resolve/abc123/config.json", cfg)
        sha_w = put("org/repo/resolve/abc123/model.safetensors", wts)
        put("org/repo/resolve/abc123/pytorch_model.bin", b"B" * 4096)
        put("org/repo/resolve/abc123/onnx/model.onnx", b"O" * 10)
        blob = hashlib.sha1(b"blob %d\0" % len(cfg) + cfg).hexdigest()
        api = {"siblings": [
            {"rfilename": "config.json", "size": len(cfg), "blobId": blob},
            {"rfilename": "model.safetensors", "size": len(wts),
             "lfs": {"sha256": sha_w, "size": len(wts)}},
            {"rfilename": "pytorch_model.bin", "size": 4096,
             "lfs": {"sha256": "0" * 64, "size": 4096}},
            {"rfilename": "onnx/model.onnx", "size": 10,
             "lfs": {"sha256": "0" * 64, "size": 10}}]}
        put("api/models/org/repo/revision/abc123", json.dumps(api).encode())
        r = row("repo", "repo/", "hf-repo", "https://huggingface.co/org/repo", rev="abc123")
        st = fetch_row(r, dest, args, quiet=True)
        d = fetch_dest(r, dest)
        got = sorted(os.listdir(d)) if os.path.isdir(d) else []
        check("fetch: a Hugging Face repo, only what the node reads",
              st[0] == "fetched" and got == ["config.json", "model.safetensors"],
              "%s %s" % (st[0], got))
        ox = [("onnx/model.onnx", 10, "sha256", "0"), ("onnx/model_q8.onnx", 5, "sha256", "0"),
              ("voices/af.bin", 3, "sha256", "0"), ("config.json", 2, "git", "0"),
              ("imgs/a.png", 1, "sha256", "0")]
        k, sk, _g = hf_select(ox)
        check("fetch: an ONNX-only repository keeps its onnx/ (it is the model)",
              sorted(f[0] for f in k) == ["config.json", "onnx/model.onnx",
                                          "onnx/model_q8.onnx", "voices/af.bin"]
              and [f[0] for f in sk] == ["imgs/a.png"], " ".join(f[0] for f in k))

        vx, vj = b"V" * 2048, b'{"audio": {"sample_rate": 22050}}'
        put("org/voices/resolve/abc999/en/x.onnx", vx)
        put("org/voices/resolve/abc999/en/x.onnx.json", vj)
        put("org/voices/resolve/abc999/fr/y.onnx", b"Y" * 2048)
        put("api/models/org/voices/revision/abc999", json.dumps({"siblings": [
            {"rfilename": "en/x.onnx", "size": len(vx),
             "lfs": {"sha256": hashlib.sha256(vx).hexdigest(), "size": len(vx)}},
            {"rfilename": "en/x.onnx.json", "size": len(vj),
             "blobId": hashlib.sha1(b"blob %d\0" % len(vj) + vj).hexdigest()},
            {"rfilename": "fr/y.onnx", "size": 2048,
             "lfs": {"sha256": "0" * 64, "size": 2048}}]}).encode())
        r = row("voices", "voices/", "hf-repo", "https://huggingface.co/org/voices",
                rev="abc999")
        r["include"] = "en/x.onnx;en/x.onnx.json"
        st = fetch_row(r, dest, args, quiet=True)
        d = fetch_dest(r, dest)
        got = sorted(os.path.relpath(os.path.join(a, f), d).replace(os.sep, "/")
                     for a, _ds, fs in os.walk(d) for f in fs) if os.path.isdir(d) else []
        check("fetch: a partial repository takes only its include list",
              st[0] == "fetched" and got == ["en/x.onnx", "en/x.onnx.json"],
              "%s %s" % (st[0], got))
        r2 = dict(r, id="voices2", file="voices2/", include="en/x.onnx;de/z.onnx")
        st = fetch_row(r2, dest, args, quiet=True)
        check("fetch: an include the commit does not have fails by name",
              st[0] == "FAILED" and "de/z.onnx" in st[1], st[0])

        sha_new = put("zim/other/book_2025-01.zim", b"newer edition " * 300)
        put("zim/other/book_2025-01.zim.sha256",
            ("%s  book_2025-01.zim\n" % sha_new).encode())
        r = row("book", "book_2024-08.zim", "direct", base + "/zim/other/book_2024-08.zim",
                "1" * 64, 999)
        st = fetch_row(r, dest, args, quiet=True)
        check("fetch: a withdrawn Kiwix edition names the newer one and stops",
              st[0] == "GONE" and "book_2025-01.zim" in st[1], st[0])
        args.accept_newer = True
        st = fetch_row(r, dest, args, quiet=True)
        subs = os.path.join(dest, "fetch-substitutions.csv")
        check("fetch: --accept-newer fetches it, checked, and records it",
              st[0] == "SUBSTITUTED"
              and os.path.exists(os.path.join(dest, "02-corpora-core", "book_2025-01.zim"))
              and os.path.isfile(subs) and "book_2024-08.zim" in open(subs).read(), st[0])

        # PIN. An archive on disk, three repositories on the fake host, and the
        # question fetch --pin answers: which commit is the archive's copy?
        def lfs(name, data):
            return {"rfilename": name, "size": len(data),
                    "lfs": {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}}

        def small(name, data):
            return {"rfilename": name, "size": len(data),
                    "blobId": hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()}

        def revision(repo, c, sibs):
            put("api/models/%s/revision/%s" % (repo, c),
                json.dumps({"sha": c, "siblings": sibs}).encode())

        def commits(repo, rel, cs):
            put(rel or "api/models/%s/commits/main" % repo, json.dumps(
                [{"id": c, "title": "commit " + c[:1], "date": "2026-0%s-01T00:00:00Z"
                  % c[:1]} for c in cs]).encode())

        arch = os.path.join(tmp, "arch")

        def disk(rel, data):
            q = os.path.join(arch, *rel.split("/"))
            os.makedirs(os.path.dirname(q), exist_ok=True)
            with open(q, "wb") as fh:
                fh.write(data)
            return hashlib.sha256(data).hexdigest()

        w1, w2 = b"weights v1 " * 60, b"weights v2 " * 60
        p1, p2 = b'{"stop": ["<|im_end|>"], "temperature": 0.7}', b'{"temperature": 0.7}'
        c1, c2, c3 = "1" * 40, "2" * 40, "3" * 40
        tl, mm = b"{{ a }}\n{{ b }}\n", b"M" * 500
        both = [small("template", tl), lfs("mmproj-F16.gguf", mm)]
        revision("org/pin", c1, [lfs("a.gguf", w1), small("params", p1)] + both)
        revision("org/pin", c2, [lfs("a.gguf", w1), small("params", p2)] + both)
        revision("org/pin", c3, [lfs("a.gguf", w2), small("params", p2)] + both)
        commits("org/pin", None, [c3, c2])
        put("api/models/org/pin/commits/main.link",
            b'</api/models/org/pin/commits-page-2>; rel="next"')
        commits("org/pin", "api/models/org/pin/commits-page-2", [c1])
        pa = row("pa", "a.gguf", "hf-file", "https://huggingface.co/org/pin/resolve/main/a.gguf",
                 disk("01-models/t/a.gguf", w1), len(w1), rev="main")
        pp = row("pp", "params", "hf-file", "https://huggingface.co/org/pin/resolve/main/params",
                 disk("01-models/t/params", p1), len(p1), rev="main")
        crlf = tl.replace(b"\n", b"\r\n")
        pt = row("pt", "template", "hf-file",
                 "https://huggingface.co/org/pin/resolve/main/template",
                 disk("01-models/t/template", crlf), len(crlf), rev="main")
        pm = row("pm", "mmproj-model-F16.gguf", "hf-file",
                 "https://huggingface.co/org/pin/resolve/main/mmproj-model-F16.gguf",
                 disk("01-models/t/mmproj-model-F16.gguf", mm), len(mm), rev="main")

        cf1, cf2, wt = b'{"layers": 2}', b'{"layers": 3}', b"T" * 3000
        r1, r2 = "4" * 40, "5" * 40
        revision("org/prepo", r1, [small("config.json", cf1), lfs("model.safetensors", wt),
                                   lfs("extra.bin", b"x"), small("README.md", b"x\ny\n")])
        revision("org/prepo", r2, [small("config.json", cf2), lfs("model.safetensors", wt)])
        commits("org/prepo", None, [r2, r1])
        disk("01-models/f/prepo/config.json", cf1)
        disk("01-models/f/prepo/README.md", b"x\r\ny\r\n")
        sw = disk("01-models/f/prepo/model.safetensors", wt)
        disk("01-models/CHECKSUMS.sha256", ("%s *./f/prepo/model.safetensors\n" % sw).encode())
        pr = row("pr", "prepo/", "hf-repo", "https://huggingface.co/org/prepo", rev="main")

        m1 = "7" * 40
        revision("org/mixed", m1, [small("README.md", b"new card"), lfs("model.safetensors", wt),
                                   lfs("imgs/a.jpg", b"J1")])
        disk("01-models/f/mixed/README.md", b"old card")
        disk("01-models/f/mixed/model.safetensors", wt)
        disk("01-models/f/mixed/imgs/a.jpg", b"J0")
        px = row("px", "mixed/", "hf-repo", "https://huggingface.co/org/mixed", rev=m1)

        n1 = "6" * 40
        revision("org/nopin", n1, [small("config.json", b"theirs")])
        commits("org/nopin", None, [n1])
        put("api/models/org/nopin/commits/main.429", b"")
        disk("01-models/f/nopin/config.json", b"mine")
        pn = row("pn", "nopin/", "hf-repo", "https://huggingface.co/org/nopin", rev="main")
        for x, sh in ((pa, "01-models/t"), (pp, "01-models/t"), (pr, "01-models/f"),
                      (pn, "01-models/f"), (pt, "01-models/t"), (pm, "01-models/t"),
                      (px, "01-models/f")):
            x["shelf"] = sh
        pins = pin_rows([pa, pp, pr, pn, pt, pm, px], arch, quiet=True)
        g = lambda i, k: pins.get(i, {}).get(k, "")
        check("pin: a file pins to the newest commit that holds its sha256",
              g("pa", "state") == "PINNED" and g("pa", "revision") == c2,
              "%s %s" % (g("pa", "state"), g("pa", "revision")[:6]))
        check("pin: a small file pins by git blob id, from the second page",
              g("pp", "state") == "PINNED" and g("pp", "revision") == c1,
              "%s %s" % (g("pp", "state"), g("pp", "revision")[:6]))
        check("pin: a repository pins where every archived file matches",
              g("pr", "revision") == r1 and "1 more" in g("pr", "checked")
              and "1 of them text saved with CRLF" in g("pr", "checked"), g("pr", "checked")[:48])
        check("pin: a text file saved with CRLF pins by its LF bytes, and says so",
              g("pt", "revision") == c3 and g("pt", "upstream_bytes") == str(len(tl))
              and g("pt", "upstream_sha256") == hashlib.sha256(tl).hexdigest(),
              "%s %s" % (g("pt", "state"), g("pt", "upstream_bytes")))
        check("pin: a file renamed in the archive is found by its sha256",
              g("pm", "revision") == c3 and g("pm", "path") == "mmproj-F16.gguf", g("pm", "path"))
        check("pin: a folder mixing commits holds its pin on what the node reads",
              g("px", "state") == "CONFIRMED" and "node reads" in g("px", "checked")
              and "2 other file" in g("px", "checked"), g("px", "state"))
        check("pin: a repository row records what a fetch takes at its commit",
              (g("pr", "fetch_files"), g("pr", "fetch_bytes")) == ("3", str(13 + 3000 + 4))
              and (g("px", "fetch_files"), g("px", "fetch_bytes")) == ("2", "3008")
              and g("pa", "fetch_bytes") == "",
              "pr %s/%s px %s/%s" % (g("pr", "fetch_files"), g("pr", "fetch_bytes"),
                                     g("px", "fetch_files"), g("px", "fetch_bytes")))
        check("pin: a 429 from the API is waited out, not reported",
              os.path.isfile(os.path.join(web, "api/models/org/nopin/commits/main.429-done"))
              and "HTTP" not in g("pn", "note"), "retried")
        check("pin: a copy no commit matches is UNPINNED, with the closest",
              g("pn", "state") == "UNPINNED" and "closest" in g("pn", "note")
              and not g("pn", "revision"), g("pn", "note")[:48])
        where = write_pins(os.path.join(tmp, "pins.csv"), pins)
        back = list(csv.DictReader(open(where, encoding="utf-8")))
        check("pin: pins.csv holds every row, merged by id",
              [b["id"] for b in back] == ["pa", "pm", "pn", "pp", "pr", "pt", "px"]
              and write_pins(where, {"pn": dict(pins["pn"], note="again")}) and
              len(list(csv.DictReader(open(where, encoding="utf-8")))) == 7, "%d rows" % len(back))

        # LF. An archive whose text files were saved with CRLF, and the proof and
        # conversion `fetch --lf --apply` does, CHECKSUMS and MANIFEST included.
        lfroot = os.path.join(tmp, "lfarch")

        def lfdisk(rel, data):
            q = os.path.join(lfroot, *rel.split("/"))
            os.makedirs(os.path.dirname(q), exist_ok=True)
            with open(q, "wb") as fh:
                fh.write(data)
            return hashlib.sha256(data).hexdigest()

        L1 = "8" * 40
        cfg_lf, tpl_lf, wts = b'{\n  "a": 1\n}\n', b"{{ x }}\n{{ y }}\n", b"W" * 900
        revision("org/lf", L1, [small("config.json", cfg_lf), lfs("model.safetensors", wts),
                                small("notes.txt", b"theirs\n"), small("tpl", tpl_lf),
                                small("LICENSE", b"MIT\r\nyes\r\n")])
        revision("org/lf", "main", [small("notes.txt", b"theirs too\n")])
        crlf = lambda b: b.replace(b"\n", b"\r\n")
        s_cfg = lfdisk("01-models/f/lfrepo/config.json", crlf(cfg_lf))
        s_w = lfdisk("01-models/f/lfrepo/model.safetensors", wts)
        s_n = lfdisk("01-models/f/lfrepo/notes.txt", b"mine\r\n")
        lfdisk("01-models/f/lfrepo/LICENSE", b"MIT\r\nyes\r\n")
        s_t = lfdisk("01-models/t/tpl", crlf(tpl_lf))
        lfdisk("01-models/f/lfrepo/CHECKSUMS.sha256", (
            "%s *./config.json\n%s *./model.safetensors\n%s *./notes.txt\n"
            % (s_cfg, s_w, s_n)).encode())
        lfdisk("01-models/CHECKSUMS.sha256", (
            "%s *./f/lfrepo/config.json\n%s *./f/lfrepo/model.safetensors\n"
            "%s *./f/lfrepo/notes.txt\n%s *./t/tpl\n" % (s_cfg, s_w, s_n, s_t)).encode())
        ftot = _folder_total(os.path.join(lfroot, "01-models", "f", "lfrepo"))
        lfdisk("MANIFEST.csv", (
            "category,filename,source_url,expected_size_bytes,sha256,download_date,"
            "verified_date,status,notes\n"
            "01-models/f,lfrepo/,https://huggingface.co/org/lf,%d,%s,,,VERIFIED,x\n"
            "01-models/t,tpl,https://huggingface.co/org/lf,%d,%s,,,VERIFIED,y\n"
            % (ftot, s_w, len(crlf(tpl_lf)), s_t)).encode())
        lr = row("lr", "lfrepo/", "hf-repo", "https://huggingface.co/org/lf", rev=L1)
        lt = row("lt", "tpl", "hf-file", "https://huggingface.co/org/lf/resolve/%s/tpl" % L1,
                 s_t, len(crlf(tpl_lf)), rev=L1)
        lr["shelf"], lt["shelf"] = "01-models/f", "01-models/t"
        proven, unproven, same = lf_plan([lr, lt], lfroot, quiet=True)
        names = sorted(os.path.basename(x[0]) for x in proven)
        check("lf: CRLF files whose LF bytes are upstream's are proven, others left",
              names == ["config.json", "tpl"]
              and [os.path.basename(u[0]) for u in unproven] == ["notes.txt"],
              "%s / %d" % (names, len(unproven)))
        check("lf: a file with CRLF upstream too is already right, not unproven",
              [os.path.basename(x) for x in same] == ["LICENSE"], "%d same" % len(same))
        log, notes = lf_apply(proven, lfroot, stamp="test", quiet=True)
        cfgp = os.path.join(lfroot, "01-models", "f", "lfrepo", "config.json")
        kept = os.path.join(lfroot, "_incoming", "crlf-originals-test", "01-models", "f",
                            "lfrepo", "config.json")
        check("lf: --apply writes upstream's bytes and keeps the original",
              open(cfgp, "rb").read() == cfg_lf and open(kept, "rb").read() == crlf(cfg_lf),
              "%d converted" % len(log))
        new_cfg = hashlib.sha256(cfg_lf).hexdigest()
        sums_ok = all(new_cfg in open(os.path.join(lfroot, *q.split("/"))).read()
                      and s_cfg not in open(os.path.join(lfroot, *q.split("/"))).read()
                      for q in ("01-models/CHECKSUMS.sha256",
                                "01-models/f/lfrepo/CHECKSUMS.sha256"))
        check("lf: every CHECKSUMS.sha256 that lists a file gets its new sha256",
              sums_ok and s_n in open(os.path.join(lfroot, "01-models", "CHECKSUMS.sha256")).read(),
              "2 of 2" if sums_ok else "stale")
        mtext = open(os.path.join(lfroot, "MANIFEST.csv")).read()
        check("lf: MANIFEST.csv follows: the file row, and the folder's exact total",
              (",%d,%s," % (len(tpl_lf), hashlib.sha256(tpl_lf).hexdigest())) in mtext
              and (",%d," % (ftot - cfg_lf.count(b"\n"))) in mtext, "%d notes" % len(notes))
        again, _u, _s = lf_plan([lr, lt], lfroot, quiet=True)
        check("lf: a second run finds nothing to convert", not again, "%d" % len(again))

        st = fetch_row(row("m", "m/", "manual"), dest, args, quiet=True)
        check("fetch: a manual row says where to get it", st[0] == "manual", st[1][:40])
    finally:
        srv.shutdown()
        if old_hf is None:
            os.environ.pop("HF_ENDPOINT", None)
        else:
            os.environ["HF_ENDPOINT"] = old_hf
        _sh.rmtree(tmp, ignore_errors=True)


def selftest_setup(check):
    """setup, in a temporary tree: a synthetic catalog, fake ports and a fake
    environment. Nothing is downloaded, installed or started."""
    import zipfile
    tmp = tempfile.mkdtemp(prefix="ark-setup-")
    root = os.path.join(tmp, "clone")
    os.makedirs(os.path.join(root, "bin"))

    def row(i, shelf, f, kind, prof, b="5"):
        return {"id": i, "shelf": shelf, "file": f, "kind": kind, "profile": prof,
                "fetch": "direct", "fetch_url": "http://127.0.0.1:9/" + f, "revision": "",
                "sha256": "", "bytes": b, "include": "", "license_status": "recorded"}
    T1, T2 = "01-models/tier1-reasoning", "01-models/tier2-fallback"
    cat = [row("water", "07-corpora-supplemental", "water.zim", "zim", "starter"),
           row("med", "07-corpora-supplemental", "med.zim", "zim", "starter"),
           row("bge-m3", "01-models/tier4-embedding", "bge-m3/", "model-repo", "starter"),
           row("qwen", T1, "Qwen3.8-27B-IQ4_XS.gguf", "gguf", "starter"),
           row("gemma12", T2, "gemma-4-12b-it-Q4_K_M.gguf", "gguf", "full"),
           row("phi", T2, "microsoft_Phi-4-mini-instruct-Q4_K_M.gguf", "gguf", "full", "2400000000"),
           row("llama", "09-software", "llamacpp-bin/llama-b1-bin-win-cuda-x64.zip", "software", "starter"),
           row("cudart", "09-software", "llamacpp-bin/cudart-llama-bin-win-cuda-x64.zip", "software", "starter"),
           row("kiwix", "09-software", "kiwix-tools/kiwix-tools_win-x86_64-3.8.1.zip", "software", "starter")]
    env = {"ARK_ROOT": root}
    cfg = load_settings(environ=env)
    calls = []

    def mk(**kw):
        a = dict(windows=True, run=lambda c: calls.append(c) or 0,
                 capture=lambda c: (0, ""), health=lambda p: None,
                 busy=lambda p: False, free=lambda d: 10 ** 15, vram=12282,
                 out=lambda *a: None, python="py", environ=env)
        a.update(kw)
        return Setup(root, a.pop("cfg", cfg), cat, **a)

    S = mk()
    with open(os.path.join(root, "MANIFEST.csv"), "w") as fh:
        fh.write("filename\n")
    check("setup: refuses an established archive (MANIFEST.csv)",
          "MANIFEST.csv" in (S.refusal() or ""), (S.refusal() or "")[:40])
    os.remove(os.path.join(root, "MANIFEST.csv"))
    r0 = S.refusal()
    check("setup: an empty clone is accepted (Python 3.11+ for ark.toml)",
          r0 is None if sys.version_info >= (3, 11) else "3.11" in (r0 or "")
          or "3.12" in (r0 or ""), "python %s" % platform.python_version())

    ids = sorted(r["id"] for r in S.rows()[0])
    check("setup: a 12 GB card fetches its own model, not the 16 GB one",
          S.profile()["name"] == "12gb" and "gemma12" in ids and "qwen" not in ids
          and "phi" not in ids and {"water", "med", "bge-m3", "llama", "kiwix"} <= set(ids),
          " ".join(ids))
    check("setup: not on Windows, the Windows zips are not fetched",
          not [r for r in mk(windows=False).rows()[0] if r["kind"] == "software"])
    check("setup: no NVIDIA card and no --models is blocked, by name",
          mk(vram=None).st_settings()[0] == "blocked")

    S.do_settings()
    lines = open(os.path.join(root, "ark.toml"), encoding="utf-8").read()
    S.reload()
    check("setup: writes ark.toml for the profile, cross-check off and how to turn it on",
          S.cfg.file and not S.cfg["models.crosscheck.enabled"]
          and S.cfg["models.primary.file"].endswith("gemma-4-12b-it-Q4_K_M.gguf")
          and "fetch phi" in lines and not settings_problems_of(S.cfg)
          and S.st_settings()[0] == "current", S.st_settings()[1][:50])
    calls[:] = []

    def put(rel, data=b"12345"):
        p = os.path.join(root, *rel.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as fh:
            fh.write(data)
        return p
    S = mk(cfg=S.cfg)
    check("setup: fetch is due with nothing here", S.st_fetch()[0] == "due", S.st_fetch()[1])
    put("07-corpora-supplemental/water.zim")
    put("07-corpora-supplemental/med.zim.part", b"12")
    w = S.cat[0]
    check("setup: a file of the catalog's size is present, a .part is partial",
          S.here(w) == "present" and S.here(S.cat[1]) == "partial")
    put("01-models/tier4-embedding/bge-m3/config.json", b"{}")
    S.pins = {"bge-m3": {"fetch_files": "3"}}
    check("setup: a repository with fewer files than its pin says is partial",
          S.here(S.cat[2]) == "partial")
    put("07-corpora-supplemental/med.zim", b"123")
    check("setup: a different file where a row goes blocks fetch, not overwritten",
          S.st_fetch()[0] == "blocked" and "Move it aside" in S.st_fetch()[1])
    os.remove(os.path.join(root, "07-corpora-supplemental", "med.zim"))

    def zipit(rel, members):
        p = os.path.join(root, *rel.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with zipfile.ZipFile(p, "w") as z:
            for n, d in members.items():
                z.writestr(n, d)
        return p
    zl = zipit("09-software/llamacpp-bin/llama-b1-bin-win-cuda-x64.zip",
               {"llama-server.exe": b"L", "ggml-cuda.dll": b"G"})
    zipit("09-software/llamacpp-bin/cudart-llama-bin-win-cuda-x64.zip", {"cudart64_12.dll": b"C"})
    zipit("09-software/kiwix-tools/kiwix-tools_win-x86_64-3.8.1.zip",
          {"kiwix-serve.exe": b"K", "kiwix-manage.exe": b"M"})
    check("setup: binaries are due before they are extracted", S.st_binaries()[0] == "due")
    S.do_binaries()
    check("setup: llama.cpp and kiwix-tools land where the settings look",
          S.st_binaries()[0] == "current"
          and os.path.exists(os.path.join(S.cfg["paths.llama"], "cudart64_12.dll")),
          S.st_binaries()[1])
    with open(os.path.join(S.cfg["paths.llama"], "llama-server.exe"), "wb") as fh:
        fh.write(b"MINE")
    wk = extract_zip(zl, S.cfg["paths.llama"])
    check("setup: extracting again overwrites nothing",
          wk == (0, 2) and open(os.path.join(S.cfg["paths.llama"], "llama-server.exe"),
                                "rb").read() == b"MINE", str(wk))
    ev = zipit("evil.zip", {"../outside.dll": b"x", "ok.dll": b"y"})
    try:
        extract_zip(ev, os.path.join(tmp, "evil"))
        ok = False
    except ValueError:
        ok = not os.path.exists(os.path.join(tmp, "evil", "ok.dll")) \
            and not os.path.exists(os.path.join(tmp, "outside.dll"))
    check("setup: a zip member that escapes its folder is refused, nothing written", ok)

    os.makedirs(os.path.join(root, "13-ark-node", "requirements"))
    with open(os.path.join(root, "13-ark-node", "requirements", "node.txt"), "w") as fh:
        fh.write("# torch-index: https://example.invalid/cu128\ntorch==9.9.0\nnumpy==1\n")
    S.do_venv()
    vpy = S.venv_python()
    check("setup: venv makes the environment, then CUDA torch, then the rest",
          [c[1:4] for c in calls[:1]] == [["-m", "venv", S.cfg["paths.venv"]]]
          and calls[2][-3:] == ["torch==9.9.0", "--index-url", "https://example.invalid/cu128"]
          and calls[3][-2] == "-r" and calls[3][-1].endswith("index.txt")
          and all(c[0] == vpy for c in calls[1:]), "%d commands" % len(calls))
    put(os.path.relpath(vpy, root).replace(os.sep, "/"), b"")
    probe = {"missing": [], "python": "3.12.9", "torch": "2.11.0+cpu", "cuda": False}
    Sv = mk(cfg=S.cfg, capture=lambda c: (0, "ARK-PROBE " + json.dumps(probe)))
    check("setup: a torch that does not see the GPU is not a finished environment",
          Sv.st_venv()[0] == "due" and "CPU" in Sv.st_venv()[1], Sv.st_venv()[1][:50])
    probe.update(cuda=True, torch="2.11.0+cu128", gpu="RTX")
    check("setup: imports and CUDA, and the environment is current",
          Sv.st_venv(fresh=True)[0] == "current", Sv.st_venv()[1])

    ix = os.path.join(root, "10-index")
    os.makedirs(os.path.join(ix, "chunks"))
    os.makedirs(os.path.join(ix, "vectors"))
    with open(os.path.join(ix, "sources.json"), "w") as fh:
        json.dump({"artifacts": {"1": {"shelf": "07-corpora-supplemental", "file": "water.zim",
                                       "kiwix_book": "water"},
                                 "2": {"shelf": "07-corpora-supplemental", "file": "med.zim",
                                       "kiwix_book": "med"}}}, fh)
    for s in ("water.zim", "med.zim"):
        put("10-index/chunks/%s.jsonl" % s)
    put("10-index/vectors/water.zim.f16.npy")
    check("setup: build is due until every starter ZIM has chunks and vectors",
          S.st_build() == ("due", "1 of 2 ZIM files indexed"), S.st_build()[1])
    put("10-index/vectors/med.zim.f16.npy")
    S.write_scope()
    sc = load_scope_lines(S.scope_path())
    check("setup: ...and current when they do; the scope is the catalog's ZIMs",
          S.st_build()[0] == "current" and sc == ["07-corpora-supplemental/water.zim",
                                                  "07-corpora-supplemental/med.zim"], str(sc))

    other = {"archive_root": os.path.join(tmp, "somewhere-else")}
    Sp = mk(cfg=S.cfg, busy=lambda p: True, health=lambda p: other)
    check("setup: another archive's node on our port is refused, and named",
          "somewhere-else" in (Sp.ports_refusal() or ""), (Sp.ports_refusal() or "")[:60])
    Sp = mk(cfg=S.cfg, busy=lambda p: True, health=lambda p: {"archive_root": root})
    check("setup: our own node on our port is not a refusal", Sp.ports_refusal() is None)
    Sp = mk(cfg=S.cfg, busy=lambda p: p == S.cfg["ports.archive"])
    check("setup: a port held by something else is refused",
          "%d" % S.cfg["ports.archive"] in (Sp.ports_refusal() or ""))

    class Fake:
        def __init__(self, fail=None):
            self.ran, self.fail = [], fail

        def states(self):
            return dict((s, (lambda s=s: ("current", "") if s in self.ran else ("due", "")))
                        for s in SETUP_STEPS)

        def st_venv(self, fresh=False):
            return self.states()["venv"]()

        def __getattr__(self, name):
            if name.startswith("do_"):
                step = name[3:]

                def f():
                    if step == self.fail:
                        return 1
                    self.ran.append(step)
                    return 0
                return f
            raise AttributeError(name)
    F = Fake()
    code, done = setup_run(F, out=lambda *a: None)
    check("setup: every step runs once, in order, and the check is always asked",
          code == 0 and F.ran == SETUP_STEPS, " ".join(F.ran))
    F = Fake(fail="build")
    code, done = setup_run(F, out=lambda *a: None)
    check("setup: a failing step stops the run; nothing after it runs",
          code == 1 and F.ran == SETUP_STEPS[:SETUP_STEPS.index("build")]
          and done[-1][:2] == ("build", "FAILED"), " ".join(F.ran))
    F = Fake()
    setup_run(F, plan=True, out=lambda *a: None)
    check("setup: --plan runs nothing", F.ran == [])

    ok = summarize_answer({"_wall": 41.2, "retrieval": {
        "mode_ran": "hybrid", "results": [{"citation": {"title": "Hesperian: Water"}}]},
        "answer": {"grounding": "grounded", "cited": [1], "text": "2 drops per litre [1]."}})
    down = summarize_answer({"retrieval": {"results": [{}]}, "answer": None,
                             "model_down": "The primary model is not running."})
    check("setup: the first answer is shown with its citations; a down model fails",
          ok["ok"] and ok["cited"] == 1 and any("Hesperian" in x for x in ok["lines"])
          and not down["ok"] and "not running" in down["lines"][-1], ok["lines"][0])

    said = []
    rehash_dir("10-index", root, out=lambda *a: said.append(" ".join(str(x) for x in a)))
    check("setup: a clone's rehash says nothing about a MANIFEST.csv it does not have",
          said and not any("MANIFEST" in x for x in said), said[0][:40] if said else "")
    tiles = [c for c in components() if c["name"] == "tiles"][0]
    check("setup: tiles with no maps folder is 'none', not MISSING",
          tiles.get("absent_ok", [""])[0] == CFG["paths.maps"])
    kit_s = not_installed({"paths.maps": os.path.join(tmp, "no-maps"),
                           "models.primary.enabled": True,
                           "models.crosscheck.enabled": False})
    kit_f = not_installed({"paths.maps": tmp, "models.primary.enabled": True,
                           "models.crosscheck.enabled": True})
    check("setup: the node is told what the starter leaves out, and nothing on a full kit",
          kit_s == ["crosscheck", "map"] and kit_f == [], "%s / %s" % (kit_s, kit_f))

    # A 6 GB CARD, AND ONE TOO SMALL FOR ANY PROFILE (the 2026-10-03 laptop).
    S6 = mk(vram=6144, cfg=load_settings(environ={"ARK_ROOT": tmp}))
    s6 = S6.st_settings()
    check("setup: a 6 GB card is offered the 8gb profile, and its size is shown",
          s6[0] == "due" and "8gb" in s6[1] and "6,144" in s6[1], s6[1][-40:])
    s4 = mk(vram=4000, cfg=load_settings(environ={"ARK_ROOT": tmp})).st_settings()
    check("setup: a card too small says so, not 'no card found'",
          s4[0] == "blocked" and "4,000 MiB" in s4[1] and "no NVIDIA card" not in s4[1],
          s4[1][:50])
    s0 = mk(vram=None, cfg=load_settings(environ={"ARK_ROOT": tmp})).st_settings()
    check("setup: no nvidia-smi still says no card, and how to fix it",
          s0[0] == "blocked" and "driver" in s0[1], s0[1][:40])

    # THE ENVIRONMENT CHECK: the real error, one retry, the DLL hint.
    seq = [{"missing": ["torch (ImportError: DLL load failed while importing _C)"],
            "cuda": False},
           {"missing": [], "python": "3.12.7", "torch": "2.11.0+cu128", "cuda": True,
            "gpu": "RTX 2060"}]
    slept = []
    Sr = mk(cfg=S.cfg, sleep=lambda s: slept.append(s),
            capture=lambda c: (0, "ARK-PROBE " + json.dumps(seq.pop(0) if len(seq) > 1
                                                             else seq[0])))
    check("setup: a failed import right after install is asked once more",
          Sr.st_venv(fresh=True)[0] == "current" and slept == [8], str(slept))
    bad = {"missing": ["torch (ImportError: DLL load failed while importing _C: x)"],
           "cuda": False}
    Sb = mk(cfg=S.cfg, sleep=lambda s: None,
            capture=lambda c: (0, "ARK-PROBE " + json.dumps(bad)))
    sb = Sb.st_venv(fresh=True)
    check("setup: an import that keeps failing shows its error and the VC++ fix",
          sb[0] == "due" and "DLL load failed" in sb[1] and "vc_redist" in sb[1],
          sb[1][:48])

    # CERTIFICATES: trusted from two lists, never retried, explained.
    import ssl as _ssl
    ctx = ssl_context()
    e1 = urllib.error.URLError(_ssl.SSLCertVerificationError(1, "unable to get local issuer"))
    e2 = urllib.error.URLError(socket.timeout("timed out"))
    check("fetch: verification stays on; a refused certificate is told from a timeout",
          ctx.verify_mode == _ssl.CERT_REQUIRED and ctx.check_hostname
          and _cert_refused(e1) and not _cert_refused(e2))
    calls = []
    real_open = globals()["_open"]

    def refusing(url, start=0, timeout=60):
        calls.append(url)
        raise CertificateRefused(CERT_HELP % "example.invalid")
    globals()["_open"] = refusing
    try:
        try:
            download("https://example.invalid/x", os.path.join(tmp, "x.part"), quiet=True)
            got = "no error"
        except CertificateRefused as e:
            got = str(e)
    finally:
        globals()["_open"] = real_open
    check("fetch: a refused certificate is not retried, and says what to do",
          len(calls) == 1 and "SSL_CERT_FILE" in got, "%d call(s)" % len(calls))

    real = os.path.join(os.path.dirname(HERE), "13-ark-node", "requirements", "node.txt")
    if os.path.isfile(real):
        t = requirements_torch(real)
        check("setup: node.txt names torch and the CUDA index it comes from",
              t[0] and t[0].startswith("torch==") and t[1] and "cu1" in t[1], " ".join(t))
    shutil.rmtree(tmp, ignore_errors=True)


def settings_problems_of(cfg):
    return list(getattr(cfg, "problems", []))


def load_scope_lines(path):
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.split("#", 1)[0].strip()
            if line:
                out.append(line)
    return out


_CHECKS = [0]


def do_selftest(args):
    """The three warnings, against a synthetic process table and a fake GPU.

    THEY CAN ONLY FIRE ON A BROKEN MACHINE, which is exactly why they need a
    test that does not require one. Every defect they detect was found on
    2026-09-10 by reading a status line that looked healthy; none of them would
    have failed anything. So the detections are exercised here with made-up
    inputs, and the numbers in the fixtures are the real ones from that day."""
    cs = components()
    node = [c for c in cs if c["name"] == "node"][0]
    prim = [c for c in cs if c["name"] == "primary"][0]
    bad = 0

    def check(name, ok, detail=""):
        nonlocal bad
        _CHECKS[0] += 1
        if ok:
            print("  ok    %-38s %s" % (name, detail))
        else:
            bad += 1
            print("  FAIL  %-38s %s" % (name, detail))

    # 1. DUPLICATES. The real shape from that day: three serve.py, one serving.
    # Each was started separately from a shell, so none is another's child and
    # all three are real servers - the case `roots()` must NOT collapse.
    fake = [(28504, 1000, r"C:\ark\.venv\Scripts\python.exe C:\ark\13-ark-node\ark-api\serve.py"),
            (9908,  1000, r'"C:\Python312\python.exe" C:\ark\13-ark-node\ark-api\serve.py'),
            (37332, 1000, r'"C:\Python312\python.exe" C:\ark\13-ark-node\ark-api\serve.py'),
            (20744, 1000, r"C:\ark\llama\llama-server.exe -a qwen-primary -c 8192")]
    inst = instances(node, fake)
    check("three serve.py are all seen", len(inst) == 3, "found %d" % len(inst))
    check("three separate servers are three roots",
          len(roots(inst, fake)) == 3, "%d roots" % len(roots(inst, fake)))
    check("the model server is told apart by its alias",
          len(instances(prim, fake)) == 1)
    check("a blind process table is not an empty one",
          instances(node, None) is None)

    # 2. STRAYS. Stopping the recorded pid must leave the other two named.
    # AND THE MODEL SERVER COUNTS TOO. The first version of this assertion
    # expected only the two node duplicates and failed on the llama-server -
    # which was the assertion being wrong, not the code. A `primary` that this
    # script did not start and cannot stop is holding 14.8 GB of VRAM, and an
    # operator running `down` to free the card needs to be told it is there.
    st = _strays(cs, fake, {28504})
    names = sorted(pid for _n, pid, _c in st)
    check("every unrecorded component process is named",
          names == [9908, 20744, 37332], "strays=%s" % names)
    check("a recorded pid is not reported as a stray", 28504 not in names)
    check("strays of a blind table are blind, not none",
          _strays(cs, None, set()) is None)

    # 2b. PARENTAGE. The 09-12 shape: ONE node, under the keeper venv, with a
    # child of its own under the base interpreter. Every assertion here failed
    # before the parent was part of the process table.
    pair = [(23108, 1, r"C:\Windows\System32\cmd.exe"),
            (44320, 23108, r"C:\ark\.venv\Scripts\python.exe C:\ark\13-ark-node\ark-api\serve.py"),
            (6896, 44320, r'"C:\Python312\python.exe" C:\ark\13-ark-node\ark-api\serve.py')]
    pr = roots(instances(node, pair), pair)
    check("a node and its own child are one server",
          len(pr) == 1, "%d roots" % len(pr))
    check("the root is the parent, not the child",
          [h[0] for h in pr] == [44320], "%s" % [h[0] for h in pr])
    check("the root is the interpreter the node was started with",
          ".venv" in (pr[0][2] if pr else ""))
    sp = sorted(pid for _n, pid, _c in (_strays(cs, pair, set()) or []))
    check("a child of a running node is not a stray",
          sp == [44320], "strays=%s" % sp)
    # AND THE ORPHAN STILL IS. Same child, parent already gone from the table:
    # nothing is holding it, it answers nothing, and it must still be named.
    orph = [(6896, 44320, r'"C:\Python312\python.exe" C:\ark\13-ark-node\ark-api\serve.py')]
    so = sorted(pid for _n, pid, _c in (_strays(cs, orph, set()) or []))
    check("an orphan with a vanished parent is still a stray",
          so == [6896], "strays=%s" % so)
    check("a descendant of a pid we just stopped is not a stray",
          _strays(cs, pair, {44320}) == [])

    # 3. EVICTION. 16,050 free with the primary UP - the real reading.
    ev = evicted(cs, ["primary"], 16050)
    check("16,050 MiB free with primary UP is a fault",
          [n for n, _ in ev] == ["primary"], "%s" % ev)
    check("614 MiB free with primary UP is healthy",
          evicted(cs, ["primary"], 614) == [])
    check("the crosscheck's 212 MiB is not a VRAM claim",
          evicted(cs, ["crosscheck"], 16050) == [])
    check("unknown VRAM raises nothing", evicted(cs, ["primary"], None) == [])
    check("a model started seconds ago is not judged by an unsettled reading",
          evicted(cs, ["primary"], 16050, fresh={"primary": 12.0}) == []
          and evicted(cs, ["primary"], 16050, fresh={"node": 12.0}) != [])

    # 4. INTERPRETER. The node ran nine hours under system Python.
    want = node_python()[0].replace("\\", "/").lower()
    mism = [pid for pid, _ppid, cmd in inst
            if want not in cmd.replace("\\", "/").lower()]
    check("an interpreter mismatch is detectable", len(mism) >= 2,
          "%d of %d differ from %s" % (len(mism), len(inst),
                                       os.path.basename(want)))
    # AND IT MUST NOT FIRE ON THE 09-12 PAIR. The child differs from the root;
    # reading it as the node's interpreter is the false alarm this fixes.
    pm = [pid for pid, _ppid, cmd in pr
          if want not in cmd.replace("\\", "/").lower()]
    # (Kept machine-independent: on a box where the keeper venv is not the
    # chosen interpreter the root legitimately mismatches. What must never
    # happen anywhere is the CHILD's interpreter being reported as the node's.)
    check("the interpreter warning cannot name the node's child",
          6896 not in pm, "%s" % pm)

    # 5. THE DENSE NOTE. Added 2026-09-17 with dense_note() itself, because the
    # line it replaced could only be caught by a human reading a status page on
    # a day the node was broken - which is how 2026-09-16 was found, three hours
    # in. These are pure-function fixtures and the strings are the real ones:
    # the backend and coverage the node reported that night, and the ImportError
    # /api/health was carrying, verbatim, while the screen said something else.
    NUMPY_0916 = ("ImportError: cannot import name 'NDArray' from partially "
                  "initialized module 'numpy._typing' (most likely due to a "
                  "circular import) [__init__.py:4 <- defmatrix.py:13 <- "
                  "__init__.py:87 <- _linalg.py:76]")
    n_ok, w_ok = dense_note({"dense_backend": "pq", "dense_coverage": 6926271})
    check("a loaded backend is reported as the backend",
          "pq" in n_ok and "6,926,271" in n_ok and w_ok is None, n_ok)
    n_bad, w_bad = dense_note({"dense_attempted": True, "dense_backend": None,
                               "dense_error": NUMPY_0916})
    check("the 09-16 failure reads as a failure",
          n_bad.startswith("DENSE FAILED") and "NDArray" in (w_bad or ""),
          n_bad)
    n_lazy, w_lazy = dense_note({"dense_attempted": False,
                                 "dense_backend": None})
    # A LAZY LOADER THAT HAS NOT BEEN ASKED IS NOT BROKEN. `status` run seconds
    # after `up` lands here, and the whole value of the column is lost if that
    # reads as a fault.
    check("an unasked lazy loader is not a fault",
          "not loaded yet" in n_lazy and w_lazy is None
          and "FAIL" not in n_lazy.upper(), n_lazy)
    check("no health payload says so rather than guessing",
          dense_note(None) == (None, None))
    # THE INVARIANT THE OLD LINE BROKE. Every note above describes RETRIEVAL.
    # The one it replaced described a directory - `keeper venv: dense retrieval
    # available` - and was printed, true, about a node serving keyword only.
    check("the note never answers with the filesystem",
          not any("venv" in s.lower() for s in (n_ok, n_bad, n_lazy)))
    # AND THE ONE NOTE THAT STILL COMES FROM THE FILESYSTEM MUST NOT OVERCLAIM.
    # `up` and `commands` have no running node to ask, so node_python() is all
    # they have. Its NEGATIVE branch is entailed by the disk - no venv, no dense
    # half - and its POSITIVE branch is not, which is why it may say what was
    # found and may not say what that means for retrieval.
    _np_note = node_python()[1].lower()
    check("the launcher note claims no more than it can see",
          "available" not in _np_note and
          ("keyword only" in _np_note or "status" in _np_note),
          node_python()[1])

    selftest_settings(check)
    selftest_fetch(check)
    selftest_index(check)
    selftest_profiles(check)
    selftest_rehash(check)
    selftest_setup(check)
    selftest_index_add(check)
    total = _CHECKS[0]
    print("\n%d/%d" % (total - bad, total))
    return 1 if bad else 0


def selftest_index_add(check):
    """index --add: the row it accepts, and a scope file written once."""
    import tempfile
    rows = [{"id": "z", "kind": "zim"}, {"id": "m", "kind": "gguf"}]
    check("index --add finds a collection row", corpus_row(rows, "z")[0] is rows[0])
    check("index --add refuses a model row", corpus_row(rows, "m")[0] is None)
    check("index --add refuses an unknown id", corpus_row(rows, "q")[0] is None)
    d = tempfile.mkdtemp()
    sp = os.path.join(d, "10-index", "scope-local.txt")
    first, again = scope_add(sp, "07-x/a.zim"), scope_add(sp, "07-x/a.zim")
    with open(sp, encoding="utf-8") as fh:
        body = fh.read()
    check("index --add writes the path once", (first, again, body.count("07-x/a.zim")) == (True, False, 1))
    check("index --add writes UTF-8 with LF", "\r" not in body and body.startswith("# Collections"))


DESCRIPTION = """ark.py - start the node's servers, say what is running, and stop them.

A convenience and never load-bearing: `commands` prints every command it would
run, so you can do it by hand. RUNBOOK.md stays the source of truth."""


def epilog():
    """The examples, plus the component names and ports READ OUT OF THE CODE.

    `components()` is the one place a name or a port is written down, so this
    asks it rather than repeating it. A help text listing `node(8090)` after
    the port moved would be worse than no help text at all."""
    try:
        line = "  ".join("%s(%d)" % (c["name"], c["port"]) for c in components())
    except Exception:                                  # noqa: BLE001
        line = "run `status` to list them"
    return """examples:
  python bin/ark.py setup --profile starter   empty clone to first answer
  python bin/ark.py status                 what is up, and why anything is not
  python bin/ark.py up                     start everything that is missing
  python bin/ark.py up --no-models         search only: no GPU, no 20 GB of RAM
  python bin/ark.py up node tiles          just those two
  python bin/ark.py commands               print the commands, start nothing
  python bin/ark.py down                   stop what this script started
  python bin/ark.py index --plan           after an index build: what is due
  python bin/ark.py verify 00-docs         check a shelf's checksums, no bash needed

components:
  %s

On Windows each component gets its own console window, so closing one stops that
component. Everything down when you did not run `down` means the windows were
closed, not that anything crashed.""" % line


def main():
    ap = argparse.ArgumentParser(
        prog="ark.py",
        description=DESCRIPTION,
        epilog=epilog(),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="verb", title="verbs", metavar="VERB")
    sub.add_parser("status", help="what is running right now, and why anything "
                                  "is not (the default if no verb is given)")
    u = sub.add_parser("up", help="start whatever is not already running")
    u.add_argument("--no-models", action="store_true",
                   help="search only: no GPU, no 20 GB of RAM")
    u.add_argument("--force", action="store_true",
                   help="start even when the machine says it will not fit")
    u.add_argument("only", nargs="*",
                   help="component names; default is all of them")
    sub.add_parser("commands", help="print every command and start nothing")
    su = sub.add_parser("setup", help="from an empty clone to a first cited answer: "
                                      "settings, downloads, binaries, Python "
                                      "environment, index, servers, and one question")
    su.add_argument("--profile", required=True, choices=["starter", "full"],
                    help="starter: about 20 GB, water, medicine and post-disaster care")
    su.add_argument("--models", metavar="NAME",
                    help="the model profile (8gb, 12gb, 16gb, 24gb); default: the "
                         "largest this card holds. Ignored once ark.toml exists")
    su.add_argument("--plan", action="store_true",
                    help="show each step's state and why; change nothing")
    fe = sub.add_parser("fetch", help="download catalog rows or a profile, resuming, "
                                      "and refuse any file whose checksum is wrong")
    fe.add_argument("ids", nargs="*", help="catalog ids (see --list)")
    fe.add_argument("--profile", choices=["starter", "full"],
                    help="every row of that profile; full is the whole catalog")
    fe.add_argument("--list", action="store_true",
                    help="show the rows, their size and what is already here; "
                         "download nothing")
    fe.add_argument("--dry-run", action="store_true", help="same as --list")
    fe.add_argument("--verify", action="store_true",
                    help="re-check the sha256 of files already present")
    fe.add_argument("--accept-newer", action="store_true",
                    help="when Kiwix no longer serves an edition, fetch the newer "
                         "one and record the substitution")
    fe.add_argument("--all-files", action="store_true",
                    help="whole Hugging Face repositories, ONNX and duplicates "
                         "included")
    fe.add_argument("--pin", action="store_true",
                    help="find the Hugging Face commit each row's archive copy "
                         "came from and write it to pins.csv beside the catalog; "
                         "downloads nothing. Default: every Hugging Face row")
    fe.add_argument("--pin-depth", type=int, default=HF_PIN_DEPTH,
                    help="how many commits to read back per repository "
                         "(default %d)" % HF_PIN_DEPTH)
    fe.add_argument("--lf", action="store_true",
                    help="list the archive's Hugging Face text files whose only "
                         "difference from upstream is CRLF line endings, each proven "
                         "against the pinned commit; changes nothing without --apply")
    fe.add_argument("--apply", action="store_true",
                    help="with --lf: convert them to LF, keep the originals in "
                         "_incoming, update every CHECKSUMS.sha256 and MANIFEST.csv")
    fe.add_argument("--dest", help="where the archive lives; default paths.root")
    fe.add_argument("--catalog", help="another catalog.csv; default paths.catalog")
    rh = sub.add_parser("rehash", help="rewrite a shelf's CHECKSUMS.sha256, as "
                                       "bin/rehash.sh does, without bash")
    rh.add_argument("dirs", nargs="+", help="shelves, relative to the archive root")
    vf = sub.add_parser("verify", help="check every CHECKSUMS.sha256, as bin/verify.sh "
                                       "does, without bash; or only those under the "
                                       "directories named")
    vf.add_argument("dirs", nargs="*", help="limit to these; default the whole archive")
    ms = sub.add_parser("measure", help="load a model once, read what llama-server "
                                        "reserves on the GPU and in RAM, stop it, and "
                                        "print the numbers for ark.toml")
    ms.add_argument("role", nargs="?", default="both",
                    choices=["primary", "crosscheck", "both"])
    ms.add_argument("--profile", help="measure a profile's models instead of the "
                                      "settings in use")
    ms.add_argument("--timeout", type=int, default=900,
                    help="seconds to wait for a model to load (default 900)")
    ix = sub.add_parser("index", help="after index-build.py: run the steps that make "
                                      "new chunks searchable, only those that are "
                                      "due, in order, stopping at the first failure")
    ix.add_argument("--plan", action="store_true",
                    help="show each step's state and why; run nothing")
    ix.add_argument("--force", help="run these steps even if current, comma-separated: "
                                    "pq,bm25,provenance,mirror,rehash")
    ix.add_argument("--no-rehash", action="store_true",
                    help="leave the 10-index rehash for later (it reads the whole shelf)")
    ix.add_argument("--add", metavar="ID",
                    help="index this collection first (a catalog id that is on the "
                         "drive), then run the steps; uses the graphics card")
    ix.add_argument("--with-models", action="store_true",
                    help="with --add: build even while the primary model is running")
    cf = sub.add_parser("config", help="every setting, its value, and whether it "
                                       "came from the environment, ark.toml or "
                                       "the default")
    cf.add_argument("--profiles", action="store_true",
                    help="the model pairs for 8, 12, 16 and 24 GB cards, which "
                         "footprints are measured, and which fits this machine")
    cf.add_argument("--profile", metavar="NAME",
                    help="with --example: a complete ark.toml with the models "
                         "section set to that profile (8gb, 12gb, 16gb, 24gb)")
    cf.add_argument("--example", action="store_true",
                    help="print a complete ark.toml with every setting commented "
                         "out at its default, generated from this file")
    sub.add_parser("selftest", help="check the duplicate, stray, eviction and "
                                    "interpreter detections with synthetic "
                                    "inputs - needs no GPU and starts nothing")
    d = sub.add_parser("down", help="stop what this script started")
    d.add_argument("--strays", action="store_true",
                   help="also stop processes that LOOK like node components but "
                        "were not started from here - a second serve.py that "
                        "never got the port survives a plain `down` and holds "
                        "memory. Asked for explicitly, never done by default")
    a = ap.parse_args()
    if a.verb != "config":
        settings_problems(announce=True)
    if a.verb == "up":
        return do_up(a)
    if a.verb == "commands":
        return do_commands(a)
    if a.verb == "setup":
        return do_setup(a)
    if a.verb == "config":
        return do_config(a)
    if a.verb == "fetch":
        return do_fetch(a)
    if a.verb == "index":
        return do_index(a)
    if a.verb == "measure":
        return do_measure(a)
    if a.verb == "rehash":
        return do_rehash(a)
    if a.verb == "verify":
        return do_verify(a)
    if a.verb == "down":
        return do_down(a)
    if a.verb == "selftest":
        return do_selftest(a)
    return do_status(a)


if __name__ == "__main__":
    sys.exit(main())
