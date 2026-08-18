"""Read and edit a Jenkinsfile as text — a brace scanner, not a Groovy parser.

A real Groovy grammar is needed for exactly one thing, and it is the one
thing nothing here does: understanding what a pipeline *means* — which
stages run, what they do, whether the file would actually work.  Everything
this module is asked for is structural:

======================================  ==============================
task                                    needs a Groovy parser?
======================================  ==============================
structural floor check                  no — brace scanner
splice a stage into ``stages { }``      no — brace scanner
semantic understanding of the pipeline  yes — and we never need it
======================================  ==============================

The GitHub Actions side makes no stronger claim either: "must parse, and
must carry ``on`` and ``jobs``" is a floor, not an assertion that the
workflow works.  Jenkins gets the same floor without the dependency.

**Known limit, accepted.**  Groovy's slashy-regex-vs-division ambiguity
(``/foo/`` versus ``a / b``) is the genuinely hard part of lexing the
language, and ``_scan`` does not resolve it: it treats ``/`` as an ordinary
character, so a brace *inside* a slashy regex is counted as structural.
Rare in a Jenkinsfile, and the consequence is bounded — a confused scanner
makes the file fail to balance, ``insert_stage`` answers ``None``, and the
caller creates a new ``Jenkinsfile.qa-agent`` instead.  Degrading, never
corrupting.
"""

from dataclasses import dataclass

# Ordered longest-first: `"""` must be tested before `"`, or a triple-quoted
# block opens and closes on its first character.
_STRING_DELIMITERS = ('"""', "'''", '"', "'")


@dataclass(frozen=True)
class Span:
    """A half-open ``[start, end)`` range of the source text."""

    start: int
    end: int


def _skip_span(text: str, index: int) -> int | None:
    """Index just past a non-structural span starting at ``index``, if any.

    Non-structural means a brace inside it is data rather than syntax:
    string literals of all four Groovy flavours, ``//`` line comments and
    ``/* … */`` block comments.  Returns ``None`` when nothing starts here.

    An unterminated span runs to the end of the file, which makes the
    document fail to balance — the honest answer, and the one that routes
    the caller to "create a new file" rather than to a wrong edit.
    """
    if text.startswith("//", index):
        end = text.find("\n", index)
        return len(text) if end == -1 else end
    if text.startswith("/*", index):
        end = text.find("*/", index + 2)
        return len(text) if end == -1 else end + 2
    for delimiter in _STRING_DELIMITERS:
        if not text.startswith(delimiter, index):
            continue
        cursor = index + len(delimiter)
        while cursor < len(text):
            if text[cursor] == "\\":
                cursor += 2
                continue
            if text.startswith(delimiter, cursor):
                return cursor + len(delimiter)
            cursor += 1
        return len(text)
    return None


def _scan(text: str) -> list[Span]:
    """Every non-structural span in ``text``, in order.

    The one primitive the rest of this module rests on: with these ranges
    excluded, a brace is a block delimiter and nothing else.  Never raises.
    """
    spans: list[Span] = []
    index = 0
    while index < len(text):
        end = _skip_span(text, index)
        if end is None:
            index += 1
            continue
        spans.append(Span(index, end))
        index = end
    return spans


def _structural(text: str) -> str:
    """``text`` with every non-structural span blanked to spaces.

    Same length as the input, so every index into it is an index into the
    original — which is what lets ``insert_stage`` compute a real insertion
    point from a scan of the masked copy.
    """
    masked = list(text)
    for span in _scan(text):
        for index in range(span.start, span.end):
            if masked[index] != "\n":  # keep line structure for readability
                masked[index] = " "
    return "".join(masked)


def _matching_brace(masked: str, open_index: int) -> int | None:
    """Index of the ``}`` closing the ``{`` at ``open_index``, or ``None``."""
    depth = 0
    for index in range(open_index, len(masked)):
        if masked[index] == "{":
            depth += 1
        elif masked[index] == "}":
            depth -= 1
            if depth == 0:
                return index
    return None


def braces_balance(text: str) -> bool:
    """Whether every structural brace in ``text`` is matched.

    The precondition for every edit here: a file that does not balance is
    one this module has demonstrably failed to understand.
    """
    depth = 0
    for character in _structural(text):
        if character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0


def find_block(text: str, keyword: str) -> Span | None:
    """The span of ``keyword { … }``, from the keyword to its closing brace.

    ``None`` when the keyword does not appear as a block opener, or when its
    braces do not resolve.
    """
    masked = _structural(text)
    search_from = 0
    while True:
        index = masked.find(keyword, search_from)
        if index == -1:
            return None
        search_from = index + len(keyword)
        # Must be a whole word followed by `{` (whitespace allowed between).
        if index > 0 and (masked[index - 1].isalnum() or masked[index - 1] in "_.$"):
            continue
        cursor = index + len(keyword)
        while cursor < len(masked) and masked[cursor] in " \t\r\n":
            cursor += 1
        if cursor >= len(masked) or masked[cursor] != "{":
            continue
        close = _matching_brace(masked, cursor)
        if close is None:
            return None
        return Span(index, close + 1)


def floor_check(text: str) -> list[str]:
    """Structural problems with a Jenkinsfile — empty means it clears the floor.

    Every failure is reported rather than only the first: a validation error
    the user has to fix one round-trip at a time is barely better than none.

    This is a **floor**, deliberately not a claim that the pipeline runs.
    The Actions gate ("parses, carries ``on`` and ``jobs``") makes no
    stronger claim, and matching it is the point — a gate written for one
    provider and applied to the other is what let unvalidated Groovy through
    before.
    """
    problems = []
    if not braces_balance(text):
        problems.append("braces do not balance")

    declarative = find_block(text, "pipeline") is not None
    scripted = find_block(text, "node") is not None
    if not declarative and not scripted:
        problems.append("no 'pipeline { }' or 'node { }' block")

    if declarative:
        if find_block(text, "stages") is None:
            problems.append("declarative pipeline has no 'stages { }' block")
        elif "stage(" not in _structural(text):
            problems.append("declarative pipeline declares no stages")
    return problems


def insert_stage(text: str, stage_src: str) -> str | None:
    """Splice ``stage_src`` in as the last stage of the ``stages`` block.

    Returns ``None`` — never a guess — when the file does not brace-balance,
    when ``stages { }`` cannot be located, or when the result no longer
    balances.  The caller falls back to creating a new
    ``Jenkinsfile.qa-agent``, so this only ever acts on a file it
    demonstrably understands.  Same philosophy as ``add_job``'s round-trip
    guard on the Actions side.

    Everything outside the insertion point is byte-identical: the stage goes
    in immediately before the closing brace, and nothing is reformatted.
    """
    if not braces_balance(text):
        return None
    stages = find_block(text, "stages")
    if stages is None:
        return None

    close = stages.end - 1  # index of the `}` closing `stages`
    line_start = text.rfind("\n", 0, close) + 1
    closing_indent = text[line_start:close]
    body_indent = closing_indent + "    "

    block = "\n".join(
        f"{body_indent}{line}" if line.strip() else line
        for line in stage_src.strip("\n").splitlines()
    )
    output = f"{text[:line_start]}{block}\n{text[line_start:]}"

    if not braces_balance(output):
        return None
    return output
