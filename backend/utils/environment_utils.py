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
from collections.abc import Iterable, Mapping
from importlib.metadata import version

# Matches the separator used in the UI's single-line rendering.
_SEPARATOR = " · "


# ── What may leave the application ────────────────────────────────────


def url_values(env_vars: Mapping[str, str] | None) -> set[str]:
    """The environment values that are http(s) URLs."""
    if not env_vars:
        return set()
    return {
        value
        for value in env_vars.values()
        if isinstance(value, str) and value.startswith(("http://", "https://"))
    }


def redactable_values(
    env_vars: Mapping[str, str] | None,
    *,
    keep: Iterable[str] = (),
) -> frozenset[str]:
    """Environment values to blank out of outbound text, minus ``keep``.

    Both exits — the exploratory action log and an outbound ticket — hold
    the same rule: *every* environment value is a candidate credential,
    except the ones naming the application itself.  A URL is something a
    bug report has to be allowed to name, and redacting it would gut the
    report while protecting nothing.

    The two callers differ, deliberately, on what "the application" means,
    which is why ``keep`` is the caller's to compute rather than something
    this function decides:

    * the exploratory task keeps the run's **nominated base URLs** — it
      knows exactly which variables the charters were pointed at;
    * export keeps **every** http(s) value (:func:`url_values`) — it has
      no run to ask, and a URL left unredacted is the safer error there.

    Collapsing those into one rule would silently change what gets
    redacted at one of the two exits.
    """
    if not env_vars:
        return frozenset()
    return frozenset(set(env_vars.values()) - set(keep))


def os_environment() -> str:
    """Host OS and build, e.g. ``Windows-11-10.0.26200-SP0``.

    Whatever ``platform.platform()`` reports, verbatim.  Older Pythons name
    Windows 11 builds ``Windows-10-…``; the build number is the real
    discriminator either way, and rewriting the string to a marketing name
    would be inventing something we cannot actually read.
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

    Guarded as a whole rather than statement by statement: every argument
    comes from a live browser via ``BrowserSession._environment``, which
    calls this *outside* its own suppress blocks, and an exception here
    would propagate through ``record_finding`` into the exploration loop —
    ending the session and discarding the action log of a charter that had
    already spent its budget.  The OS is always available as a floor.
    """
    parts: list[str] = []
    try:
        if browser_label:
            parts.append(str(browser_label))
        # Guarded on its own so a viewport that isn't a mapping costs only
        # the viewport — the browser and URL are still worth reporting.
        with contextlib.suppress(Exception):
            if viewport:
                width, height = viewport.get("width"), viewport.get("height")
                if width and height:
                    parts.append(f"viewport {width}x{height}")
        parts.append(os_environment())
        if url:
            parts.append(str(url))
        return _SEPARATOR.join(parts)
    except Exception:  # pragma: no cover — the floor, not a known path
        return os_environment()
