"""Tests for backend/services/finding_dedup.py — the two grouping stages.

``llm.group_findings`` is stubbed throughout; the module is a pure
function over its inputs and never touches the database.
"""

import pytest

from backend.services import finding_dedup, llm
from backend.services.llm import FindingGroupingResult, FindingGroupItem
from backend.services.llm_prompts import FiledFinding, FindingCandidate


def _candidate(title: str, *, severity: str = "medium", actual: str = "500", **overrides):
    defaults = {
        "severity": severity,
        "title": title,
        "steps_to_reproduce": "Open /checkout\nSubmit",
        "expected": "The order is created",
        "actual": actual,
    }
    defaults.update(overrides)
    return FindingCandidate(**defaults)


def _filed(issue_key: str, title: str, *, actual: str = "500"):
    return FiledFinding(
        issue_key=issue_key,
        title=title,
        expected="The order is created",
        actual=actual,
    )


class _GroupStub:
    """Answers with fixed groups and records whether it was called."""

    def __init__(self, groups=None, error: Exception | None = None):
        self.groups = groups or []
        self.error = error
        self.calls: list = []

    def __call__(self, candidates, already_filed):
        self.calls.append((candidates, already_filed))
        if self.error is not None:
            raise self.error
        return FindingGroupingResult(groups=[FindingGroupItem(**group) for group in self.groups])


@pytest.fixture
def group_stub(monkeypatch):
    stub = _GroupStub()
    monkeypatch.setattr(llm, "group_findings", stub)
    return stub


# ── Stage 1: the deterministic prefilter ──────────────────────────────


def test_no_candidates_makes_no_llm_call(group_stub):
    assert finding_dedup.group_findings([], []) == []
    assert group_stub.calls == []


def test_identical_findings_collapse_without_the_llm(group_stub):
    """The common case — one broken dependency fails every case in a plan
    with the same words — must not cost a round trip."""
    candidates = [_candidate("Checkout returns 500") for _ in range(8)]

    groups = finding_dedup.group_findings(candidates, [])

    assert len(groups) == 1
    assert sorted(groups[0].members) == list(range(8))
    assert group_stub.calls == []


def test_normalization_ignores_ids_and_punctuation(group_stub):
    """Two reports of one defect routinely differ only by a generated id."""
    candidates = [
        _candidate("Order 8814 was not created!", actual="HTTP 500"),
        _candidate("Order 9021 was not created", actual="HTTP 500"),
    ]

    groups = finding_dedup.group_findings(candidates, [])

    assert len(groups) == 1
    assert group_stub.calls == []


def test_different_findings_are_not_collapsed_by_the_prefilter(group_stub):
    candidates = [_candidate("Checkout returns 500"), _candidate("Tax is omitted")]

    finding_dedup.group_findings(candidates, [])

    # Two survivors, so the LLM stage is asked about them.
    assert len(group_stub.calls) == 1
    assert len(group_stub.calls[0][0]) == 2


def test_a_single_bucket_with_nothing_filed_skips_the_llm(group_stub):
    finding_dedup.group_findings([_candidate("Checkout returns 500")], [])
    assert group_stub.calls == []


def test_a_single_bucket_still_asks_when_tickets_exist(group_stub):
    """There is a real question left — whether this defect already has a
    ticket — even with nothing to merge."""
    finding_dedup.group_findings(
        [_candidate("Checkout returns 500")], [_filed("QA-1", "Orders fail")]
    )
    assert len(group_stub.calls) == 1


# ── Stage 2: the LLM pass ─────────────────────────────────────────────


def test_llm_merges_paraphrases_the_prefilter_misses(monkeypatch):
    """Two reports of one defect can share no tokens at all."""
    monkeypatch.setattr(llm, "group_findings", _GroupStub(groups=[{"indices": [0, 1]}]))
    candidates = [
        _candidate("Checkout returns 500"),
        _candidate("The order endpoint errors on submit"),
    ]

    groups = finding_dedup.group_findings(candidates, [])

    assert len(groups) == 1
    assert sorted(groups[0].members) == [0, 1]


def test_llm_answers_in_bucket_positions_not_candidate_indices(monkeypatch):
    """The model is shown one leader per bucket, so its indices must be
    mapped back through the buckets or the wrong findings get merged."""
    monkeypatch.setattr(llm, "group_findings", _GroupStub(groups=[{"indices": [0, 1]}]))
    candidates = [
        _candidate("Checkout returns 500"),
        _candidate("Checkout returns 500"),  # collapses into bucket 0
        _candidate("The order endpoint errors on submit"),  # bucket 1
    ]

    groups = finding_dedup.group_findings(candidates, [])

    assert len(groups) == 1
    assert sorted(groups[0].members) == [0, 1, 2]


def test_llm_error_falls_back_to_stage_one_groups(monkeypatch):
    """A worse grouping, not no grouping — and never a raised exception
    into a run that already spent its whole budget."""
    monkeypatch.setattr(llm, "group_findings", _GroupStub(error=llm.LLMError("down")))
    candidates = [
        _candidate("Checkout returns 500"),
        _candidate("Checkout returns 500"),
        _candidate("Tax is omitted"),
    ]

    groups = finding_dedup.group_findings(candidates, [])

    assert len(groups) == 2
    assert sorted(sorted(g.members) for g in groups) == [[0, 1], [2]]


def test_unexpected_exception_is_also_swallowed(monkeypatch):
    monkeypatch.setattr(llm, "group_findings", _GroupStub(error=TypeError("boom")))

    groups = finding_dedup.group_findings(
        [_candidate("Checkout returns 500"), _candidate("Tax is omitted")], []
    )

    assert len(groups) == 2


