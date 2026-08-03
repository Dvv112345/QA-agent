"""Definition of database models"""

import json
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from sqlmodel import Field, Relationship, SQLModel

from backend.config import (
    MAX_CLARIFICATION_ROUNDS,
    MAX_TEST_ENV_REVISION_ROUNDS,
    MAX_TEST_PLAN_FEEDBACK_ROUNDS,
)


class Repo(SQLModel, table=True):
    """A GitHub repository registered for QA analysis."""

    id: int | None = Field(default=None, primary_key=True)
    github_link: str
    github_token: str | None = Field(default=None)
    name: str
    description: str | None = Field(default=None)
    active: bool = Field(default=True)
    file_tree: str | None = Field(default=None)  # filtered path listing for LLM prompt context
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )

    sprints: list["Sprint"] = Relationship(back_populates="repo")


class Sprint(SQLModel, table=True):
    """A named sprint linked to a GitHub repository."""

    id: int | None = Field(default=None, primary_key=True)
    name: str
    repo_id: int = Field(foreign_key="repo.id")
    active: bool = Field(default=True)
    directory: str = Field(unique=True)
    readme_user_provided: bool = Field(default=False)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )

    repo: Repo | None = Relationship(back_populates="sprints")
    # Every requirement row, archived included. Deliberately *not* named
    # `requirements` — see the property below.
    all_requirements: list["Requirement"] = Relationship(back_populates="sprint")
    test_environment: Optional["TestEnvironmentAccess"] = Relationship(
        back_populates="sprint", sa_relationship_kwargs={"uselist": False}
    )
    test_runs: list["TestRun"] = Relationship(back_populates="sprint")
    exploratory_runs: list["ExploratoryRun"] = Relationship(back_populates="sprint")

    @property
    def requirements(self) -> list["Requirement"]:
        """The sprint's live requirements — archived rows excluded.

        Deleting a requirement archives it rather than removing the row, so
        that test runs which already executed against it keep working.  The
        filter lives here, under the name every caller already uses, rather
        than at ~12 call sites that would each have to remember it: a missed
        one would silently resurrect a deleted requirement in a list, a
        completion check, or a run selector.

        Reach for ``all_requirements`` only when archived rows are genuinely
        wanted — which is nowhere outside the archive machinery itself.
        """
        return [r for r in self.all_requirements if not r.archived]

    @property
    def requirements_complete(self) -> bool:
        """Whether the sprint has at least one requirement and all are confirmed."""
        return len(self.requirements) > 0 and all(
            r.status == RequirementStatus.CONFIRMED for r in self.requirements
        )

    @property
    def has_test_environment_submission(self) -> bool:
        """Whether a test environment access description has been submitted."""
        return self.test_environment is not None

    @property
    def requirements_locked(self) -> bool:
        """Whether the requirement set is frozen (test environment confirmed)."""
        return (
            self.test_environment is not None
            and self.test_environment.status == TestEnvironmentStatus.CONFIRMED
        )

    @property
    def has_test_plans(self) -> bool:
        """Whether at least one requirement has a test plan row."""
        return any(r.test_plan is not None for r in self.requirements)

    @property
    def test_plans_complete(self) -> bool:
        """Whether every requirement has an approved test plan (and one exists)."""
        return len(self.requirements) > 0 and all(
            r.test_plan is not None and r.test_plan.status == TestPlanStatus.APPROVED
            for r in self.requirements
        )

    @property
    def has_test_runs(self) -> bool:
        """Whether at least one test run has been submitted for this sprint."""
        return any(self.test_runs)

    @property
    def has_exploratory_runs(self) -> bool:
        """Whether at least one exploratory run has been started for this sprint."""
        return any(self.exploratory_runs)


class RequirementStatus(str, Enum):
    """Lifecycle status of a requirement's clarity analysis."""

    PENDING = "pending"
    ANALYZING = "analyzing"
    NEEDS_CLARIFICATION = "needs_clarification"
    READY = "ready"
    CONFIRMED = "confirmed"
    FAILED = "failed"


# Error stored on rows failed because their sprint was finished mid-analysis
# (shared by the finish-sprint sweep and the worker task's guard).
SPRINT_FINISHED_ERROR = "Sprint was finished before analysis completed."


