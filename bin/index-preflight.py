#!/usr/bin/env python3
"""
index-preflight.py - prove the indexing stack works OFFLINE before building anything.

    python bin/index-preflight.py

WHY THIS EXISTS. Operating rule R12: a set of packages that pip resolved is not a
set that runs. This build has already been bitten by a resolver reporting success
on a torch/torchaudio pair that could not import each other. The indexer has a
sharper version of the same problem:

    LlamaIndex's default SentenceSplitter measures chunk size in TOKENS, and its
    default tokenizer is tiktoken, which DOWNLOADS its BPE file from the internet
    on first use and caches it.

On a machine with network that works and is invisible. On the node it is a
retrieval layer that will not start. So the splitter must be given the BGE-M3
tokenizer explicitly, loaded from local files - which also makes the chunk size
mean what we think it means, since it is then counted in the same tokens the
embedding model will actually see.

This script asserts nothing. It reports, and every check is one a person can
re-run after any change to the wheel set.
"""

import os, sys, importlib, traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODEL = os.path.join(ROOT, "01-models", "tier4-embedding", "bge-m3")

PASS, FAIL, WARN = "  ok  ", " FAIL ", " warn "
problems = []
warnings = []


def check(label, fn):
    sys.stdout.write("[      ] %-46s" % label)
    sys.stdout.flush()
    try:
        note = fn() or ""
        sys.stdout.write("\r[%s] %-46s %s\n" % (PASS, label, note))
        return True
    except Exception as e:
        sys.stdout.write("\r[%s] %-46s %s: %s\n" % (FAIL, label, type(e).__name__, e))
        problems.append(label)
        return False


def optional(label, fn, needed_for):
    """A check whose failure costs one feature, not the build. Printed as a
    warning with what it is needed for, and not counted as a failure."""
    sys.stdout.write("[      ] %-46s" % label)
    sys.stdout.flush()
    try:
        note = fn() or ""
        sys.stdout.write("\r[%s] %-46s %s\n" % (PASS, label, note))
        return True
    except Exception as e:
        sys.stdout.write("\r[%s] %-46s %s: %s - needed only %s\n"
                         % (WARN, label, type(e).__name__, e, needed_for))
        warnings.append(label)
        return False


def ver(name):
    def f():
        m = importlib.import_module(name)
        return getattr(m, "__version__", "?")
    return f


print("Ark index preflight")
print("python %s" % sys.version.split()[0])
print("model  %s\n" % MODEL)

print("-- the wheel set actually imports " + "-" * 40)
for mod in ("libzim", "torch", "transformers", "sentence_transformers",
            "llama_index.core", "numpy"):
    check(mod, ver(mod))
# OPTIONAL SINCE 2026-09-29. PyMuPDF (fitz) reads the PDF shelves and nothing
# else; it is AGPL-3.0, so the public release makes it an extra rather than a
# requirement, and a ZIM-only build must not fail here for want of it. Chroma is
# off by default in index-build.py, and the node's dense index is faiss PQ64.
optional("fitz (PyMuPDF)", ver("fitz"), "to index PDF shelves")
optional("chromadb", ver("chromadb"), "for index-build.py --chroma")

print("\n-- hardware " + "-" * 53)


def cuda():
    import torch
    if not torch.cuda.is_available():
        raise RuntimeError("no CUDA - embedding will run on CPU and take days")
    p = torch.cuda.get_device_properties(0)
    return "%s, %.1f GB" % (p.name, p.total_memory / 1024 ** 3)


check("CUDA device", cuda)

print("\n-- OFFLINE readiness (the point of this script) " + "-" * 17)


def tokenizer_local():
    """Load the BGE-M3 tokenizer with the network explicitly forbidden."""
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    from transformers import AutoTokenizer
    tk = AutoTokenizer.from_pretrained(MODEL, local_files_only=True)
    n = len(tk.encode("El agua hervida es segura para beber."))
    return "loads with local_files_only, %d tokens on a Spanish probe" % n


check("BGE-M3 tokenizer, network forbidden", tokenizer_local)


def tiktoken_status():
    """Is the hazard real on THIS machine? Report, do not assume."""
    try:
        import tiktoken
    except ImportError:
        return "tiktoken not installed - LlamaIndex default splitter WILL fail offline"
    cache = os.environ.get("TIKTOKEN_CACHE_DIR") or "<default temp dir>"
    return "tiktoken %s present, cache=%s - DO NOT RELY ON IT" % (
        getattr(tiktoken, "__version__", "?"), cache)


check("tiktoken (the hazard)", tiktoken_status)


def splitter_with_our_tokenizer():
    """The splitter we will actually use: BGE-M3 tokens, no tiktoken anywhere."""
    from transformers import AutoTokenizer
    from llama_index.core.node_parser import SentenceSplitter
    tk = AutoTokenizer.from_pretrained(MODEL, local_files_only=True)
    sp = SentenceSplitter(chunk_size=512, chunk_overlap=77,
                          tokenizer=lambda t: tk.encode(t, add_special_tokens=False))
    txt = ("Boil water for one minute at a rolling boil. " * 200)
    parts = sp.split_text(txt)
    return "%d chunks from %d chars, counted in BGE-M3 tokens" % (len(parts), len(txt))


check("SentenceSplitter on BGE-M3 tokens", splitter_with_our_tokenizer)


def chroma_local():
    import chromadb, tempfile, shutil
    d = tempfile.mkdtemp()
    try:
        c = chromadb.PersistentClient(path=d)
        col = c.create_collection("preflight")
        col.add(ids=["a", "b"], embeddings=[[0.1] * 8, [0.2] * 8],
                metadatas=[{"src": 1}, {"src": 1}], documents=["x", "y"])
        got = col.query(query_embeddings=[[0.1] * 8], n_results=1)
        assert got["ids"][0], "query returned nothing"
        return "persistent client writes and queries"
    finally:
        shutil.rmtree(d, ignore_errors=True)


optional("Chroma persistent client", chroma_local, "for index-build.py --chroma")


def embed_roundtrip():
    """One real embedding. Proves the model loads offline and the dim is 1024."""
    os.environ["HF_HUB_OFFLINE"] = "1"
    from sentence_transformers import SentenceTransformer
    m = SentenceTransformer(MODEL, local_files_only=True)
    v = m.encode(["clean water", "agua limpia"], normalize_embeddings=True)
    if v.shape[1] != 1024:
        raise RuntimeError("expected 1024 dims, got %d" % v.shape[1])
    import numpy as np
    sim = float(np.dot(v[0], v[1]))
    return "1024 dims; EN/ES cross-lingual similarity %.3f" % sim


check("BGE-M3 embeds, offline, 1024-dim", embed_roundtrip)

print()
if warnings:
    print("optional, not installed or not working: %s" % ", ".join(warnings))
if problems:
    print("PREFLIGHT FAILED: %s" % ", ".join(problems))
    print("Nothing is built until these pass. R12: a resolved set is not a running set.")
    sys.exit(1)
print("PREFLIGHT CLEAN - the stack runs with the network forbidden.")
