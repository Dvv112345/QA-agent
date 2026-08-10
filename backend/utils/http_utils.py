"""The SSL context every outbound HTTPS client in this app must use.

On Windows ``SSL_CERT_FILE`` may point at a file that does not exist, which
breaks the *construction* of any ``httpx`` client relying on the library
default — including the one inside the OpenAI SDK.  Pinning certifi's bundle
sidesteps the environment entirely.

This lives in one module because it is a rule rather than a preference: a new
outbound client that forgets it works on Linux and fails on a developer's
Windows box.  Import ``SSL_CONTEXT``; do not build your own.
"""

from __future__ import annotations

import ssl

import certifi

SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