class Requirement(SQLModel, table=True):
    """A single requirement attached to a sprint, analyzed for QA clarity."""

    id: int | None = Field(default=None, primary_key=True)
    sprint_id: int = Field(foreign_key="sprint.id", index=True)
    name: str
    description: str  # current text (possibly LLM-rewritten)
    original_description: str
    from_prd: bool = Field(default=False)  # PRD-split rows; replaced on re-upload
    # Soft delete. A requirement with test runs or exploratory runs behind it
    # cannot be removed without destroying their history, so "delete" archives
    # instead and every live view filters it out (see Sprint.requirements).
    # Requirements with no such history are still deleted outright.
    archived: bool = Field(default=False)
    # Bumped only when the *substance* changes (the description text), never
    # on a status transition. A test run copies this at creation; the two
    # differing later is what makes the run outdated. Deliberately not
    # `updated_at`, which confirm/restart/confirm-all all stamp.
    content_revision: int = Field(default=0)
    status: str = Field(default=RequirementStatus.PENDING)
    clarifying_question: str | None = Field(default=None)
    pending_answer: str | None = Field(default=None)  # user answer awaiting the revise job
    revision_count: int = Field(default=0)
    retry_count: int = Field(default=0)  # automatic re-enqueues (reconciler/task)
    job_id: str | None = Field(default=None)  # RQ job id — reconciler dedup guard
    last_heartbeat: datetime | None = Field(default=None)  # worker liveness while analyzing
    error: str | None = Field(default=None)  # user-facing summary when failed
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )

    sprint: Sprint | None = Relationship(back_populates="all_requirements")
    test_plan: Optional["TestPlan"] = Relationship(
        back_populates="requirement", sa_relationship_kwargs={"uselist": False}
    )
    test_executions: list["TestExecution"] = Relationship(back_populates="requirement")
    exploratory_runs: list["ExploratoryRun"] = Relationship(back_populates="requirement")

    @property
    def clarification_cap_reached(self) -> bool:
        """Whether the clarification Q&A rounds are exhausted (confirm/edit only)."""
        return self.revision_count >= MAX_CLARIFICATION_ROUNDS


class TestEnvironmentStatus(str, Enum):
    """Lifecycle status of a sprint's test environment access description."""

    __test__ = False  # tell pytest this "Test*" name is not a test class

    NEEDS_INFO = "needs_info"
    READY = "ready"
    CONFIRMED = "confirmed"


class TestEnvironmentAccess(SQLModel, table=True):
    """Free-text description of how to access a sprint's test environment.

    One row per sprint, judged synchronously by the LLM — no queue/worker
    involvement, so no heartbeat/retry columns.
    """

    __test__ = False  # tell pytest this "Test*" name is not a test class

    id: int | None = Field(default=None, primary_key=True)
    sprint_id: int = Field(foreign_key="sprint.id", unique=True)
    content: str  # current text (possibly LLM-rewritten)
    original_content: str
    status: str = Field(default=TestEnvironmentStatus.NEEDS_INFO)
    clarifying_question: str | None = Field(default=None)
    revision_count: int = Field(default=0)
    # JSON-serialized {"NAME": "value", ...} — extracted from `content` once
    # the sufficiency check judges it sufficient; cleared to None if a later
    # resubmission comes back insufficient (never left describing stale
    # content). The only artifact in this feature holding literal secrets.
    env_vars_json: str | None = Field(default=None)
    # See Requirement.content_revision. Bumped by a content resubmission and
    # by a direct edit of the extracted variables — the latter deliberately
    # *without* touching updated_at, which means "last LLM check" here.
    content_revision: int = Field(default=0)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )

    sprint: Sprint | None = Relationship(back_populates="test_environment")

    @property
    def clarification_cap_reached(self) -> bool:
        """Whether the answer/revise rounds are exhausted (direct edit only)."""
        return self.revision_count >= MAX_TEST_ENV_REVISION_ROUNDS

    @property
    def env_vars(self) -> dict[str, str] | None:
        """Decoded env_vars_json — the single accessor for the extracted variables."""
        return json.loads(self.env_vars_json) if self.env_vars_json else None

    @property
    def requirements_stale(self) -> bool:
        """Whether a requirement was confirmed after this row's last LLM check.

        ``updated_at`` doubles as the last-checked time: every content
        mutation (submit/edit/answer) coincides with a successful check.
        Deleting a confirmed requirement deliberately does not trip this —
        removal can only shrink the environments needed.
        """
        if self.sprint is None:
            return False
        return any(
            r.status == RequirementStatus.CONFIRMED and r.updated_at > self.updated_at
            for r in self.sprint.requirements
        )


