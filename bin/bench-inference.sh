#!/usr/bin/env bash
# bench-inference.sh - the first numbers this build has ever had about inference.
#
#   bash bin/bench-inference.sh              # the three configs the cross-check turns on
#   bash bin/bench-inference.sh --quick      # shorter runs, for checking it works at all
#   LLAMA_BIN=/c/somewhere bash bin/bench-inference.sh
#
# WHY THIS EXISTS. Spec §9.4 wants two model families asked the same question so
# they can disagree. NODE-ARCHITECTURE §2 says build the trigger now and let the
# hardware make it cheap later. Both of those were written without a single
# tokens-per-second figure from this machine, because until 2026-09-04 no model in
# this archive had ever been loaded. 475GB of weights, never run.
#
# WHAT IT DECIDES. The cross-check needs a second model, and there are three ways
# to have one on 16GB of VRAM and 64GB of RAM. They are not close to equivalent
# and the difference is not arguable from the file sizes:
#
#   A  Qwen IQ4_XS on the GPU              the primary. Everything else is measured against it
#   B  Gemma 31B IQ4_XS on the CPU         both resident, SAME quantization, no swap.
#                                          Removes §9.4's asymmetry caveat. Costs CPU speed.
#   C  Gemma 31B Q3_K_M on the GPU         fast, but needs a model swap per cross-check
#                                          AND keeps the caveat: a quantization step below
#                                          the primary, so some disagreement is compression
#                                          noise rather than the models disagreeing.
#
# If B is usable, it is the right answer: it is the only one of the three where a
# disagreement means what §9.4 says it means. "Usable" is a number, and this script
# is how the number gets found rather than assumed.
#
# THROW THE FIRST RUN AWAY. IT IS NOT A MEASUREMENT.
# Measured 2026-09-04, and it nearly sent this whole benchmark down a false trail.
# The same model, the same flags, twice:
#
#     first invocation on a cold machine     pp512 1,016.9 t/s   tg  14.5 t/s
#     once warm                              pp512 3,099.2 t/s   tg  62.5 t/s
#
# Three times and four times out. Nothing about the model changed. The first run
# was measuring an idle GPU still ramping its clocks and a 6.6GB file being pulled
# off NVMe into a cold Windows file cache, both amortised over a window too short
# to escape them. Read as hardware, 14.5 t/s says the 4090 is running at a fifth
# of its memory bandwidth and something is badly wrong. Nothing was wrong.
#
# 62.5 t/s on a 7.1GB model is 444 GB/s of effective bandwidth against a
# theoretical 576, which is 77% and is what llama.cpp normally achieves. THAT is
# the machine. So this script burns a warm-up pass first and never reports it, and
# every measured configuration is repeated.
#
# WHY llama-bench AND NOT A STOPWATCH ON llama-cli. llama-bench reports prompt
# processing and token generation separately, repeats each run, and reports the
# spread. Hand-timing one llama-cli invocation measures the model load as if it
# were inference, and the load is exactly the thing that differs most between
# these three configurations.

set -u

ARK_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MODELS="$ARK_ROOT/01-models/tier1-reasoning"
LLAMA_BIN="${LLAMA_BIN:-$ARK_ROOT/llama}"   # ark.py paths.llama; set it if yours is elsewhere
OUT_DIR="$ARK_ROOT/_incoming"
STAMP="$(date +%Y%m%d-%H%M)"
OUT="$OUT_DIR/bench-inference-$STAMP.txt"

PP=512      # prompt tokens processed, the "reading the retrieved passages" number
TG=128      # tokens generated, the "writing the answer" number
REP=3       # never 1. A single sample has no spread, and llama-bench prints
            # "± 0.00" for it, which reads like precision and is its absence.
if [ "${1:-}" = "--quick" ]; then PP=128; TG=32; REP=2; fi

say()  { printf '\n=== %s ===\n' "$*"; }
fail() { printf '\nFAILED: %s\n' "$*" >&2; exit 1; }

# --- 1. the binaries -------------------------------------------------------
# They are NOT in the archive. 09-software/llamacpp-bin/ holds the publisher's
# zips as the artifact of record; extracting into that shelf would add ~56 files
# to a checksummed manifest that describes what was downloaded. The working copy
# lives outside the shelves, in paths.llama (<root>/llama unless ark.toml says).
[ -x "$LLAMA_BIN/llama-bench.exe" ] || fail "llama-bench.exe not found under $LLAMA_BIN
  Extract it first (see 00-docs/BUILD-LOG.md, first inference run):
    mkdir -p \"$LLAMA_BIN\"
    powershell -c \"Expand-Archive -Force '$ARK_ROOT/09-software/llamacpp-bin/llama-b10566-bin-win-cuda-12.4-x64.zip' '$(cygpath -w "$LLAMA_BIN")'\"
    powershell -c \"Expand-Archive -Force '$ARK_ROOT/09-software/llamacpp-bin/cudart-llama-bin-win-cuda-12.4-x64.zip' '$(cygpath -w "$LLAMA_BIN")'\"
  The cudart zip is NOT optional. The CUDA build will not start without it, and
  the error does not name the missing DLL."
