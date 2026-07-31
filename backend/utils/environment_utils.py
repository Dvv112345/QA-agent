"""Where a finding was observed, as a single human-readable line.

A finding is only actionable if the reader knows what it was observed on: a
bug that reproduces at 375px wide and not at 1280px is a different bug, and
"works for me" is usually a platform difference nobody wrote down.

Captured in code, never asked of the LLM.  The model cannot see the browser
build or the host OS, so asking it would buy a plausible-looking guess in
place of a fact.

Every function here is best-effort and **never raises**: a finding that
could not describe its environment is still a finding, and losing one to a
formatting helper would be absurd.  Callers can use the result unguarded.
"""

from __future__ import annotations

import contextlib
import platform
import sys
from importlib.metadata import version

# Matches the separator used in the UI's single-line rendering.
_SEPARATOR = " · "


def os_environment() -> str:
    """Host OS and build, e.g. ``Windows-10-10.0.26200-SP0``.

    ``platform.platform()`` reports Windows 11 as ``Windows-10-…`` — the
    build number is the real discriminator, and inventing the marketing
    name would be guessing at something we cannot actually read.
    """
    try:
        return platform.platform()
    except Exception:  # pragma: no cover — platform probing is OS-dependent
        return sys.platform


def script_environment() -> str:
    """Environment for a finding produced by a generated test script.

    Names the *worker host*: ``script_runner`` runs the script as a
    subprocess under ``sys.executable``, so this is genuinely where it ran.

    Deliberately no browser version.  A script may drive Playwright or may
    just call ``requests``, and the only way to learn which Chromium it
    would have launched is to launch one.  The Playwright package version
    is the honest, free approximation.
    """
    parts = [os_environment()]
    # Each probe is guarded separately so one unavailable detail costs only
    # itself. importlib.metadata can raise more than PackageNotFoundError on
    # a damaged install, and this module promises never to raise.
    with contextlib.suppress(Exception):
        parts.append(f"Python {platform.python_version()}")
    # PackageNotFoundError is the expected case — normal in a CI environment
    # that installs no browser stack — but not the only one worth surviving.
    with contextlib.suppress(Exception):
        parts.append(f"Playwright {version('playwright')}")
    return _SEPARATOR.join(parts)


def browser_environment(
    browser_label: str | None,
    viewport: dict[str, int] | None,
    url: str | None,
) -> str:
    """Environment for a finding observed in a live exploratory session.

    Every part is optional because every part can be unavailable: the
    browser handle may not have been opened, and a closed or crashed page
    answers neither for its viewport nor its URL.  With all of them missing
    this still returns the OS, so the field is never blank.
    """
    parts: list[str] = []
    if browser_label:
        parts.append(browser_label)
    # Guarded rather than trusted: the viewport comes straight back from
    # Playwright, and a value that isn't a mapping would otherwise raise
    # AttributeError out of a function callers use unguarded.
    with contextlib.suppress(Exception):
        if viewport:
            width, height = viewport.get("width"), viewport.get("height")
            if width and height:
                parts.append(f"viewport {width}x{height}")
    parts.append(os_environment())
    if url:
        parts.append(url)
    return _SEPARATOR.join(parts)
