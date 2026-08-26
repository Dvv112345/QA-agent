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
from urllib.parse import urlsplit

# Matches the separator used in the UI's single-line rendering.
_SEPARATOR = " · "


# ── What may leave the application ────────────────────────────────────


def url_values(env_vars: Mapping[str, str] | None) -> set[str]:
    """The environment values that are http(s) URLs.

    Answers **"may a human reading this be shown the value"** — a bug report
    about a page has to be allowed to name the page.  That is deliberately
    *not* the same question as "may this be a plain CI variable"; see
    :func:`ci_variable_values`, which is narrower, and do not collapse the
    two.
    """
    if not env_vars:
        return set()
    return {
        value
        for value in env_vars.values()
        if isinstance(value, str) and value.startswith(("http://", "https://"))
    }


def ci_variable_values(env_vars: Mapping[str, str] | None) -> set[str]:
    """The environment values safe to hold as a plain **CI variable**.

    A strictly narrower question than :func:`url_values`, and the two must
    not share a classifier.  "A human may read this in a ticket" is weaker
    than "this may sit in a world-readable store and go unmasked in CI
    logs": a repository variable is visible to anyone with read access and
    is printed verbatim in job output, where a ticket has an audience.

    So an ``http(s)`` value qualifies only when it carries no credential
    material — no ``user:pass@`` userinfo, and no query string, both of
    which routinely carry tokens.  Everything else falls through to the
    secret store, so the rule can only ever move a value toward the safer
    of the two.

    **Known limit:** a token embedded in a *path*
    (``/services/T00/B00/XXXX``) is indistinguishable from an ordinary
    route and is classified as a variable.  The PR trailer names every
    variable the team must create, so a reviewer sees it before it is
    stored — that visibility is the backstop, not this function.
    """
    if not env_vars:
        return set()
    safe = set()
    for value in env_vars.values():
        if not isinstance(value, str) or not value.startswith(("http://", "https://")):
            continue
        try:
            parts = urlsplit(value)
        except ValueError:  # a malformed URL is not one we will vouch for
            continue
        if parts.username or parts.password or parts.query:
            continue
        safe.add(value)
    return safe


def variable_and_secret_names(
    env_vars: Mapping[str, str] | None,
) -> tuple[list[str], list[str]]:
    """Environment variable **names**, split into CI variables and CI secrets.

    One home for the split, because two callers need the identical answer:
    the export page tells the team which repository secrets to create, and
    the export job commits references to them.  Two copies that drift name
    one set on screen and commit another.

    Values are read here only to sort the names and are then discarded —
    nothing downstream of this function ever sees one.
    """
    if not env_vars:
        return [], []
    safe = ci_variable_values(env_vars)
    variables = [name for name, value in env_vars.items() if value in safe]
    secrets = [name for name, value in env_vars.items() if value not in safe]
    return variables, secrets


# Below this length the replacement does more damage than the leak: a
# two-character value ("80", "qa") occurs inside ordinary prose constantly,
# and a bug report with every other word rewritten is not a bug report.
# Real credentials comfortably clear this; ports and short usernames do
# not, and those are not what the guard is for.
_MIN_REDACTABLE_LENGTH = 6


def redactable_items(
    env_vars: Mapping[str, str] | None,
    *,
    keep: Iterable[str] = (),
) -> dict[str, str]:
    """``{name: value}`` for every environment value that must not leave, minus ``keep``.

    Every environment value is a candidate credential except the ones
    naming the application itself.  Which ones those are is the caller's to
    decide, because the three exits differ on **who is reading**:

    * ``diagnose_and_fix_script`` — a model that was already handed
      ``env_var_names`` and is looking at a script that reads
      ``os.environ["BASE_URL"]``.  ``$BASE_URL`` fully resolves for it, so
      nothing is kept;
    * ``create_issue`` — a human opening a ticket, who may have no idea
      what ``$BASE_URL`` is set to.  A bug report about a page has to be
      allowed to name the page, so every http(s) value is kept
      (:func:`url_values`);
    * the exploratory action log — same reasoning, narrowed: that run
      knows exactly which variables its charters were pointed at, so it
      keeps those and nothing else.

    Collapsing these into one rule would silently change what is hidden at
    one of the three exits.
    """
    if not env_vars:
        return {}
    kept = set(keep)
    return {name: value for name, value in env_vars.items() if value not in kept}


def redact(text: str, secrets: Mapping[str, str]) -> str:
    """Replace each environment value in ``text`` with ``$NAME``.

    The variable's **name**, not a blanking placeholder, because redaction
    that destroys information buys safety by making the text useless:
    ``Connection refused to ***`` cannot be diagnosed, and it was the
    reason the diagnosis prompt had to be handed raw URLs at all.
    ``Connection refused to $BASE_URL`` is at least as useful — more so to
    a reader holding the variable names, since it links the failure to the
    line that produced it — and it keeps ``$PROD_URL`` and ``$BASE_URL``
    distinguishable where one shared placeholder collapses them.

    Substring matching, unlike the exploratory action log's exact match on
    a whole tool argument: prose leaks a credential mid-sentence ("logged
    in as admin/hunter2…") rather than as the entire value.  The cost is
    false positives, which is what ``_MIN_REDACTABLE_LENGTH`` bounds.

    Which values arrive here is the caller's decision — see
    :func:`redactable_items`.
    """
    if not text:
        return text
    # Longest value first, so a value containing another does not leave the
    # shorter one's replacement embedded in a half-rewritten string. Ties
    # break on the name, or two variables sharing one value would redact
    # differently between runs and the same stderr would not reproduce.
    ordered = sorted(secrets.items(), key=lambda item: (-len(item[1]), item[0]))
    for name, value in ordered:
        if value and len(value) >= _MIN_REDACTABLE_LENGTH:
            text = text.replace(value, f"${name}")
    return text


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