class TestPlanStatus(str, Enum):
    """Lifecycle status of a requirement's generated test plan."""

    __test__ = False  # tell pytest this "Test*" name is not a test class

    PENDING = "pending"
    GENERATING = "generating"
    DRAFT = "draft"
    APPROVED = "approved"
    FAILED = "failed"


class TestPlan(SQLModel, table=True):
    """LLM-generated test plan for a single confirmed requirement.

    Generated asynchronously by an RQ worker (same machinery columns as
    ``Requirement``: status/retry_count/job_id/last_heartbeat/error).
    """

    __test__ = False  # tell pytest this "Test*" name is not a test class

    id: int | None = Field(default=None, primary_key=True)
    requirement_id: int = Field(foreign_key="requirement.id", unique=True)
    complexity: str | None = Field(default=None)  # low / medium / high
    summary: str | None = Field(default=None)
    status: str = Field(default=TestPlanStatus.PENDING)
    pending_feedback: str | None = Field(default=None)  # user feedback awaiting the revise job
    revision_count: int = Field(default=0)  # LLM feedback revisions only (direct edits don't count)
    # See Requirement.content_revision. Distinct from `revision_count`
    # above: that counts LLM feedback rounds against a cap, this tracks any
    # change to the case set — direct edits included.
    content_revision: int = Field(default=0)
    retry_count: int = Field(default=0)  # automatic re-enqueues (reconciler/task)
    job_id: str | None = Field(default=None)  # RQ job id — reconciler dedup guard
    last_heartbeat: datetime | None = Field(default=None)  # worker liveness while generating
    error: str | None = Field(default=None)  # user-facing summary when failed
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )

    requirement: Optional["Requirement"] = Relationship(back_populates="test_plan")
    # Every case row, archived included — see the `cases` property below.
    #
    # The delete-orphan cascade this relationship used to carry is gone on
    # purpose: revisions and edits now *archive* the outgoing cases instead
    # of deleting them, and under delete-orphan detaching a case would
    # delete the row — precisely the history loss archiving exists to
    # prevent. Nothing deletes a TestCase any more.
    all_cases: list["TestCase"] = Relationship(
        back_populates="test_plan",
        sa_relationship_kwargs={"order_by": "TestCase.position"},
    )

    @property
    def cases(self) -> list["TestCase"]:
        """The plan's live cases — archived rows excluded.

        Same arrangement as ``Sprint.requirements``: the filter sits under
        the name every caller already uses, so a revision cannot leak the
        superseded cases into a prompt, a plan card, or a new run.
        """
        return [c for c in self.all_cases if not c.archived]

    @property
    def feedback_cap_reached(self) -> bool:
        """Whether the feedback/revise rounds are exhausted (direct edit only)."""
        return self.revision_count >= MAX_TEST_PLAN_FEEDBACK_ROUNDS

    @property
    def requirement_name(self) -> str:
        """Name of the requirement this plan covers (serialized for plan cards)."""
        return self.requirement.name if self.requirement is not None else ""

    @property
    def requirement_description(self) -> str:
        """Description of the requirement this plan covers (serialized for plan cards)."""
        return self.requirement.description if self.requirement is not None else ""


class TestCasePriority(str, Enum):
    """Execution priority of a test case."""

    __test__ = False  # tell pytest this "Test*" name is not a test class

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class TestCase(SQLModel, table=True):
    """A single test case belonging to a test plan.

    Rows are superseded wholesale on every LLM revision or direct edit, but
    never deleted: a ``TestCaseExecution`` from an earlier run points here
    for its title, steps, and expected result, so removing the row would
    rewrite the history of something that already happened.  The outgoing
    rows are marked ``archived`` instead and drop out of ``TestPlan.cases``.
    """

    __test__ = False  # tell pytest this "Test*" name is not a test class

    id: int | None = Field(default=None, primary_key=True)
    # Nullable so an archived case can be detached when its plan row is
    # removed. TestPlan.requirement_id is unique, so the plan row cannot
    # simply be archived alongside — the next generation needs that slot.
    test_plan_id: int | None = Field(default=None, foreign_key="testplan.id", index=True)
    archived: bool = Field(default=False)
    position: int
    title: str
    preconditions: str | None = Field(default=None)
    steps: str  # newline-joined step list
    expected_result: str
    case_type: str
    priority: str  # TestCasePriority value
    # Cached Playwright script, reused across runs. Overwritten whenever a
    # run ends PASSED or FAILED (app bug) — never on ERROR (self-heal
    # exhausted, still looks broken; a future run should regenerate fresh).
    script: str | None = Field(default=None)

    test_plan: Optional["TestPlan"] = Relationship(back_populates="all_cases")