[ -f "$LLAMA_BIN/cudart64_12.dll" ] || fail "cudart64_12.dll is missing from $LLAMA_BIN
  The CUDA runtime zip was not extracted alongside the binaries. See above."

# --- 2. the models ---------------------------------------------------------
QWEN="$MODELS/Qwen3.8-27B-IQ4_XS.gguf"
GEMMA_XS="$MODELS/google_gemma-4-31B-it-IQ4_XS.gguf"
GEMMA_Q3="$MODELS/google_gemma-4-31B-it-Q3_K_M.gguf"
for m in "$QWEN" "$GEMMA_XS" "$GEMMA_Q3"; do
  [ -f "$m" ] || fail "model missing: $m"
done

# THREADS. Not "all of them". The M18's P-cores and E-cores do not run at the
# same speed, and llama.cpp splits work evenly, so the E-cores set the pace for
# every core. Physical P-core count is the usual best guess; override and compare
# if a run looks wrong.
THREADS="${THREADS:-16}"

# --- 3. warm up, and discard it ---------------------------------------------
# Spins the GPU out of its idle clocks and pulls one model through the file cache.
# Its numbers are deliberately thrown away; see the note at the top of this file.
say "warm-up (discarded)"
"$LLAMA_BIN/llama-bench.exe" -m "$(cygpath -w "$QWEN")" -p 128 -n 32 -r 1 -ngl 48     >/dev/null 2>&1 || echo "  (warm-up run failed; continuing, but treat row A with suspicion)"

mkdir -p "$OUT_DIR"
{
  echo "bench-inference $STAMP"
  echo "host:    $(hostname 2>/dev/null)"
  echo "binaries:$LLAMA_BIN"
  echo "pp=$PP tg=$TG reps=$REP threads(cpu runs)=$THREADS"
  echo
} | tee "$OUT"

run() {
  local label="$1" model="$2"; shift 2
  say "$label"
  {
    echo
    echo "---- $label ----"
    echo "model: $(basename "$model")  ($(du -h "$model" | cut -f1))"
  } >> "$OUT"
  # cygpath: Git Bash rewrites anything path-shaped before a native binary sees
  # it, which is how kiwix-serve was handed 'C:\Users\<user>\C:/ark/...'
  # on 2026-09-04. The Windows form is passed explicitly instead.
  "$LLAMA_BIN/llama-bench.exe" -m "$(cygpath -w "$model")" \
      -p "$PP" -n "$TG" -r "$REP" "$@" 2>&1 | tee -a "$OUT"
}

run "A  Qwen3.8-27B IQ4_XS  - GPU, all layers      (the primary)" \
    "$QWEN"      -ngl 99
run "B  Gemma 4 31B IQ4_XS  - CPU only, 64GB RAM   (no swap, no asymmetry)" \
    "$GEMMA_XS"  -ngl 0 -t "$THREADS"
run "C  Gemma 4 31B Q3_K_M  - GPU, all layers      (fast, but a step below)" \
    "$GEMMA_Q3"  -ngl 99

say "done"
cat <<EOM

  Written to: $OUT

  A COLD FIRST RUN UNDERSTATES BY THREE TO FOUR TIMES. This script discards one
  before measuring anything. If you run llama-bench by hand, do the same, or you
  will conclude the GPU is broken. It is not; that was measured too.

  READ IT LIKE THIS.
    tg   token generation, tokens/sec. How fast an answer is written.
    pp   prompt processing, tokens/sec. How fast the retrieved passages are read.

  The question this answers is NOT "which model is faster". It is whether config
  B - the second opinion running in system RAM at the SAME quantization as the
  primary - is fast enough to be worth having. A cross-check that takes four
  minutes is still useful if it runs in the background while the operator reads
  the first answer. One that takes forty is not.

  If A fails to allocate, lower -ngl until it fits and record the number: that is
  the real layer budget of a 16GB laptop 4090 and nothing in this build knows it
  yet. llama-fit-params.exe will estimate it.
EOM
