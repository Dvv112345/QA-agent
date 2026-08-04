"""Group findings that describe one defect, before any ticket is filed.

Two stages, and the order matters.  A deterministic prefilter collapses
findings whose text is the same once normalized — the common case, since
one broken dependency makes every test case in a plan fail with the same
words.  What survives goes to a single LLM call, which catches the
paraphrases the prefilter cannot: "checkout returns 500" and "the order
endpoint errors on submit" are one defect and share not a single token.

Pure over its inputs and **never raises**.  It is called from a worker
task at the end of a run that has already spent its whole budget, so a
grouping failure has to cost a duplicate ticket, never the findings.
That is also why the LLM stage degrades to the prefilter's answer rather
than to "no grouping": the deterministic pass is the floor, not the
fallback of last resort.
"""

import logging
import re
import unicodedata
from dataclasses import dataclass

from backend.services import llm
from backend.services.llm_prompts import FiledFinding, FindingCandidate

logger = logging.getLogger(__name__)

_SEVERITY_RANK = {"high": 0, "medium": 1, "low": 2}

# Digits go with the punctuation: a finding's text routinely carries an
# id, a timestamp, or a generated email that differs per run while the
# defect does not ("order 8814 was not created" / "order 9021 was not
# created"). Keeping them would defeat the prefilter on exactly the
# findings it exists for.
_NOISE_RE = re.compile(r"[\W\d_]+", re.UNICODE)


@dataclass(frozen=True)
class FindingGroup:
    """One ticket's worth of findings.

    ``representative`` and ``duplicates`` are indices into the
    ``candidates`` list the caller passed in — the caller owns the rows,
    this module only ever sees text.
    """

    representative: int
    duplicates: list[int]
    # An already-filed ticket describing this same defect. The caller
    # checks it is still open before adopting it: a closed ticket is a
    # decision somebody made, and reopening it silently would undo that.
    existing_key: str | None = None

    @property
    def members(self) -> list[int]:
        return [self.representative, *self.duplicates]


def _normalize(*fields: str) -> str:
    """A comparison key that ignores wording noise but not wording.

    Casefold, strip accents, drop punctuation and digits, collapse
    whitespace.  Deliberately conservative: this is an *equality* test,
    so anything it discards is something two reports of the same defect
    would differ on for no reason.
    """
    joined = " ".join(field or "" for field in fields)
    decomposed = unicodedata.normalize("NFKD", joined)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return " ".join(_NOISE_RE.sub(" ", stripped.casefold()).split())


def _elect(candidates: list[FindingCandidate], indices: list[int]) -> int:
    """The member whose report becomes the ticket.

    Chosen **in code**, never by the model: the ticket's title and body
    come from this row, so letting the model pick would let a grouping
    call quietly decide how a defect is described.  Highest severity
    first, then earliest position — the most alarming report of a defect
    is the one worth reading first, and ties resolve to the one observed
    soonest.
    """
    return min(
        indices,
        key=lambda i: (_SEVERITY_RANK.get(candidates[i].severity, len(_SEVERITY_RANK)), i),
    )


def _prefilter(
    candidates: list[FindingCandidate],
    already_filed: list[FiledFinding],
) -> tuple[list[list[int]], dict[int, str]]:
    """Collapse exact-equal normalized findings and match them to tickets.

    Returns the buckets (lists of candidate indices, in first-seen order)
    and, per bucket index, the key of a filed ticket whose own normalized
    text matches.  ``already_filed`` arrives newest-first, so the first
    match found is the most recent ticket for that defect.
    """
    filed_by_key: dict[str, str] = {}
    for filed in already_filed:
        key = _normalize(filed.title, filed.expected, filed.actual)
        # setdefault keeps the *first* — newest — key for a repeated defect.
        filed_by_key.setdefault(key, filed.issue_key)

    buckets: list[list[int]] = []
    seen: dict[str, int] = {}
    matched: dict[int, str] = {}
    for index, candidate in enumerate(candidates):
        key = _normalize(candidate.title, candidate.expected, candidate.actual)
        if key in seen:
            buckets[seen[key]].append(index)
            continue
        seen[key] = len(buckets)
        buckets.append([index])
        if key in filed_by_key:
            matched[len(buckets) - 1] = filed_by_key[key]
    return buckets, matched


def _merge_with_llm(
    candidates: list[FindingCandidate],
    already_filed: list[FiledFinding],
    buckets: list[list[int]],
    matched: dict[int, str],
) -> tuple[list[list[int]], dict[int, str]]:
    """Ask the model to merge buckets the prefilter left apart.

    One representative per bucket is shown, not every member: the members
    are already known to be textually identical, so the extra copies add
    prompt size and nothing else.
    """
    leaders = [bucket[0] for bucket in buckets]
    result = llm.group_findings([candidates[i] for i in leaders], already_filed)

    merged: list[list[int]] = []
    merged_matches: dict[int, str] = {}
    placed: set[int] = set()
    valid_keys = {filed.issue_key for filed in already_filed}

    for group in result.groups:
        # The model answers in *leader* positions, so every index has to
        # be range-checked and de-duplicated before it indexes anything.
        positions = [
            position
            for position in dict.fromkeys(group.indices)
            if 0 <= position < len(buckets) and position not in placed
        ]
        if not positions:
            continue
        placed.update(positions)
        members: list[int] = []
        for position in positions:
            members.extend(buckets[position])
        # A key the prefilter already matched wins over the model's
        # answer: it came from identical text, which is stronger evidence
        # than a judgement call. Otherwise take the model's, but only if
        # it names a ticket that actually exists.
        key = next((matched[p] for p in positions if p in matched), None)
        if key is None and group.existing_key in valid_keys:
            key = group.existing_key
        if key is not None:
            merged_matches[len(merged)] = key
        merged.append(members)

    # A bucket the model forgot to mention keeps its own group rather
    # than vanishing — dropping a finding here would lose a bug report.
    for position, bucket in enumerate(buckets):
        if position not in placed:
            if position in matched:
                merged_matches[len(merged)] = matched[position]
            merged.append(bucket)
    return merged, merged_matches


def group_findings(
    candidates: list[FindingCandidate],
    already_filed: list[FiledFinding],
) -> list[FindingGroup]:
    """Group *candidates* into one ticket's worth each; never raises."""
    if not candidates:
        return []  # nothing to group, and nothing worth an LLM call

    buckets, matched = _prefilter(candidates, already_filed)

    # One bucket with nothing already filed has no grouping question left
    # to answer, and the call would cost a round trip to be told so.
    if len(buckets) > 1 or already_filed:
        try:
            buckets, matched = _merge_with_llm(candidates, already_filed, buckets, matched)
        except llm.LLMError as exc:
            logger.warning("Finding grouping fell back to exact matching: %s", exc)
        except Exception:  # never raise into a run that already finished
            logger.exception("Finding grouping failed unexpectedly; using exact matching")

    groups: list[FindingGroup] = []
    for position, members in enumerate(buckets):
        representative = _elect(candidates, members)
        groups.append(
            FindingGroup(
                representative=representative,
                duplicates=[i for i in members if i != representative],
                existing_key=matched.get(position),
            )
        )
    return groups