# ── Findings (shared by scripted and exploratory testing) ─────────────
#
# Defined here rather than in the exploratory section below because both
# testing modes now report findings in this vocabulary: an exploratory
# session records them through a tool, a scripted run derives them from a
# test case's outcome.


class FindingType(str, Enum):
    """SBTM distinguishes product defects from testing obstructions.

    Collapsing these would discard the signal that a session was *blocked*
    rather than clean.  The same split carries over to scripted runs, where
    it is derived from the case outcome: a ``failed`` case caught the
    product being wrong, an ``error`` case means the test itself never got
    off the ground.
    """

    BUG = "bug"  # the product is wrong
    ISSUE = "issue"  # something obstructed the testing itself

    @classmethod
    def normalize(cls, value: str | None) -> str:
        """Coerce a reported type to a usable one, defaulting to ``bug``.

        An unrecognised type is worse than a wrong one: a finding counted
        toward neither ``bug_count`` nor ``issue_count`` while still
        counting toward ``finding_count`` makes the run page show numbers
        that do not add up.

        ``bug`` matches what ``BrowserSession.record_finding`` already
        defaults to when the model omits the field — this closes the same
        gap for a value it got wrong.
        """
        return value if value in {member.value for member in cls} else cls.BUG.value


class FindingSeverity(str, Enum):
    """Reporter-assigned severity of a finding."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"

    @classmethod
    def normalize(cls, value: str | None) -> str:
        """Coerce a reported severity to a usable one, defaulting to medium.

        Both finding sources are LLM-reported and both feed the same card
        and the same ``high_severity_count``, so an unrecognised value has
        to be resolved the same way on each — otherwise that count means
        two different things depending on which mode found the bug.

        Medium is the neutral choice: it neither inflates the headline
        number nor buries something that might matter.

        Returns the plain string, never the enum member: on Python 3.12 an
        f-string of a member renders as ``FindingSeverity.MEDIUM``, and
        these values reach both prompt text and stored columns.
        """
        return value if value in {member.value for member in cls} else cls.MEDIUM.value


# ── Run staleness (shared by scripted and exploratory runs) ───────────


def outdated_reasons(
    requirement: Optional["Requirement"],
    test_env: Optional["TestEnvironmentAccess"],
    *,
    requirement_revision: int,
    plan_revision: int,
    env_revision: int,
) -> list[str]:
    """Which upstream artifacts have moved since a run executed.

    A run copies the three ``content_revision`` values it ran against; an
    inequality here means the run no longer describes the current sprint.
    Revisions rather than timestamps because ``updated_at`` is stamped by
    confirm, approve, restart, and confirm-all — none of which change what
    a run was testing.

    Shared by both run types so "outdated" cannot come to mean two
    different things depending on which mode produced the run.
    """
    reasons: list[str] = []

    if requirement is None or requirement.archived:
        # Deletion is change taken to its limit: the requirement this run
        # used is not the requirement that exists now.
        reasons.append("requirement")
        # The plan went with it, so reporting it too would be noise rather
        # than a second fact — and there is nothing left to compare against.
    else:
        if requirement.content_revision != requirement_revision:
            reasons.append("requirement")
        plan = requirement.test_plan
        if plan is None or plan.content_revision != plan_revision:
            reasons.append("test_plan")

    # Evaluated even for a deleted requirement: the sprint is still
    # reachable, and an environment change is genuinely independent.
    #
    # A *missing* environment row is deliberately not a reason, unlike a
    # missing plan above. The asymmetry is real: plans are removed by the
    # cascade precisely when upstream content changed, so their absence is
    # evidence. An absent environment row is evidence of nothing — it
    # cannot be deleted through any flow — so claiming "the test
    # environment changed" would be a guess dressed up as a finding.
    if test_env is not None and test_env.content_revision != env_revision:
        reasons.append("test_environment")

    return reasons


_REASON_LABELS = {
    "requirement": "the requirement changed",
    "test_plan": "the test plan changed",
    "test_environment": "the test environment changed",
}


def outdated_restart_error(reasons: list[str], requirement_deleted: bool) -> str:
    """Why a restart was refused, naming what moved.

    Lives here beside ``outdated_reasons`` so the two share one vocabulary,
    following ``SPRINT_FINISHED_ERROR``'s precedent of keeping a user-facing
    string next to the state it describes.  Both run types raise it, so the
    wording cannot drift between them.
    """
    labels = [
        "the requirement was deleted"
        if reason == "requirement" and requirement_deleted
        else _REASON_LABELS.get(reason, reason)
        for reason in reasons
    ]
    joined = ", ".join(labels)
    return (
        f"This run is out of date ({joined}) and can no longer be restarted — "
        "start a new run to test the current state."
    )


class TestExecutionStatus(str, Enum):
    """Lifecycle status of one requirement's test-case run within a TestRun."""

    __test__ = False  # tell pytest this "Test*" name is not a test class

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class TestCaseExecutionStatus(str, Enum):
    """Outcome of a single test case within a TestExecution."""

    __test__ = False  # tell pytest this "Test*" name is not a test class

    PENDING = "pending"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"


