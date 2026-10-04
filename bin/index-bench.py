#!/usr/bin/env python3
"""
index-bench.py - find the batch size before committing to a 15-hour build.

    python bin/index-bench.py

The pilot measured 147 chunks/s embedding on prose. At pass 1 scope that is about
15 hours end to end. The default batch of 64 was a guess, and the machine has 16 GB
of VRAM - not the 64 GB of system RAM the index sizing was about. Three minutes
here is worth several hours there, and an OOM at hour nine is worth avoiding
outright.

Reports throughput AND peak VRAM per batch size, on real chunks read from the index
that already exists, so the token-length distribution is the real one rather than a
synthetic string that would make every batch look uniform.
"""

import os, sys, json, time, glob

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
MODEL = os.path.join(ROOT, "01-models", "tier4-embedding", "bge-m3")
CHUNKS = os.path.join(ROOT, "10-index", "chunks")

os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"

N = 3000
BATCHES = [32, 64, 128, 192, 256]


def sample_chunks(n):
    txts = []
    for f in sorted(glob.glob(os.path.join(CHUNKS, "*.jsonl"))):
        with open(f, encoding="utf-8") as fh:
            for line in fh:
                txts.append(json.loads(line)["text"])
                if len(txts) >= n:
                    return txts
    return txts


def main():
    import torch
    from sentence_transformers import SentenceTransformer

    txts = sample_chunks(N)
    if len(txts) < 200:
        sys.exit("need chunks in 10-index/chunks first - run a pilot build")
    lens = sorted(len(t) for t in txts)
    print("benchmark on %d REAL chunks: median %d chars, p90 %d, max %d\n"
          % (len(txts), lens[len(lens) // 2], lens[int(len(lens) * .9)], lens[-1]))

    m = SentenceTransformer(MODEL, local_files_only=True)
    m.half()
    print("max_seq_length = %s" % m.max_seq_length)

    # warm the kernels so the first row is not penalised for everyone else
    m.encode(txts[:64], batch_size=32, normalize_embeddings=True, show_progress_bar=False)

    print("\n %-8s %12s %12s %10s" % ("batch", "chunks/s", "hours@5.7M", "peak VRAM"))
    best = (0, None)
    for b in BATCHES:
        torch.cuda.empty_cache()
        torch.cuda.reset_peak_memory_stats()
        try:
            t = time.time()
            m.encode(txts, batch_size=b, normalize_embeddings=True, show_progress_bar=False)
            el = time.time() - t
            rate = len(txts) / el
            vram = torch.cuda.max_memory_allocated() / 1024 ** 3
            print(" %-8d %12.1f %12.1f %9.1f GB" % (b, rate, 5692937 / rate / 3600, vram))
            if rate > best[0]:
                best = (rate, b)
        except RuntimeError as e:
            print(" %-8d %12s   %s" % (b, "OOM", str(e).split("\n")[0][:50]))
            break

    print("\nfastest: batch %d at %.0f chunks/s -> %.1f h of GPU for pass 1"
          % (best[1], best[0], 5692937 / best[0] / 3600))
    print("set EMBED_BATCH in bin/index-build.py to that value.")
    print("\nNOTE: this is EMBEDDING only. Reading and chunking added ~27%% on wikem")
    print("and ~120%% on the PDF-heavy zimgit archive, and it runs on the CPU while")
    print("the GPU waits - so the wall-clock build is longer than any row above.")


if __name__ == "__main__":
    main()
