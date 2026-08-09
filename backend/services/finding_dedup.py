"""Group findings that describe one defect.

The mechanics only, shared by both callers that need the judgement:
``finding_grouping`` asks it which of a run's findings are occurrences of
the sprint's already-known defects, and ``finding_export`` asks it whose
report becomes which ticket.  Neither identity means anything here — a
match target is an opaque key with three fields of text.

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
from backend.services.llm_prompts import FindingCandidate, KnownDefect

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
    # The key of a known defect this group turned out to be another
    # occurrence of, or None when it is new. The key is whatever the
    # caller passed in — a `DefectGroup` id when the assignment pass is
    # asking, a ticket key when a filing run is — and this module never
    # inspects it.
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


def dedup_key(title: str, expected: str, actual: str) -> str:
    """The text identity of a defect, for callers with no ticket to group by.

    ``_prefilter`` below groups by exactly this, so a caller that has to
    answer "are these the same defect?" outside a filing run — the QA
    metrics aggregator, which collapses findings for a sprint whose
    tracker was never connected — gets the same answer this module would
    give, rather than a second opinion.

    Sharing the *name* would not be enough: the two paths have to agree on
    **which fields** are normalized.  A caller that keyed on the title
    alone would report a different grouping of the same findings, with
    nothing to say which was right — a metrics panel reading five bugs
    beside a tracker holding seven.  Hence the fixed triple here and the
    variadic ``_normalize`` beneath it, which stays private as the general
    mechanic.
    """
    return _normalize(title, expected, actual)


def elect_representative(candidates: list[FindingCandidate], indices: list[int]) -> int:
    """The member whose report speaks for the group.

    Chosen **in code**, never by the model: this row's text becomes the
    ticket's title and body, and the frozen text of a new ``DefectGroup``,
    so letting the model pick would let a grouping call quietly decide how
    a defect is described.  Highest severity first, then earliest position
    — the most alarming report of a defect is the one worth reading first,
    and ties resolve to the one observed soonest.
    """
    return min(
        indices,
        key=lambda i: (_SEVERITY_RANK.get(candidates[i].severity, len(_SEVERITY_RANK)), i),
    )


def _prefilter(
    candidates: list[FindingCandidate],
    known: list[KnownDefect],
) -> tuple[list[list[int]], dict[int, str]]:
    """Collapse exact-equal normalized findings and match them to known defects.

    Returns the buckets (lists of candidate indices, in first-seen order)
    and, per bucket index, the key of a known defect whose own normalized
    text matches.  ``known`` arrives newest-first, so the first match
    found is the most recently recorded description of that defect.
    """
    known_by_key: dict[str, str] = {}
    for defect in known:
        key = dedup_key(defect.title, defect.expected, defect.actual)
        # setdefault keeps the *first* — newest — key for a repeated defect.
        known_by_key.setdefault(key, defect.key)

    buckets: list[list[int]] = []
    seen: dict[str, int] = {}
    matched: dict[int, str] = {}
    for index, candidate in enumerate(candidates):
        key = dedup_key(candidate.title, candidate.expected, candidate.actual)
        if key in seen:
            buckets[seen[key]].append(index)
            continue
        seen[key] = len(buckets)
        buckets.append([index])
        if key in known_by_key:
            matched[len(buckets) - 1] = known_by_key[key]
    return buckets, matched


def _merge_with_llm(
    candidates: list[FindingCandidate],
    known: list[KnownDefect],
    buckets: list[list[int]],
    matched: dict[int, str],
) -> tuple[list[list[int]], dict[int, str]]:
    """Ask the model to merge buckets the prefilter left apart.

    One representative per bucket is shown, not every member: the members
    are already known to be textually identical, so the extra copies add
    prompt size and nothing else.
    """
    leaders = [bucket[0] for bucket in buckets]
    result = llm.group_findings([candidates[i] for i in leaders], known)

    merged: list[list[int]] = []
    merged_matches: dict[int, str] = {}
    placed: set[int] = set()
    valid_keys = {defect.key for defect in known}

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
        # it names a known defect that actually exists.
        prefiltered = list(dict.fromkeys(matched[p] for p in positions if p in matched))
        key = prefiltered[0] if prefiltered else None
        if len(prefiltered) > 1:
            # The merged buckets had matched *different* known defects, so
            # the sprint already holds two records of what is really one.
            # One has to win — a group is one defect by definition — and
            # the losers are left exactly as they are: no group is merged
            # or rewritten, and no ticket is reopened, closed, or
            # commented on. Logged because that duplicate pair is
            # otherwise invisible; it is the older record that needs
            # tidying, not this run.
            logger.info(
                "Merged findings matched several known defects (%s); adopting %s",
                ", ".join(prefiltered),
                key,
            )
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
    known: list[KnownDefect],
) -> list[FindingGroup]:
    """Group *candidates* into one defect's worth each; never raises."""
    if not candidates:
        return []  # nothing to group, and nothing worth an LLM call

    buckets, matched = _prefilter(candidates, known)

    # One bucket with nothing already known has no grouping question left
    # to answer, and the call would cost a round trip to be told so.
    if len(buckets) > 1 or known:
        try:
            buckets, matched = _merge_with_llm(candidates, known, buckets, matched)
        except llm.LLMError as exc:
            logger.warning("Finding grouping fell back to exact matching: %s", exc)
        except Exception:  # never raise into a run that already finished
            logger.exception("Finding grouping failed unexpectedly; using exact matching")

    groups: list[FindingGroup] = []
    for position, members in enumerate(buckets):
        representative = elect_representative(candidates, members)
        groups.append(
            FindingGroup(
                representative=representative,
                duplicates=[i for i in members if i != representative],
                existing_key=matched.get(position),
            )
        )
    return groups