class TestRun(SQLModel, table=True):
    """One submission ("Run new test") covering one or more requirements.

    Carries no worker-job machinery of its own — status and requirement
    names are derived from its executions on every read, the same spirit
    as Sprint's computed flags (Convention #10), applied one level down.
    """

    __test__ = False  # tell pytest this "Test*" name is not a test class

    id: int | None = Field(default=None, primary_key=True)
    sprint_id: int = Field(foreign_key="sprint.id", index=True)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )

    sprint: Sprint | None = Relationship(back_populates="test_runs")
    executions: list["TestExecution"] = Relationship(
        back_populates="test_run",
        sa_relationship_kwargs={"cascade": "all, delete-orphan", "order_by": "TestExecution.id"},
    )

    @property
    def status(self) -> str:
        """Rolled-up status: running while any execution is in progress, else
        failed if any execution failed, else completed."""
        if any(
            e.status in (TestExecutionStatus.PENDING, TestExecutionStatus.RUNNING)
            for e in self.executions
        ):
            return TestExecutionStatus.RUNNING
        if any(e.status == TestExecutionStatus.FAILED for e in self.executions):
            return TestExecutionStatus.FAILED
        return TestExecutionStatus.COMPLETED

    @property
    def requirement_names(self) -> list[str]:
        """Names of the requirements covered by this run, in execution order."""
        return [e.requirement_name for e in self.executions]

    @property
    def outdated_reasons(self) -> list[str]:
        """Union of its executions' reasons, de-duplicated, order preserved."""
        seen: list[str] = []
        for execution in self.executions:
            for reason in execution.outdated_reasons:
                if reason not in seen:
                    seen.append(reason)
        return seen

    @property
    def outdated(self) -> bool:
        return bool(self.outdated_reasons)

    @property
    def requirement_deleted(self) -> bool:
        """Whether *any* of this run's requirements has since been deleted."""
        return any(e.requirement_deleted for e in self.executions)


