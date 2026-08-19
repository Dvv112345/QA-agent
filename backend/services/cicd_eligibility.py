"""Which of a sprint's test cases can be exported to CI, and why not.

One walk over ``sprint.requirements → test_plan → cases``, with one home
for the answer — the same treatment ``services/findings.py`` gives "what
counts as a reportable bug".  Every count and every checkbox on the export
page reads through here.

Three per-case outcomes, and the middle one absorbs a fourth:

``eligible``
    A cached script exists and still describes the current sprint.

``no_script``
    The case has never run to a verdict.  Remedy: run it.

``stale``
    The script exists but the requirement, plan or environment has moved
    since it was written — **or** its revisions are NULL, meaning it was
    cached before the stamp existed and its staleness is unknowable.
    Unknown and stale collapse into one state deliberately: they share a
    remedy (re-run the case), and a third state would have to be carried
    through the response model, the page and every test to say nothing new.

Staleness is decided by ``models.database.outdated_reasons`` — the *same*
comparison the run badges use — so "out of date" cannot come to mean two
different things depending on which screen is asking.
"""

import logging
from collections.abc import Sequence

from sqlalchemy.orm import selectinload
from sqlmodel import Session, select

from backend.models.database import (
    CicdExport,
    CicdExportItem,
    CicdExportStatus,
    Requirement,
    Sprint,
    TestCase,
    TestPlan,
    outdated_reasons,
)
from backend.models.types import CicdCaseEntry

logger = logging.getLogger(__name__)

# A script cached before the revision stamp existed. Reported as a stale
# reason rather than a separate state — see the module docstring.
UNKNOWN_REVISION = "unknown"


def _export_history(session: Session, sprint_id: int) -> dict[int, str]:
    """``{test_case_id: pr_url}`` from this sprint's completed exports.

    Newest export wins, so a case exported twice links to the most recent
    pull request rather than the first one.  Only ``COMPLETED`` exports
    count: a failed one wrote nothing to the repository, and offering its
    PR link would point at a branch that does not exist.
    """
    rows = session.exec(
        select(CicdExportItem.test_case_id, CicdExport.pr_url)
        .join(CicdExport, CicdExportItem.cicd_export_id == CicdExport.id)
        .where(
            CicdExport.sprint_id == sprint_id,
            CicdExport.status == CicdExportStatus.COMPLETED,
        )
        .order_by(CicdExport.updated_at.asc())
    ).all()
    return dict(rows)


def _stale_reasons(case: TestCase, requirement: Requirement, sprint: Sprint) -> list[str]:
    """Why this case's cached script no longer describes the sprint."""
    if (
        case.script_requirement_revision is None
        or case.script_plan_revision is None
        or case.script_env_revision is None
    ):
        return [UNKNOWN_REVISION]
    return outdated_reasons(
        requirement,
        sprint.test_environment,
        requirement_revision=case.script_requirement_revision,
        plan_revision=case.script_plan_revision,
        env_revision=case.script_env_revision,
    )


def case_entries(session: Session, sprint: Sprint) -> list[CicdCaseEntry]:
    """Every live test case in the sprint, with its export eligibility.

    Ineligible cases are **returned**, not filtered: the page renders them
    disabled with their reason, because a row that simply vanishes is
    indistinguishable from a bug.

    Archived cases never appear at all — ``TestPlan.cases`` filters them —
    so a plan revision drops its old cases from the eligible set with no
    code here.
    """
    history = _export_history(session, sprint.id)
    entries: list[CicdCaseEntry] = []

    for requirement in sprint.requirements:
        plan = requirement.test_plan
        if plan is None:
            continue
        for case in plan.cases:
            entry = CicdCaseEntry(
                test_case_id=case.id,
                case_title=case.title,
                requirement_id=requirement.id,
                requirement_name=requirement.name,
                eligible=True,
                previously_exported=case.id in history,
                last_export_pr_url=history.get(case.id),
            )
            if not case.script:
                entry.eligible = False
                entry.reason = "no_script"
            else:
                reasons = _stale_reasons(case, requirement, sprint)
                if reasons:
                    entry.eligible = False
                    entry.reason = "stale"
                    entry.stale_reasons = reasons
            entries.append(entry)

    return entries


def eligible_ids(entries: Sequence[CicdCaseEntry]) -> set[int]:
    """The subset a selection may legitimately name."""
    return {entry.test_case_id for entry in entries if entry.eligible}


def load_sprint_for_eligibility(session: Session, sprint_id: int) -> Sprint | None:
    """A sprint with both relationship chains eager-loaded.

    Same shape ``qa_metrics`` uses: the walk touches every requirement's
    plan and every plan's cases, so without this it is one query per
    requirement and one per plan.
    """
    return session.exec(
        select(Sprint)
        .where(Sprint.id == sprint_id)
        .options(
            selectinload(Sprint.all_requirements)
            .selectinload(Requirement.test_plan)
            .selectinload(TestPlan.all_cases),
            selectinload(Sprint.test_environment),
        )
    ).first()
