# Security

## Reporting a vulnerability

Please report it privately: on this repository's **Security** tab, choose
**Report a vulnerability**. Do not open a public issue for it. You will get an
answer as soon as the maintainer can give one; this is a project maintained by
one person, so please allow a few days.

Only the latest release is supported.

## What to know about the node's exposure

1. **The node's surface listens on every network interface, port 8090, with no
   login**, so that a phone or a second computer on the same local network can
   use it. Anyone on that network can ask it questions and read the library.
   Do not expose it to the internet. To keep it to one machine, block port 8090
   in the Windows firewall, or start the node by hand with
   `13-ark-node\ark-api\serve.py --host 127.0.0.1` instead of `ark.py up`.
2. **kiwix-serve listens on port 8080** the same way.
3. **The model servers listen on 127.0.0.1 only** (ports 8091 and 8092), and
   that is not a setting: an answer engine with no citations on the room's
   network is what the project chose not to build.
4. **Setup downloads files from the internet** (Kiwix, Hugging Face, GitHub,
   PyPI, PyTorch). Every file in the catalog is checked against a sha256 or a
   git blob id before it is used, and a file that does not match is set aside,
   never used.