class TestExecution(SQLModel, table=True):
    """The row an RQ job operates on: one requirement's cases within a run.

    Same machinery columns as ``Requirement``/``TestPlan``
    (status/retry_count/job_id/last_heartbeat/error) — no
    ``pending_feedback``-equivalent field, since resumability is derived
    entirely from its ``TestCaseExecution`` rows' own statuses.
    """

    __test__ = False  # tell pytest this "Test*" name is not a test class

    id: int | None = Field(default=None, primary_key=True)
    test_run_id: int = Field(foreign_key="testrun.id", index=True)
    requirement_id: int = Field(foreign_key="requirement.id", index=True)
    # The content this run actually executed against, copied at creation.
    # Compared with the live values to tell whether the run still describes
    # the current state of the sprint (see `outdated_reasons`).
    requirement_revision: int = Field(default=0)
    plan_revision: int = Field(default=0)
    env_revision: int = Field(default=0)
    status: str = Field(default=TestExecutionStatus.PENDING)
    retry_count: int = Field(default=0)  # automatic re-enqueues (reconciler/task)
    job_id: str | None = Field(default=None)  # RQ job id — reconciler dedup guard
    last_heartbeat: datetime | None = Field(default=None)  # worker liveness while running
    error: str | None = Field(default=None)  # user-facing summary when failed
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )

    test_run: Optional["TestRun"] = Relationship(back_populates="executions")
    requirement: Optional["Requirement"] = Relationship(back_populates="test_executions")
    cases: list["TestCaseExecution"] = Relationship(
        back_populates="test_execution",
        sa_relationship_kwargs={
            "cascade": "all, delete-orphan",
            "order_by": "TestCaseExecution.id",
        },
    )

    @property
    def requirement_name(self) -> str:
        """Name of the requirement this execution covers (serialized for cards)."""
        return self.requirement.name if self.requirement is not None else ""

    @property
    def requirement_deleted(self) -> bool:
        """Whether the requirement this run covered has since been deleted.

        A label refinement for the badge, not a parallel state: it selects
        the wording for the ``requirement`` reason. Nothing branches on it
        for correctness — use ``outdated``.
        """
        return self.requirement is None or self.requirement.archived

    @property
    def outdated_reasons(self) -> list[str]:
        """Upstream artifacts that changed since this execution ran."""
        sprint = self.test_run.sprint if self.test_run is not None else None
        return outdated_reasons(
            self.requirement,
            sprint.test_environment if sprint is not None else None,
            requirement_revision=self.requirement_revision,
            plan_revision=self.plan_revision,
            env_revision=self.env_revision,
        )

    @property
    def outdated(self) -> bool:
        """Exactly ``bool(outdated_reasons)`` — one flag, one meaning.

        Backend-only, deliberately not serialized: shipping it alongside
        ``outdated_reasons`` would put two fields in the payload that must
        agree when one is derivable from the other. Its consumer is the
        restart guard.
        """
        return bool(self.outdated_reasons)


class TestCaseExecution(SQLModel, table=True):
    """Result of running one test case within a TestExecution.

    Stores only the final attempt (script/output/attempts) — intermediate
    failed self-heal attempts aren't user-facing.

    A terminal failure also carries a structured finding, in the same
    vocabulary an exploratory session uses.  The fields live directly on
    this row rather than in a child table because a case stops at its first
    verdict: it produces at most one finding, so a 0..1 relationship would
    buy a foreign key and cascade for nothing.
    """

    __test__ = False  # tell pytest this "Test*" name is not a test class

    id: int | None = Field(default=None, primary_key=True)
    test_execution_id: int = Field(foreign_key="testexecution.id", index=True)
    test_case_id: int = Field(foreign_key="testcase.id", index=True)
    status: str = Field(default=TestCaseExecutionStatus.PENDING)
    attempts: int = Field(default=0)
    output: str | None = Field(default=None)
    error: str | None = Field(default=None)
    script_snapshot: str | None = Field(default=None)  # credential-free, downloadable as-is
    # ── structured finding (set on a terminal failure, cleared on pass) ──
    # Prefixed because `title`/`expected` alone would read as the test
    # case's own; the `finding` property below maps them back onto the
    # shared names the API exposes.
    finding_severity: str | None = Field(default=None)  # FindingSeverity value
    finding_title: str | None = Field(default=None)
    finding_steps_to_reproduce: str | None = Field(default=None)  # newline-joined
    finding_expected: str | None = Field(default=None)
    finding_actual: str | None = Field(default=None)
    # Where the script ran (worker host). Part of the finding above, so it
    # is None on every case that has no finding — a pass, a row still in
    # flight, or a row written before findings were structured.
    environment: str | None = Field(default=None)
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )

    test_execution: Optional["TestExecution"] = Relationship(back_populates="cases")
    test_case: Optional["TestCase"] = Relationship()

    @property
    def finding_type(self) -> str | None:
        """Derived from the outcome — deliberately never stored.

        ``failed`` means the script ran correctly and the product was
        wrong; ``error`` means self-heal gave up and the testing itself
        never landed.  Storing this alongside ``status`` would create two
        sources of truth that a restart could put out of step.

        Plain strings, not enum members, for the same reason
        ``FindingSeverity.normalize`` returns one.
        """
        if self.status == TestCaseExecutionStatus.FAILED:
            return FindingType.BUG.value
        if self.status == TestCaseExecutionStatus.ERROR:
            return FindingType.ISSUE.value
        return None

    @property
    def finding(self) -> dict[str, str | None] | None:
        """The finding in the shared shape, or None when there isn't one.

        Keyed to match ``FindingBase`` in ``models/types.py`` so FastAPI can
        validate it straight into the nested response model.

        Gated on ``finding_title`` rather than on status: rows written
        before this feature are ``failed`` with no finding, and must not
        surface as an empty card.

        The five required fields below the title are coalesced because
        ``FindingBase`` declares them non-optional over nullable columns: a
        single ``None`` would fail response validation and 500 the *whole*
        run detail response rather than just this card.  The task always writes them as
        a group, so this should be unreachable — but the blast radius is far
        out of proportion to the cost of a fallback.  ``environment`` is
        left alone: the response model already allows it to be null.
        """
        if not self.finding_title:
            return None
        return {
            "finding_type": self.finding_type or "",
            "severity": self.finding_severity or "",
            "title": self.finding_title,
            "steps_to_reproduce": self.finding_steps_to_reproduce or "",
            "expected": self.finding_expected or "",
            "actual": self.finding_actual or "",
            "environment": self.environment,
        }


