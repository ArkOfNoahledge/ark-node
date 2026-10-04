#!/usr/bin/env bash
# Regenerate CHECKSUMS.sha256 for a directory after files have been added or
# removed. Required after any deletion — the existing file records what was
# present at intake, so verify.sh will otherwise report failure that looks like
# corruption but is not.
#
#   usage: bin/rehash.sh <directory>
set -euo pipefail
cd "$(dirname "$0")/.." || exit 1

d="${1:-}"
[ -z "$d" ] && { echo "usage: bin/rehash.sh <directory>"; exit 1; }
[ -d "$d" ] || { echo "no such directory: $d"; exit 1; }

SUM=$(command -v sha256sum || command -v gsha256sum)
[ -z "$SUM" ] && { echo "need sha256sum"; exit 1; }

old=0
[ -f "$d/CHECKSUMS.sha256" ] && old=$(grep -c . "$d/CHECKSUMS.sha256")

# Write the temp file OUTSIDE the directory being hashed. Writing it inside
# means the shell creates it before find runs, find sees it, and sha256sum
# hashes its own half-written output — producing a CHECKSUMS entry for a file
# that is renamed away moments later. verify.sh then reports FAILED open or read.
tmp=$(mktemp)
trap 'rm -f "$tmp"' EXIT

# library.xml IS EXCLUDED, DELIBERATELY, AND THIS IS THE ONLY EXCLUSION.
# From 2026-09-12 the Kiwix library lives at the ARCHIVE ROOT and stores ABSOLUTE
# ZIM paths, one file covering all six corpus shelves. An absolute path is opened
# directly by kiwix-serve and the book's URL key is the bare basename, which is
# what lets one library serve 67 books across six shelves. The root is not itself
# a checksummed shelf, and the pre-09-12 copy inside 07-corpora-supplemental was
# removed on 2026-09-12, so the exclusion below currently catches nothing. IT
# STAYS ANYWAY: a cold-copy restore or an older bin/kiwix-library.py can put a
# library back inside a shelf, and this file's whole job is that the manifest
# never drifts for a generated, machine-local file. An exclusion that matches
# nothing today costs nothing; one that is missing the day the file returns
# makes a 1.6 TB shelf report a mismatch that is indistinguishable from real
# corruption. DECISIONS.md 2026-09-12, BUILD-LOG.md 2026-09-12b.
#
# SUPERSEDED, KEPT BECAUSE THE COMMENT ITSELF WENT STALE FOR A DAY: from
# 2026-09-04 to 2026-09-12 this said the library must live INSIDE
# 07-corpora-supplemental, beside the ZIMs, because the stored path is relative
# to the library file - so a library one directory up produced the key
# "07-corpora-supplemental/mdwiki_..." and every ARTICLE under it 404d. That was
# true of RELATIVE paths and was recorded as though it were true of the server.
#
# It is a generated, machine-local file: bin/kiwix-library.py rewrites it on every
# run, and the absolute ZIM paths it is built from differ per machine. Checksumming
# it would mean the shelf's manifest drifts every time the library is rebuilt, and
# a 1.6 TB shelf reporting a mismatch is indistinguishable from real corruption at
# the moment you most need to trust it. verify.sh only checks files the manifest
# lists, so excluding it here is sufficient and complete.
#
# THE COST OF THAT EXCLUSION IS NOW LARGER AND IS NOT THIS SCRIPT'S TO PAY.
# Absolute paths make library.xml machine-specific, and because it is excluded,
# verify.sh can never report a restored archive's library as wrong: kiwix-serve
# will load it, print success, list every book, and every link will fail. The
# mitigation is in RECOVERY.md step 2 and in both operator manuals - rebuild the
# library before serving anything - not in a hash.
# __pycache__ IS EXCLUDED FOR THE SAME REASON, and it was already causing this.
# bin/CHECKSUMS.sha256 listed three .pyc files on 2026-09-04. Python writes them
# whenever a tool is imported rather than run - index-query.py and the node's
# store.py both import bin/index-bm25.py by path - so they appear, change, and
# multiply per interpreter version. Three were present: two cpython-312 from this
# machine and one cpython-310 written by a session running the same tools across
# the bridge. Every one of those is a manifest change that looks like a modified
# tool. Bytecode is a cache of a file already checksummed; there is nothing in it
# to protect.
# 'Claude outputs' IS EXCLUDED FOR THE SAME REASON, added 2026-09-07. The Cowork
# desktop app writes a session's output files into a directory of that name inside
# whichever folder is connected, and on 2026-09-06 two of them landed in 12-Ebook -
# a shelf that is checksummed, carries .no-index, and is excluded from every public
# repository because it is unpublished commercial work. They are not archive
# payload, they arrive without the archive's knowledge, and hashing them means a
# shelf reports a mismatch because a session wrote a scratch file. Same class as
# __pycache__: machine-generated, outside the archive's control, and a manifest
# change that looks like a modified artifact.
( cd "$d" && find . -type f ! -name 'CHECKSUMS.sha256*' ! -name 'library.xml' \
    ! -path '*/__pycache__/*' ! -path '*/Claude outputs/*' -print0 | xargs -0 "$SUM" ) > "$tmp"