def test_a_bucket_the_model_forgot_keeps_its_own_group(monkeypatch):
    """Dropping it would silently lose a bug report."""
    monkeypatch.setattr(llm, "group_findings", _GroupStub(groups=[{"indices": [0]}]))
    candidates = [_candidate("Checkout returns 500"), _candidate("Tax is omitted")]

    groups = finding_dedup.group_findings(candidates, [])

    assert sorted(sorted(g.members) for g in groups) == [[0], [1]]


def test_out_of_range_and_repeated_indices_are_ignored(monkeypatch):
    monkeypatch.setattr(
        llm,
        "group_findings",
        _GroupStub(groups=[{"indices": [0, 0, 7, -1]}, {"indices": [1]}]),
    )
    candidates = [_candidate("Checkout returns 500"), _candidate("Tax is omitted")]

    groups = finding_dedup.group_findings(candidates, [])

    assert sorted(sorted(g.members) for g in groups) == [[0], [1]]


def test_a_bucket_claimed_twice_is_placed_once(monkeypatch):
    """Every finding belongs to exactly one ticket — a bucket in two
    groups would file the same finding twice."""
    monkeypatch.setattr(
        llm, "group_findings", _GroupStub(groups=[{"indices": [0, 1]}, {"indices": [1]}])
    )
    candidates = [_candidate("Checkout returns 500"), _candidate("Tax is omitted")]

    groups = finding_dedup.group_findings(candidates, [])

    placed = sorted(i for group in groups for i in group.members)
    assert placed == [0, 1]


# ── Representative election ───────────────────────────────────────────


def test_representative_is_the_highest_severity_member(monkeypatch):
    """The ticket's title and body come from this row, so the choice is
    made in code rather than by the model."""
    monkeypatch.setattr(llm, "group_findings", _GroupStub(groups=[{"indices": [0, 1, 2]}]))
    candidates = [
        _candidate("Low report", severity="low"),
        _candidate("High report", severity="high"),
        _candidate("Medium report", severity="medium"),
    ]

    groups = finding_dedup.group_findings(candidates, [])

    assert groups[0].representative == 1
    assert sorted(groups[0].duplicates) == [0, 2]


def test_ties_break_on_position(monkeypatch):
    monkeypatch.setattr(llm, "group_findings", _GroupStub(groups=[{"indices": [0, 1]}]))
    candidates = [_candidate("A", severity="high"), _candidate("B", severity="high")]

    groups = finding_dedup.group_findings(candidates, [])

    assert groups[0].representative == 0


def test_unknown_severity_ranks_last(monkeypatch):
    monkeypatch.setattr(llm, "group_findings", _GroupStub(groups=[{"indices": [0, 1]}]))
    candidates = [_candidate("A", severity="catastrophic"), _candidate("B", severity="low")]

    groups = finding_dedup.group_findings(candidates, [])

    assert groups[0].representative == 1


# ── Matching an already-filed ticket ──────────────────────────────────


def test_exact_match_against_a_filed_ticket_yields_its_key(group_stub):
    candidates = [_candidate("Checkout returns 500")]
    filed = [_filed("QA-142", "Checkout returns 500")]

    groups = finding_dedup.group_findings(candidates, filed)

    assert groups[0].existing_key == "QA-142"


def test_several_matches_take_the_most_recent(group_stub):
    """`already_filed` arrives newest-first, so the first match wins."""
    candidates = [_candidate("Checkout returns 500")]
    filed = [
        _filed("QA-300", "Checkout returns 500"),
        _filed("QA-142", "Checkout returns 500"),
    ]

    groups = finding_dedup.group_findings(candidates, filed)

    assert groups[0].existing_key == "QA-300"


def test_llm_may_match_a_ticket_the_prefilter_missed(monkeypatch):
    monkeypatch.setattr(
        llm,
        "group_findings",
        _GroupStub(groups=[{"indices": [0], "existing_key": "QA-142"}]),
    )
    candidates = [_candidate("The order endpoint errors on submit")]

    groups = finding_dedup.group_findings(candidates, [_filed("QA-142", "Checkout 500")])

    assert groups[0].existing_key == "QA-142"


def test_a_key_the_model_invented_is_ignored(monkeypatch):
    """Adopting a key that names no ticket would attach a finding to
    nothing and lose it."""
    monkeypatch.setattr(
        llm,
        "group_findings",
        _GroupStub(groups=[{"indices": [0], "existing_key": "QA-999"}]),
    )

    groups = finding_dedup.group_findings(
        [_candidate("Checkout returns 500")], [_filed("QA-142", "Something else")]
    )

    assert groups[0].existing_key is None


def test_an_exact_match_outranks_the_models_answer(monkeypatch):
    """Identical text is stronger evidence than a judgement call."""
    monkeypatch.setattr(
        llm,
        "group_findings",
        _GroupStub(groups=[{"indices": [0], "existing_key": "QA-9"}]),
    )
    filed = [_filed("QA-9", "Unrelated"), _filed("QA-142", "Checkout returns 500")]

    groups = finding_dedup.group_findings([_candidate("Checkout returns 500")], filed)

    assert groups[0].existing_key == "QA-142"


def test_every_candidate_lands_in_exactly_one_group(group_stub):
    """The invariant the caller relies on: no finding is filed twice, and
    none is silently dropped."""
    candidates = [_candidate(f"Defect {n}") for n in range(5)]

    groups = finding_dedup.group_findings(candidates, [])

    placed = sorted(i for group in groups for i in group.members)
    assert placed == list(range(5))
