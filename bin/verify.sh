#!/usr/bin/env bash
# Walk every CHECKSUMS.sha256 and report failures.
# This is the quarterly verification required by spec §10.1.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

SUM=$(command -v sha256sum || command -v gsha256sum)
[ -z "$SUM" ] && { echo "need sha256sum (brew install coreutils on macOS)"; exit 1; }

fail=0; checked=0
printf '\n=== CHECKSUM VERIFICATION — %s ===\n\n' "$(date +%F)"

while IFS= read -r f; do
  d=$(dirname "$f")
  printf '%s ... ' "$d"
  if (cd "$d" && "$SUM" -c --quiet CHECKSUMS.sha256 2>/dev/null); then
    n=$(grep -c . "$f")
    printf 'OK (%s files)\n' "$n"
    checked=$((checked + n))
  else
    printf 'FAILED\n'
    (cd "$d" && "$SUM" -c CHECKSUMS.sha256 2>&1 | grep -v ': OK$' | sed 's/^/    /')
    fail=$((fail + 1))
  fi
done < <(find . -name CHECKSUMS.sha256)

printf '\n%s files checked across all directories.\n' "$checked"
if [ "$fail" -gt 0 ]; then
  printf 'RESULT: %s director(ies) FAILED. Restore from cold copy before proceeding.\n\n' "$fail"
  exit 1
fi
printf 'RESULT: all directories passed.\n\n'
