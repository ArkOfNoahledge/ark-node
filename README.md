# Ark node

**An offline answer engine for a Windows PC with an NVIDIA card.** Ask it a
question and it answers from a library on your own disk, and every claim in the
answer links to the page it came from. No account, no cloud, no network once it
is set up.

[Leer en español](README.es.md) · [arkofnoahledge.org](https://arkofnoahledge.org)

![Every citation opens the page it came from: Nuclear War Survival Skills, page 116, in Kiwix, on the same machine](docs/images/source.webp)

## What makes it different

1. **Every answer cites a page you can open offline.** The passages sit beside the
   answer, and each citation opens the original in [Kiwix](https://kiwix.org), on
   the same machine.
2. **The model's own knowledge is labelled.** When the model adds something no
   passage supports, that block says so, and it is never shown as coming from the
   library.
3. **A second model family can check the cited part.** A model from a different
   developer reads the same passages and the node compares the two answers. This
   needs the cross-check model, which the starter kit does not download (see
   below).
4. **A safety detector flags doses, chemicals and voltages** and asks you to read
   the source before you act.
5. **Bilingual**, English and Spanish, in the surface and in the library.
6. **It scales down and up.** The starter kit is 20 GB. The same tools build the
   reference archive: 2.4 TB, 39 million indexed passages, served from a laptop.
7. **Built to last.** Checksums for every file, a catalog pinned to exact
   versions, and recovery steps written to be followed from paper.

![A second model family reads the same passages, and the node compares the two answers](docs/images/check.webp)

## Quick start: the starter kit

**You need:**

1. Windows 10 or 11, 64-bit.
2. An NVIDIA card with 6 GB of memory or more, and an NVIDIA driver from 2023
   or later. 16 GB is the size tested end to end.
3. 16 GB of system RAM.
4. About 35 GB of free disk.
5. Python 3.12, installed as below.
6. An internet connection for setup only: about 23 GB of downloads with a
   16 GB card, about 10 GB with a 6 or 8 GB card.

**Install Python first** (skip this if `python --version` already prints
`Python 3.12`):

1. Go to [python.org/downloads/windows](https://www.python.org/downloads/windows/),
   find the newest **Python 3.12** release, and under it click **Download
   Windows installer (64-bit)**. Not the big button on the main downloads page:
   that is a newer Python than this was tested with.
2. Run it. On the first screen tick **Add python.exe to PATH** at the bottom,
   then click **Install Now**.
3. **Close PowerShell and open a new one**, so it sees the new Python.
4. Check: `python --version` should print `Python 3.12.x`. If it prints a
   message about the Microsoft Store instead, open *Settings*, *Apps*,
   *Advanced app settings*, *App execution aliases*, turn off the two *App
   Installer* entries for `python.exe` and `python3.exe`, and open a new
   PowerShell.

**Then:**

1. Download this repository (*Code*, then *Download ZIP*) and extract it to a
   short folder with no spaces, for example `C:\ark`. Or clone it there with git.
2. Open PowerShell in that folder (in File Explorer, open the folder,
   right-click an empty spot, *Open in Terminal*), and run:

   ```
   python bin\ark.py setup --profile starter --plan
   python bin\ark.py setup --profile starter
   ```

   The first command shows what will happen and changes nothing. The second does
   it: it picks the model for your card, downloads and checks every file, installs
   the Python environment, builds the search index on your GPU, starts the
   servers, and asks a first question in English and in Spanish.
3. When it finishes, open **http://localhost:8090** and ask.

On the reference machine (RTX 4090 Laptop, 16 GB) the whole setup took **70
minutes**: about 30 to download at 85 Mbit/s and about 30 to build the index.
If anything stops it, run the same command again: finished steps are skipped.
If it stops with a message, see [Troubleshooting](#troubleshooting).

**Every day after that:**

```
python bin\ark.py up        start the servers
python bin\ark.py status    what is running, and why anything is not
python bin\ark.py down      stop them
```

`bin\ark.cmd` does the same with a double click.

## What the starter kit contains

| | |
|---|---|
| Water treatment, medicine and post-disaster guides | three [zimgit](https://download.kiwix.org/zim/other/) collections of field manuals and handbooks, English |
| Medicine | Wikipedia's medicine articles, Spanish |
| The answering model | chosen for your card: Qwen3.8-27B on 16 GB, Gemma 4 12B on 12 GB, Phi-4-mini on 8 GB |
| The search model | BGE-M3, multilingual |
| The programs | llama.cpp and kiwix-tools, Windows builds |

Everything is listed, with its source, checksum and licence, in
[`13-ark-node/catalog/catalog.csv`](13-ark-node/catalog/catalog.csv) (see its
[README](13-ark-node/catalog/README.md)).

**The cross-check model is not in the starter kit.** To add it on a 16 GB card
with 32 GB of RAM or more:

```
python bin\ark.py fetch google_gemma-4-31b-it-iq4_xs
```

then set `enabled = true` under `[models.crosscheck]` in `ark.toml` and run
`python bin\ark.py up`.

## Going further

```
python bin\ark.py config --profiles                 the model pairs for 8, 12, 16 and 24 GB cards
python bin\ark.py fetch --list --profile full       every row of the full catalog, about 2.1 TB
python bin\ark.py fetch ID                          download any row, checked by sha256
python bin\ark.py verify                            check every file against its checksums
python bin\ark.py selftest                          the tools' own tests; needs no GPU
python bin\ci.py                                    every check CI runs; needs no archive
```

More content is indexed with `bin\index-build.py` and a scope file, then made
searchable with `python bin\ark.py index`.
[`13-ark-node/README.md`](13-ark-node/README.md) describes how the node works and
why, and [`13-ark-node/RECOVERY.md`](13-ark-node/RECOVERY.md) is the recovery
procedure.

![The Kiwix library of the reference archive](docs/images/library.webp)

*The screenshots are from the reference archive, which holds more than the
starter kit.*

## Troubleshooting

Setup checks each step and stops at the first one that did not work, with the
reason. Fix that, then run the same command again.

| You see | What to do |
|---|---|
| *Python was not found; run without arguments to install from the Microsoft Store* | Python is not installed, or this PowerShell window was opened before it was. See *Install Python first*. |
| *no NVIDIA card found* | Install or update the driver from [nvidia.com/drivers](https://www.nvidia.com/drivers), then check that `nvidia-smi` lists your card. |
| *this NVIDIA card has N MiB, and the smallest profile ... needs about ...* | The card is too small for any model profile. `--models 8gb` tries anyway. |
| *the server's certificate could not be verified* | Your network inspects encrypted traffic (some companies, schools and antivirus products do). Get its certificate file from whoever runs the network, then in PowerShell: `$env:SSL_CERT_FILE = "C:\path\to\certificate.pem"`, and run setup again in the same window. |
| *will not import: torch ... DLL load failed* | Install the [Microsoft Visual C++ Redistributable (x64)](https://aka.ms/vs/17/release/vc_redist.x64.exe), then run setup again. |
| *torch ... does not see an NVIDIA GPU* | Update the NVIDIA driver, then run setup again. |
| Windows asks whether to allow Python or kiwix-serve on networks | *Allow* on private networks lets a phone or another computer on your network use the node. *Cancel* keeps it to this computer. Either works. |

## Read this before you rely on it

**This is not medical, legal or engineering advice, and it is not a professional.**
It is a search tool over documents written by others, with a language model that
can be wrong. Read the cited passage before you act on any dose, mixture, voltage
or procedure, which is what the amber banner on the surface asks of you. Where it
matters, ask a qualified person.

**Known limits of this release:**

1. Tested on one machine: an RTX 4090 Laptop (16 GB) with 64 GB of RAM. The 8 GB,
   12 GB and 24 GB model pairs fit their cards but have not been through the
   node's acceptance questions.
2. The starter kit runs without the cross-check model, so the model's added
   knowledge is labelled but not checked.
3. Now and then the answering model returns its own reasoning instead of an
   answer, under a normal citation badge. If an answer reads like someone
   thinking aloud, ask again and read the passages.
4. Windows and NVIDIA only. The code runs on Linux, but `setup` downloads Windows
   builds.
5. A reference build maintained by one person, best effort.

## Licences

1. **Code:** Apache License 2.0, see [LICENSE](LICENSE).
2. **Documentation:** Creative Commons Attribution 4.0, see
   [LICENSE-docs](LICENSE-docs).
3. **The content you download** keeps its own licence, listed per row in the
   catalog with whether it may be redistributed. Some of it may not be: read that
   column before you build a node for someone else.
4. **The programs setup downloads** keep theirs: see [NOTICE](NOTICE).
5. The name *Ark of Noahledge* is not licensed by either.

## The project

The node is the working half of [Ark of Noahledge](https://arkofnoahledge.org), a
design for an offline, low-power knowledge machine that stays useful with no
internet and no grid. The design, the full catalog and these tools are free. A
book that walks through building one is the guided, paid path.

Created by Juan Hurtado, founder of Ark of Noahledge
([LinkedIn](https://www.linkedin.com/in/juaneshurtado)), with the Ark of Noahledge contributors.

Contributions are welcome: see [CONTRIBUTING.md](CONTRIBUTING.md). Security
problems: see [SECURITY.md](SECURITY.md).
