# Contributing

Thank you for looking. Issues, fixes and measurements are welcome.

## How this repository is made

This repository is exported from the maintainer's reference archive, the
machine the node was built and measured on. Code is written and tested there,
then copied here by an export that checks every file against an allowlist. So:

1. A pull request is read, tested on the reference machine, applied there, and
   arrives back here with the next export. Your authorship is kept in the commit
   message (`Co-authored-by`).
2. Small, focused pull requests are much easier to take than large ones.
3. Open an issue first for anything that changes behaviour, adds a dependency,
   or touches what an answer says.

## Before you open a pull request

1. Run `python bin\ci.py` (or `python3 bin/ci.py`): every check the CI runs on
   each push, needing no GPU, no archive and no downloads. Every check must
   pass. Two need numpy and faiss-cpu and one needs Node.js; without them they
   are skipped and named.
2. Say what you tested it on: Windows version, GPU, RAM.
3. If the change affects retrieval or answers, show a measurement, before and
   after, on named questions. "Seems better" is not enough for a tool people may
   act on.

## How the code is written

1. **`bin/ark.py` and the node (`13-ark-node/ark-api/`) use the Python standard
   library only**, except for the embedding stack the node loads when it is
   there (numpy, faiss, sentence-transformers). Please keep it that way.
2. **Line endings are LF**, and `.gitattributes` keeps git from converting them.
   Every file is checksummed, and a converted file is a different file.
3. **Comments explain why.** Many say what went wrong and when, in capitals at
   the start. They are the project's memory; please add to them rather than
   trim them.
4. **The surface is bilingual.** A sentence a person reads exists in English and
   in Spanish.
5. **Safety wording is deliberate.** Changes to the safety detector, the banners
   or the labels on unsourced text need an issue first.

## Licence of contributions

By contributing you agree that your contribution is licensed under the Apache
License 2.0 (code) or CC BY 4.0 (documentation), the same as the rest of this
repository.

## Conduct

See [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
