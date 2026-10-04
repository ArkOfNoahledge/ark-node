# catalog.csv: every file a node is built from

**Generated. Do not edit by hand.** It is written by `bin/catalog-build.py` from
the archive's `MANIFEST.csv`, and `python bin/catalog-build.py --check` fails when
the two disagree. To change a row, change the manifest, then run the script.

One row per file or folder someone can obtain to build their own node: where to
get it, how to check it, and what its licence lets them do with it. The archive
itself (2.4 TB) is not in this repository and never will be; this is the list.

## Columns

| column | meaning |
|---|---|
| `id` | unique name for the row; the fetch tool keys on it |
| `shelf` | where it lives in the archive layout (`02-corpora-core`, `01-models/tier1-reasoning` ...) |
| `file` | its name on disk; a trailing `/` is a folder |
| `kind` | `zim`, `gguf`, `model-repo`, `pmtiles`, `pdf`, `software`, `folder`, `file` |
| `profile` | `starter` (the small first install) or `full` |
| `fetch` | how to get it, below |
| `fetch_url` | the URL to download, empty for `manual` |
| `revision` | the Hugging Face commit (or `main` where no commit is known yet), or the GitHub release tag |
| `sha256` | the checksum of the file as verified when it was filed; empty for folders |
| `bytes` | what a fetch of the row downloads: a file's size, or, for a Hugging Face repository, the files `fetch` takes at `revision` (from `pins.csv`, below) |
| `license` | the licence, as the project records it |
| `license_status` | `recorded` (stated in the project's credits file) or `to-verify` (read the upstream terms before relying on it) |
| `redistribute` | `yes`, `non-commercial`, `per-item`, `ask`, or `no` |
| `indexed` | whether the search index covers it (`no` means Kiwix only) |
| `source_page` | the page it was found on |
| `include` | for a Hugging Face repository the archive keeps only part of, the files it keeps, separated by `;`; `fetch` takes exactly those. Empty for every other row |

## How each row is fetched

| `fetch` | what it means |
|---|---|
| `direct` | the URL is the file. Kiwix URLs redirect to a mirror |
| `hf-file` | one file from a Hugging Face repository, at `revision` |
| `hf-repo` | a Hugging Face repository at `revision`, less images and, when a `.safetensors` carries the weights, the ONNX export and the `.bin`/`.pt` copies (BGE-M3: 2.3 GB of a 6.9 GB repository). A repository that ships only ONNX keeps it, because it is the model. Only the `include` files when that column is set (piper-voices: 10 of 3,301 files). `--all-files` takes everything |
| `github-asset` | a file attached to a GitHub release, `revision` is the tag |
| `manual` | a person has to get it: a store, a catalogue page, a form |

**Always check the sha256.** A file that does not match was not the file this
node was built and tested with. Kiwix publishes newer editions and removes older
ones, so a `direct` URL here will eventually stop working: the newer edition is
then a substitution, and it should be recorded as one, not quietly accepted.

## Pinned revisions, and `pins.csv`

A Hugging Face repository changes: model cards are edited, files are replaced,
quantizations are redone. A catalog row that says `main` means *whatever is there
today*, which is not necessarily what this node was built and measured with.

`pins.csv`, beside this file, records which commit each Hugging Face row's archive
copy came from. It is written by `python bin/ark.py fetch --pin`, run on a machine
that holds the archive: for each row it reads the repository's commits, newest
first, and takes the newest one whose files match the archive's copy (by sha256,
or by git blob id for small files). Nothing is downloaded. `catalog-build.py`
reads it into the `revision` column and the download URLs. A row that no commit
matches is recorded as `UNPINNED` with the closest commit and what differs; it
stays on `main` here, and the build lists it.

| `pins.csv` state | meaning |
|---|---|
| `PINNED` | a commit holds exactly the archive's copy; the catalog uses it |
| `CONFIRMED` | the catalog already named a commit, and the archive's copy matches it |
| `DISAGREES` | the catalog names a commit and the archive's copy is not it |
| `UNPINNED` | no commit read matches, or the host could not be asked; see `note` |

Two things `--pin` found in this archive, and how the catalog handles them:

- **Renamed files.** Some files were saved under a longer name than the
  repository's (`mmproj-BF16.gguf` became `mmproj-gemma-4-12b-it-BF16.gguf`).
  `--pin` finds them by sha256; `pins.csv`'s `path` is the repository's name, the
  download URL uses it, and `file` stays the archive's name.
- **Line endings.** The archive's small text files (configs, READMEs, templates)
  were saved with CRLF; upstream has LF. `--pin` compares them both ways and says
  which. For a single file the catalog lists the upstream sha256 and size, which
  is what a download produces, so the archive's own copy shows as different.

For a repository row `pins.csv` also records `fetch_files` and `fetch_bytes`:
what `fetch` takes at the pinned commit, added up from that commit's own file
list by the same rule the download uses. That is the row's `bytes` here, so the
size `fetch --list` shows is the size of what arrives, not of the archive's
folder (which may hold the skipped files too). A repository row without it keeps
the folder's size, and the build lists it.

A single file on `main` is still safe to fetch: its sha256 is here, and a changed
file is refused. A whole repository on `main` is checked only against the hashes
Hugging Face reports for today's `main`.

## What `redistribute` is for

A node built for someone else carries its archive with it. Most of it is under
licences that allow that (Wikipedia, Stack Exchange and most Kiwix archives are
CC BY-SA; the model weights are Apache-2.0 or MIT). Some of it is not:

- **Hesperian Health Guides** (`no`): purchased; copying for use is encouraged,
  republishing is prohibited. Buy your own at hesperian.org.
- **FAO publications, iFixit, Khan Academy** (`non-commercial`): a node sold or
  bundled commercially cannot carry them. Given away without charge, it can.
- **OpenStax, zimgit collections, Piper voices** (`per-item`): the licence varies
  by book or component.
- **TruePrepper, Skin of Color Society, Mapterhorn** (`ask`): all rights reserved
  or not yet known.

Nothing here is legal advice.