# ── Exploratory testing ───────────────────────────────────────────────


class SfdipotArea(str, Enum):
    """Product elements from the SFDIPOT heuristic (Rapid Software Testing).

    A charter targets one or more of these; the generator skips dimensions
    that don't apply to the requirement rather than forcing all seven.
    """

    STRUCTURE = "Structure"
    FUNCTION = "Function"
    DATA = "Data"
    INTERFACES = "Interfaces"
    PLATFORM = "Platform"
    OPERATIONS = "Operations"
    TIME = "Time"


class ExploratoryRunStatus(str, Enum):
    """Lifecycle status of one requirement's exploratory run.

    Mirrors ``TestExecutionStatus`` — the run is the row an RQ job operates
    on, so it carries the same four states.
    """

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class ExploratorySessionStatus(str, Enum):
    """Outcome of a single charter's session within a run.

    ``COMPLETED`` regardless of how many findings it produced — a
    finding-heavy session is a successful session. ``ERROR`` means the
    session machinery itself broke.
    """

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    ERROR = "error"


class ExploratoryRun(SQLModel, table=True):
    """One requirement's exploration — list-page entity and RQ job row in one.

    Deliberately fuses what ``TestRun`` and ``TestExecution`` keep apart:
    ``TestRun`` exists as a machinery-free grouping row only because a
    scripted submission can batch several requirements, each needing its own
    job. An exploratory run covers exactly one requirement, so that level has
    nothing to group and the machinery columns land directly here.

    Charters run sequentially within the single job, so a run never has two
    browsers open against the test environment at once.
    """

    id: int | None = Field(default=None, primary_key=True)
    sprint_id: int = Field(foreign_key="sprint.id", index=True)
    requirement_id: int = Field(foreign_key="requirement.id", index=True)
    # The content this run explored against, copied at creation — see
    # TestExecution's identical trio. Charter generation is shown the
    # requirement's approved cases, so the plan revision matters here too.
    requirement_revision: int = Field(default=0)
    plan_revision: int = Field(default=0)
    env_revision: int = Field(default=0)
    # Comma-joined keys of TestEnvironmentAccess.env_vars holding application
    # URLs this run may reach (frontend, API, …), nominated by the
    # charter-generation call and validated there. Persisted so the task never
    # re-derives them. Comma-joined rather than JSON because these are
    # UPPER_SNAKE_CASE identifiers that can never contain a comma — same
    # storage choice as ExploratorySession.sfdipot_areas_csv.
    base_url_env_vars_csv: str
    # LLM synthesis of this run's session sheets. Best-effort: left None when
    # the (single, cheap) summary call fails, recoverable via the summarize
    # endpoint — the findings, not this paragraph, are the deliverable.
    summary: str | None = Field(default=None)
    # ── machinery (identical to TestExecution) ──
    status: str = Field(default=ExploratoryRunStatus.PENDING)
    retry_count: int = Field(default=0)  # automatic re-enqueues (reconciler/task)
    job_id: str | None = Field(default=None)  # RQ job id — reconciler dedup guard
    last_heartbeat: datetime | None = Field(default=None)  # worker liveness while running
    error: str | None = Field(default=None)  # user-facing summary when failed
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )

    sprint: Sprint | None = Relationship(back_populates="exploratory_runs")
    requirement: Optional["Requirement"] = Relationship(back_populates="exploratory_runs")
    sessions: list["ExploratorySession"] = Relationship(
        back_populates="exploratory_run",
        sa_relationship_kwargs={
            "cascade": "all, delete-orphan",
            "order_by": "ExploratorySession.position",
        },
    )

    @property
    def base_url_env_vars(self) -> list[str]:
        """Decoded ``base_url_env_vars_csv`` — the single accessor for the list.

        Mirrors ``TestEnvironmentAccess.env_vars``: the column name carries
        the encoding, this property carries the clean name, and nothing above
        the column ever sees the joined string.
        """
        return [name for name in self.base_url_env_vars_csv.split(",") if name]

    @property
    def requirement_name(self) -> str:
        """Name of the requirement this run explores (serialized for run cards)."""
        return self.requirement.name if self.requirement is not None else ""

    @property
    def requirement_deleted(self) -> bool:
        """See ``TestExecution.requirement_deleted`` — badge wording only."""
        return self.requirement is None or self.requirement.archived

    @property
    def outdated_reasons(self) -> list[str]:
        """Upstream artifacts that changed since this run explored."""
        return outdated_reasons(
            self.requirement,
            self.sprint.test_environment if self.sprint is not None else None,
            requirement_revision=self.requirement_revision,
            plan_revision=self.plan_revision,
            env_revision=self.env_revision,
        )

    @property
    def outdated(self) -> bool:
        """See ``TestExecution.outdated`` — backend-only, gates restart."""
        return bool(self.outdated_reasons)