# Atomic rename where the filesystem allows it. Some mounts - the Cowork bridge
# to this machine, for one - permit writing a file but not REPLACING one, so the
# rename fails with "unable to remove target" while `cat >` succeeds. Falling back
# to a truncate-and-copy keeps this script usable from either side; the temp file
# is still built first, so a failure part-way through hashing never reaches the
# manifest. Only the final copy is non-atomic, and it is re-runnable.
if mv "$tmp" "$d/CHECKSUMS.sha256" 2>/dev/null; then
  :
else
  cat "$tmp" > "$d/CHECKSUMS.sha256" || {
    echo "could not write $d/CHECKSUMS.sha256" >&2; exit 1; }
fi
trap - EXIT

new=$(grep -c . "$d/CHECKSUMS.sha256")
printf 'rehashed %s: %s -> %s files' "$d" "$old" "$new"
[ "$old" -ne 0 ] && printf ' (%+d)' $((new - old))
printf '\n'

# self-check: every listed path must exist
missing=0
while read -r _ f; do
  f="${f#\*}"
  [ -e "$d/$f" ] || { echo "  MISSING: $f"; missing=$((missing+1)); }
done < "$d/CHECKSUMS.sha256"
if [ "$missing" -gt 0 ]; then
  echo "  ERROR: $missing entr(ies) reference files that do not exist."
  exit 1
fi
echo "  all listed files present."
# The MANIFEST row check. Until 2026-09-07 this was an unconditional
#     NOTE: update the file count in this artifact's MANIFEST.csv row
# printed on every run whether or not anything had moved. On that day every file
# count in MANIFEST.csv was correct and four byte totals were stale, one of them
# by 205 KB - so the note fired, named the column that was already right, and
# said nothing about the one that was wrong. A check that always fires trains you
# to ignore it, and this one had earned that. It now compares both quantities
# against the artifact's own row and prints nothing when they agree.
#
# BYTES ARE DEFINED AS THE SET CHECKSUMS.sha256 LISTS - the same find filter used
# above, so the manifest itself, library.xml and __pycache__ are excluded.
# Counting CHECKSUMS.sha256 in its own artifact's total would make the number
# drift on every rehash and never settle.
bytes=$( cd "$d" && find . -type f ! -name 'CHECKSUMS.sha256*' ! -name 'library.xml' \
    ! -path '*/__pycache__/*' ! -path '*/Claude outputs/*' -printf '%s\n' 2>/dev/null \
    | awk '{s+=$1} END {print s+0}' ) || bytes=""
key=$(basename "$d")
row=$(grep -m1 "^$key,(directory)," MANIFEST.csv 2>/dev/null) || row=""

if [ -z "$row" ]; then
  # Most shelves are described by one MANIFEST row per download rather than by a
  # (directory) row - 01-models has 52 of them, 07-corpora-supplemental has 65.
  # Those rows are complete without a directory row, so saying anything here would
  # make this check fire forever on nine artifacts and become the thing it was
  # written to replace. Only an artifact absent from MANIFEST.csv entirely is worth
  # a word.
  if ! grep -qE "^$key[,/]" MANIFEST.csv 2>/dev/null; then
    printf '\n  MANIFEST.csv has no row of any kind for %s. Measured %s files, %s bytes.\n' \
        "$key" "$new" "${bytes:-unknown}"
  fi
else
  rec_bytes=$(printf '%s' "$row" | cut -d, -f4)
  rec_files=$(printf '%s' "$row" | grep -o '[0-9][0-9]* files' | head -1 | cut -d' ' -f1) || rec_files=""
  stale=0
  [ "$rec_files" != "$new" ] && stale=1
  if [ -n "$bytes" ] && [ "$bytes" != "0" ] && [ "$rec_bytes" != "$bytes" ]; then stale=1; fi
  if [ "$stale" -eq 1 ]; then
    printf '\n  MANIFEST.csv row for %s is stale:\n' "$key"
    if [ "$rec_files" != "$new" ]; then
      printf '    files: %s recorded, %s actual\n' "${rec_files:-none found}" "$new"
    fi
    if [ -n "$bytes" ] && [ "$bytes" != "0" ] && [ "$rec_bytes" != "$bytes" ]; then
      printf '    bytes: %s recorded, %s actual\n' "${rec_bytes:-none found}" "$bytes"
    fi
  fi
fi