class ExploratorySession(SQLModel, table=True):
    """One charter's session — the SBTM unit of work.

    Child row with its own status for resumability but no job machinery,
    exactly like ``TestCaseExecution``: a retried run skips sessions already
    finalized and restarts only the one that was in flight (a half-explored
    browser cannot be resumed — its state died with the worker).
    """

    id: int | None = Field(default=None, primary_key=True)
    exploratory_run_id: int = Field(foreign_key="exploratoryrun.id", index=True)
    position: int
    charter: str  # the approved (possibly user-edited) mission
    sfdipot_areas_csv: str  # comma-joined SfdipotArea values
    status: str = Field(default=ExploratorySessionStatus.PENDING)
    actions_used: int = Field(default=0)
    session_notes: str | None = Field(default=None)  # SBTM test-notes narrative
    # Full tool-call trace. Credential-free by construction: fill_secret
    # resolves values inside the browser executor, and literal matches against
    # the environment values are redacted as a backstop.
    action_log: str | None = Field(default=None)
    stop_reason: str | None = Field(default=None)  # charter_complete / action_cap / error
    error: str | None = Field(default=None)
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )

    exploratory_run: Optional["ExploratoryRun"] = Relationship(back_populates="sessions")
    findings: list["ExploratoryFinding"] = Relationship(
        back_populates="session",
        sa_relationship_kwargs={
            "cascade": "all, delete-orphan",
            "order_by": "ExploratoryFinding.position",
        },
    )

    @property
    def sfdipot_areas(self) -> list[str]:
        """Decoded ``sfdipot_areas_csv`` — see ``ExploratoryRun.base_url_env_vars``."""
        return [area for area in self.sfdipot_areas_csv.split(",") if area]


class ExploratoryFinding(SQLModel, table=True):
    """One bug or issue recorded during a session.

    Written live by the ``record_finding`` tool rather than parsed out of the
    session notes afterwards — that is the only way to capture a screenshot
    of the failure while it is still on screen.
    """

    id: int | None = Field(default=None, primary_key=True)
    exploratory_session_id: int = Field(foreign_key="exploratorysession.id", index=True)
    position: int
    finding_type: str  # FindingType value
    severity: str  # FindingSeverity value
    title: str
    steps_to_reproduce: str  # newline-joined, matching TestCase.steps storage
    expected: str
    actual: str
    # Absolute path — StorageService.store_screenshot returns abspath. None
    # whenever STORE_OFFLINE is false: the normal case for that setting, not
    # an error.
    screenshot_path: str | None = Field(default=None)
    # Browser, viewport, OS, and page URL at the moment of recording —
    # captured in code, never asked of the model. Best-effort: None on rows
    # written before capture existed, and on a page too broken to answer.
    environment: str | None = Field(default=None)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )

    session: Optional["ExploratorySession"] = Relationship(back_populates="findings")
