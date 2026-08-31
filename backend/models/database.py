"""Definition of database models"""

import json
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from sqlalchemy import UniqueConstraint
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

    @property
    def has_access_token(self) -> bool:
        """Whether an access token is stored — never the token itself.

        Serialized so the issue-tracker form can say whether ticking "use
        this sprint's repository" will supply a credential, instead of the
        user learning it from a save that fails verification.
        """
        return bool(self.github_token)


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
    nonfunctional_runs: list["NonfunctionalRun"] = Relationship(back_populates="sprint")
    # The sprint's distinct defects. Deliberately *not* eager-loaded by the
    # metrics endpoint: it counts `defect_group_id` on rows it already
    # loads and never dereferences one, so the panel needs counts rather
    # than representative text.
    defect_groups: list["DefectGroup"] = Relationship(back_populates="sprint")
    issue_tracker: Optional["IssueTrackerConfig"] = Relationship(
        back_populates="sprint", sa_relationship_kwargs={"uselist": False}
    )
    cicd_config: Optional["CicdConfig"] = Relationship(
        back_populates="sprint", sa_relationship_kwargs={"uselist": False}
    )
    cicd_exports: list["CicdExport"] = Relationship(back_populates="sprint")

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
    def environment_confirmed(self) -> bool:
        """Whether the test environment has been confirmed.

        Named for what it reports rather than what it used to enforce: this
        was ``requirements_locked`` back when confirming the environment
        froze the requirement set. Requirements are editable now — adding
        one simply sends the environment back for re-checking — so the only
        thing left hanging off this flag is the plan-generation gate, which
        is a genuine precondition rather than a freeze.
        """
        return (
            self.test_environment is not None
            and self.test_environment.status == TestEnvironmentStatus.CONFIRMED
        )

    @property
    def has_test_plans(self) -> bool:
        """Whether at least one requirement has a test plan row."""
        return any(r.test_plan is not None for r in self.requirements)

    @property
    def test_plans_missing(self) -> bool:
        """Whether a confirmed requirement has no test plan row.

        Mirrors exactly what ``generate_test_plans`` would create, so the UI
        can offer the button precisely when pressing it would do something.

        Reachable long after the first generation: editing a confirmed
        requirement removes the plan written against its old text
        (``services/invalidation.py``), and nothing regenerates it
        automatically.  Without this flag the test-plans page has no way to
        tell a sprint whose plans are all present from one missing the plan
        for the requirement just edited — the plan list looks identical,
        because a requirement with no plan contributes no row.
        """
        return any(
            r.status == RequirementStatus.CONFIRMED and r.test_plan is None
            for r in self.requirements
        )

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

    @property
    def has_nonfunctional_runs(self) -> bool:
        """Whether at least one nonfunctional run has been started for this sprint.

        A property rather than something the route composes, so it coerces
        straight off the row like its two siblings. Not a gate: the run
        stage's gate is unchanged, and this only drives the third list's
        empty state.
        """
        return any(self.nonfunctional_runs)


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

# Same disposition as the above (fail the row, stop working on it) but a
# different cause — reporting "sprint finished" for a deleted requirement
# sends a reader looking at the wrong thing entirely.
REQUIREMENT_DELETED_ERROR = "The requirement was deleted before this finished."

# Stored on a run abandoned because an upstream artifact changed under it.
# Not a failure of the run: it was testing something that has since moved,
# and finishing would spend LLM calls and wall-clock on a result already
# marked out of date.
SUPERSEDED_ERROR = (
    "Superseded — the requirement, test plan, or test environment changed while this was running."
)


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
    nonfunctional_runs: list["NonfunctionalRun"] = Relationship(back_populates="requirement")

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
    # What the cached script was written against, stamped at the same moment
    # the script is cached. Read by `services/cicd_eligibility.py` through the
    # *same* `outdated_reasons` comparison the run badges use, so "out of
    # date" cannot come to mean two different things.
    #
    # NULL means the script predates staleness tracking, and its staleness is
    # therefore unknowable — which reads as **stale** rather than as a third
    # per-case state (unknown and stale share one remedy: re-run the case).
    script_requirement_revision: int | None = Field(default=None)
    script_plan_revision: int | None = Field(default=None)
    script_env_revision: int | None = Field(default=None)

    test_plan: Optional["TestPlan"] = Relationship(back_populates="all_cases")


# ── Issue tracker ─────────────────────────────────────────────────────


class IssueTrackerProvider(str, Enum):
    """Where a sprint's bug findings are filed.

    Exactly one per sprint, never both: a finding that exists as two
    tickets in two systems is worse than one that exists in neither.
    """

    JIRA = "jira"
    GITHUB = "github"


class IssueTrackerConfig(SQLModel, table=True):
    """One sprint's connection to a Jira project or a GitHub Issues repo.

    Editable — provider switch included — which is why every finding
    records the ``tracker_target`` it was filed against (see
    ``TestCaseExecution``).  Credentials are verified against the live
    tracker on every save, so ``verified_at`` records when that last
    succeeded; it is displayed, never branched on.
    """

    id: int | None = Field(default=None, primary_key=True)
    sprint_id: int = Field(foreign_key="sprint.id", unique=True, index=True)
    provider: str  # IssueTrackerProvider value
    base_url: str | None = Field(default=None)  # Jira site root; None for GitHub
    account_email: str | None = Field(default=None)  # Jira Basic-auth user
    # Fernet-encrypted, exactly like Repo.github_token: never serialized,
    # never logged, decrypted only to build an outbound request.
    api_token: str
    target: str  # Jira project key | "owner/repo"
    issue_type: str | None = Field(default=None)  # Jira issue type name
    verified_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )

    sprint: Sprint | None = Relationship(back_populates="issue_tracker")

    @property
    def tracker_target(self) -> str:
        """``"{provider}:{target}"`` — what a filed finding records.

        The de-duplication window is scoped by this string rather than by
        the sprint alone, because the config is editable.  It matters most
        on GitHub, whose issue numbers are per-repo integers: without it,
        repo B's ``#7`` would answer ``issue_is_open`` for repo A's ``#7``
        and a finding would be attached to an unrelated ticket.
        """
        return f"{self.provider}:{self.target}"

    @property
    def target_label(self) -> str:
        """Human-readable "where findings go", for the connect panel."""
        provider = "Jira" if self.provider == IssueTrackerProvider.JIRA else "GitHub"
        return f"{provider} · {self.target}"


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


class DefectPool(str, Enum):
    """Which pool of distinct defects a group belongs to.

    Functional bugs (a feature is wrong) and nonfunctional ones (a rule a
    tool checks was broken) are never the same defect, and grouping them
    together would ask an LLM to decide whether "login rejects a valid
    password" and "the login button has no accessible name" describe one
    problem. They do not, and the answer would not be stable.

    Kept as a column on the group rather than derived from its members at
    read time: ``finding_grouping`` filters the known-defect read by it
    before the LLM sees anything, so it has to be queryable.
    """

    FUNCTIONAL = "functional"
    NONFUNCTIONAL = "nonfunctional"


class DefectGroup(SQLModel, table=True):
    """One distinct defect in a sprint — what several findings describe.

    Written only by ``services/finding_grouping.py``, once per completed
    run, and **append-only**: a group's membership grows, and neither the
    group nor an existing member is ever rewritten.  Both finding carriers
    point here through a nullable ``defect_group_id``, so "how many bugs
    did this sprint find" is answered by counting groups rather than
    finding texts — paraphrase-aware, and independent of whether an issue
    tracker is connected.

    The three text fields are **frozen at creation**.  They are what the
    model is shown as the sprint's known defects on every later run, so a
    representative re-elected per run would describe the same defect
    differently each week and make matching progressively harder.  The
    ticket body still comes from a live finding row, which carries steps,
    severity, environment, and a screenshot; this text is for *matching*.

    Deliberately no ``severity`` column: severity is the max over the
    group's members, computed where it is already computed
    (``qa_metrics``' ``high_severity_bug_count``).  Storing it would mean
    rewriting a group whenever a higher-severity member joined — a write
    on the one path whose whole point is that nothing rewrites an existing
    group — and would become a second source of truth the first time one
    of those writes was missed.  Same argument
    ``TestCaseExecution.finding_type`` already rests on.
    """

    id: int | None = Field(default=None, primary_key=True)
    sprint_id: int = Field(foreign_key="sprint.id", index=True)
    # Which pool this defect belongs to. Indexed because every grouping
    # pass filters the sprint's known defects by it before matching.
    # Rows written before this column existed are backfilled to
    # `functional` by a migration — a NULL here would drop the group out
    # of every known-defect read and silently re-open a defect that
    # already exists.
    pool: str = Field(default=DefectPool.FUNCTIONAL, index=True)
    title: str
    expected: str
    actual: str
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )

    sprint: Optional["Sprint"] = Relationship(back_populates="defect_groups")
    tickets: list["DefectGroupTicket"] = Relationship(back_populates="defect_group")


class DefectGroupTicket(SQLModel, table=True):
    """Where one defect lives in one tracker.  Written only by finding_export.

    A child table rather than three columns on ``DefectGroup``, because a
    defect can outlive the tracker it was first filed to.  With one triple
    on the group, filing into a newly connected tracker would overwrite
    the old target and forget the earlier ticket — so switching **back**
    would file a duplicate of a ticket that already exists and is probably
    still open.

    The unique constraint states the invariant in the schema: one defect
    gets at most one ticket in any given tracker.  That is the
    group-is-one-ticket rule extended along the axis a config edit moves
    on, and it is also what makes the upsert in ``finding_export`` the
    only legal move rather than a preference.

    (Note the claim is about the *row*, not the tracker: two sibling jobs
    exporting concurrently can still both create an issue, since nothing
    serializes ``_export`` itself.  Only one row survives, and the extra
    ticket is a duplicate a human can close.)
    """

    __table_args__ = (UniqueConstraint("defect_group_id", "tracker_target"),)

    id: int | None = Field(default=None, primary_key=True)
    defect_group_id: int = Field(foreign_key="defectgroup.id", index=True)
    tracker_target: str  # "{provider}:{target}", as stamped at file time
    issue_key: str  # "QA-142" | "7"
    issue_url: str  # absolute; "" when the provider gave none
    # Advanced explicitly whenever this row is repointed at a replacement
    # ticket: a default_factory fires on insert only.
    filed_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )

    defect_group: Optional["DefectGroup"] = Relationship(back_populates="tickets")


# ── Export roll-up (shared by scripted and exploratory runs) ─────────


def export_rollup(findings: list, *, export_findings: bool = False) -> dict:
    """Summarize what a run's bug findings did on their way to a tracker.

    Computed at response time, never stored — the same treatment the case
    counts already get (Convention #10 applied one level down).

    *findings* is any sequence of objects carrying the tracker receipt
    columns, so one definition serves both carriers and the run page
    cannot come to mean two different things depending on which mode
    produced the run.

    The counts are deliberately nested rather than disjoint:
    ``export_error_count`` is a subset of ``unexported_finding_count``.
    That is what lets the page use one condition to decide whether to
    offer the button and the other to word it — "File 6 bugs" for a run
    that never filed, "Retry" for one that tried and failed.

    ``groups`` is exposed rather than only counts because the whole point
    of grouping is that six findings can be two tickets, and a reader
    should not have to open every card to work out which four became
    QA-142.

    *export_findings* is the run's own toggle — the one **stored** value
    carried here, passed through rather than derived.  Without it the page
    cannot tell "was set to file and has not yet" from "was never set to
    file", which are different things to say to a reader now that the
    button files either way.
    """
    exported = [f for f in findings if f.tracker_issue_key]
    unexported = [f for f in findings if not f.tracker_issue_key]

    groups: dict[str, dict] = {}
    for finding in exported:
        group = groups.setdefault(
            finding.tracker_issue_key,
            {
                "issue_key": finding.tracker_issue_key,
                "issue_url": finding.tracker_issue_url or "",
                "finding_count": 0,
            },
        )
        group["finding_count"] += 1

    return {
        "export_findings": export_findings,
        "exported_finding_count": len(exported),
        "exported_issue_count": len(groups),
        "export_error_count": sum(1 for f in unexported if f.tracker_error),
        "unexported_finding_count": len(unexported),
        "export_groups": list(groups.values()),
    }


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
    """Outcome of a single test case within a TestExecution.

    ``SKIPPED`` is the only value the walking loop never writes: it is
    stamped by ``services/finalization.py`` when the parent execution
    finishes without reaching this case.  Deliberately *not* part of the
    loop's finalized set — a restart re-runs a skipped case, so the row
    carries no stale verdict forward (see the ``finding_type`` note below
    for the same reasoning applied to the type).
    """

    __test__ = False  # tell pytest this "Test*" name is not a test class

    PENDING = "pending"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"
    SKIPPED = "skipped"


class TestRun(SQLModel, table=True):
    """One submission ("Run new test") covering one or more requirements.

    Carries no worker-job machinery of its own — status and requirement
    names are derived from its executions on every read, the same spirit
    as Sprint's computed flags (Convention #10), applied one level down.
    """

    __test__ = False  # tell pytest this "Test*" name is not a test class

    id: int | None = Field(default=None, primary_key=True)
    sprint_id: int = Field(foreign_key="sprint.id", index=True)
    # Decided at run start, never afterwards: whether this run's bug
    # findings are filed to the sprint's issue tracker when it completes.
    export_findings: bool = Field(default=False)
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

    @property
    def bug_findings(self) -> list["TestCaseExecution"]:
        """Every case in this run that reported the product being wrong.

        Gated on a report as well as a type, which matters here rather than
        being pedantry: this list is what ``export_rollup`` counts and what
        ``finding_export`` files.  An ungated case with no report would land
        in ``unexported_finding_count``, so the run page would offer to file
        a bug that does not exist — and pressing the button would hand that
        row to the exporter.  ``services/findings.py`` owns that gate for
        every reader of it.
        """
        # Imported here rather than at module scope: `services/findings.py`
        # reads these models, so a top-level import would be a cycle.
        from backend.services.findings import iter_findings

        return [finding.row for finding in iter_findings(self, bugs_only=True)]

    # The export roll-up is deliberately *not* exposed here as five
    # properties. Each would re-walk `executions x cases` on every access,
    # and caching one on the row would be stale the moment something
    # mutates findings and re-serializes the same instance in a single
    # request — which is exactly what the export-findings retry route
    # does. `routes/test_execution.py::_run_detail` splats
    # `export_rollup(...)` once instead, the arrangement
    # `routes/exploratory.py` already uses for its own two builders.


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
    # ── issue-tracker receipt (written only by services/finding_export.py) ──
    # Deliberately *not* cleared alongside the finding fields on a passing
    # case: a filed ticket is a receipt for an irreversible action in a
    # system this app does not own. The finding may stop reporting itself;
    # the record of having reported it may not.
    tracker_issue_key: str | None = Field(default=None)  # "QA-142" | "7"
    tracker_issue_url: str | None = Field(default=None)  # absolute, self-describing
    tracker_error: str | None = Field(default=None)  # last filing failure, cleared on success
    # IssueTrackerConfig.tracker_target as it stood when this was filed.
    tracker_target: str | None = Field(default=None)
    tracker_is_duplicate: bool = Field(default=False)  # grouped into another finding's ticket
    # ── which distinct defect this finding is an occurrence of ──
    # Never cleared. A finalized case is never re-walked
    # (tasks/execute_test.py's loop skips terminal rows) and nothing else
    # writes these rows, so a fixed bug leaves its old failed row — and
    # this FK — exactly as they were, in the run that observed it. A
    # re-run writes new rows. Deliberately absent from `_NO_FINDING`,
    # which would be clearing something unreachable.
    defect_group_id: int | None = Field(default=None, foreign_key="defectgroup.id", index=True)
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
    def finding(self) -> dict[str, str | bool | None] | None:
        """The finding in the shared shape, or None when there isn't one.

        Keyed to match ``FindingBase`` in ``models/types.py`` so FastAPI can
        validate it straight into the nested response model.

        **Gated on ``finding_title`` rather than on status**, and the gate
        is load-bearing: ``FindingBase.title`` is declared ``str`` over a
        nullable column, so an ungated null title fails response validation
        and 500s the *whole* run-detail response rather than just this
        card.  That is the same argument the coalescing below rests on —
        the gate is simply where it applies to the title itself.

        (The gate originally read as an accommodation for rows written
        before findings were structured.  It is not: ``finding_type`` is
        derived from ``status`` alone, so *any* path that marks a case
        ``failed`` without writing the report — today's writer does write
        them as a group — would take the whole page down with it.)

        The five required fields below the title are coalesced because
        ``FindingBase`` declares them non-optional over nullable columns: a
        single ``None`` would fail response validation and 500 the *whole*
        run detail response rather than just this card.  The task always writes them as
        a group, so this should be unreachable — but the blast radius is far
        out of proportion to the cost of a fallback.  ``environment`` is
        left alone: the response model already allows it to be null.

        The four tracker fields pass straight through for the same reason —
        the response model allows null, and coalescing them would turn "not
        filed" into an empty link.
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
            "tracker_issue_key": self.tracker_issue_key,
            "tracker_issue_url": self.tracker_issue_url,
            "tracker_error": self.tracker_error,
            "tracker_is_duplicate": self.tracker_is_duplicate,
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
    session machinery itself broke.  ``SKIPPED`` means the charter was
    never explored at all because the run ended first — written only by
    ``services/finalization.py``, exactly like its scripted twin.
    """

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    ERROR = "error"
    SKIPPED = "skipped"


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
    # See TestRun.export_findings — decided at run start, read at completion.
    export_findings: bool = Field(default=False)
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

    @property
    def bug_findings(self) -> list["ExploratoryFinding"]:
        """Every finding in this run reporting the product being wrong."""
        # See TestRun.bug_findings for why this import is deferred.
        from backend.services.findings import iter_findings

        return [finding.row for finding in iter_findings(self, bugs_only=True)]


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

    @property
    def finding_count(self) -> int:
        """How many findings this session recorded.

        A property so the summary response can be coerced straight off the
        row (``Repo.has_access_token``'s precedent) rather than composed
        field by field in the route.
        """
        return len(self.findings)


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
    # ── issue-tracker receipt — see TestCaseExecution's identical block ──
    tracker_issue_key: str | None = Field(default=None)
    tracker_issue_url: str | None = Field(default=None)
    tracker_error: str | None = Field(default=None)
    tracker_target: str | None = Field(default=None)
    tracker_is_duplicate: bool = Field(default=False)
    # Which distinct defect this is an occurrence of — see
    # TestCaseExecution's identical column. Never cleared: `record_finding`
    # only ever creates rows, and the restart route leaves child rows alone.
    defect_group_id: int | None = Field(default=None, foreign_key="defectgroup.id", index=True)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )

    session: Optional["ExploratorySession"] = Relationship(back_populates="findings")

    @property
    def has_screenshot(self) -> bool:
        """Whether a screenshot was captured and stored for this finding.

        The path itself is never serialized — it is a server-side location,
        and the image is served by its own endpoint. Clients only need to
        know whether to ask for it.
        """
        return self.screenshot_path is not None


# ── Nonfunctional testing ─────────────────────────────────────────────
#
# The third run mode. A run covers one requirement, drives a real browser
# through the approved plan's steps, and runs a fixed catalogue —
# accessibility, single-request performance, passive security — at every
# URL it lands on. Approved load profiles then run last.
#
# The oracle is inverted relative to the other two modes: the *tools*
# decide what is a violation and how severe it is, and the model only
# navigates and writes prose. That is why nothing below has a column an
# LLM verdict could land in.


class NonfunctionalRunStatus(str, Enum):
    """Lifecycle of one requirement's nonfunctional run.

    The same four states as ``ExploratoryRunStatus``, for the same reason:
    the run is the row an RQ job operates on.
    """

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class NonfunctionalChildStatus(str, Enum):
    """Outcome of one target or one load profile within a run.

    Shared by both child types because they mean the same thing on each:
    ``ERROR`` is the machinery breaking, ``SKIPPED`` is never reached at
    all — written only by ``services/finalization.py``, exactly like its
    two older twins.

    One caution specific to a load profile: ``SKIPPED`` there does **not**
    mean "safe to re-run". A profile that already put traffic on the host
    is never re-sent whatever its status says; the invariant is carried by
    ``NonfunctionalLoadProfile.requests_sent``, not by this column.
    """

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    ERROR = "error"
    SKIPPED = "skipped"


class NonfunctionalDomain(str, Enum):
    """What the catalogue examines at each target.

    Fixed and small by design: every selected domain runs at every target,
    so "we did not look" is never a possible reading of a clean result.
    """

    ACCESSIBILITY = "accessibility"
    PERFORMANCE = "performance"
    SECURITY = "security"


class DomainOutcome(str, Enum):
    """What one domain found at one target — four-valued, not two.

    ``NOT_APPLICABLE`` and ``FAILED_TO_RUN`` exist because collapsing
    either into ``CLEAN`` states something false: accessibility does not
    apply to a JSON endpoint, and an axe injection a strict CSP refused
    found nothing *because it never ran*. A recorded outcome, never a
    silent skip.
    """

    CLEAN = "clean"
    VIOLATIONS = "violations"
    NOT_APPLICABLE = "not_applicable"
    FAILED_TO_RUN = "failed_to_run"


class TargetKind(str, Enum):
    """Whether a target is a rendered page or an API endpoint.

    Endpoints are discovered from the browser's own XHR/fetch traffic, so
    they are URLs the application itself calls rather than anything
    guessed or crawled.
    """

    PAGE = "page"
    ENDPOINT = "endpoint"


class LoadMethod(str, Enum):
    """HTTP method a load profile may use, split by whether it is *safe*.

    "Safe" is the HTTP sense: the request is a read, so repeating it two
    thousand times changes nothing but the load. Non-safe methods change
    data, which is why they need the disposable-environment declaration
    and a much lower ceiling — see ``NonfunctionalRun.environment_disposable``.
    """

    GET = "GET"
    HEAD = "HEAD"
    OPTIONS = "OPTIONS"
    POST = "POST"
    PUT = "PUT"
    PATCH = "PATCH"
    DELETE = "DELETE"

    @classmethod
    def safe_methods(cls) -> set[str]:
        """The read-only methods, allowed on any confirmed origin."""
        return {cls.GET.value, cls.HEAD.value, cls.OPTIONS.value}

    @classmethod
    def is_safe(cls, method: str | None) -> bool:
        """Whether *method* only reads. Unknown methods are **not** safe.

        Resolving the doubt toward "non-safe" is the cheap mistake: it
        costs a refusal the user can act on, where the other direction
        costs writes against an environment nobody declared disposable.
        """
        return (method or "").upper() in cls.safe_methods()


class NonfunctionalRun(SQLModel, table=True):
    """One requirement's nonfunctional run — list entity and RQ job row in one.

    Fuses the two levels exactly as ``ExploratoryRun`` does, and for the
    same reason: a run covers one requirement, so there is nothing above it
    to group.

    Carries **two** child row types rather than one — the targets it
    examined and the load profiles it applied — which is what
    ``finalization.RowSpec.child_specs`` was widened for.
    """

    id: int | None = Field(default=None, primary_key=True)
    sprint_id: int = Field(foreign_key="sprint.id", index=True)
    requirement_id: int = Field(foreign_key="requirement.id", index=True)
    # What this run examined against, copied at creation — see
    # ExploratoryRun's identical trio.
    requirement_revision: int = Field(default=0)
    plan_revision: int = Field(default=0)
    env_revision: int = Field(default=0)
    # Comma-joined env-var names holding the application URLs this run may
    # reach. Also the coverage floor: every one of them is seeded as a
    # target before the model navigates anywhere. Comma-joined for the same
    # reason as ExploratoryRun.base_url_env_vars_csv.
    base_url_env_vars_csv: str
    # Comma-joined NonfunctionalDomain values the user approved. Every one
    # of them runs at every target.
    domains_csv: str
    # Whether the user declared this environment disposable — the gate on
    # non-safe load methods, and the only thing that unlocks the second,
    # lower ceiling tier. Stored on the run because it describes what this
    # run was permitted to do, which a later config change must not rewrite.
    environment_disposable: bool = Field(default=False)
    # Best-effort synthesis, recoverable via the summarize endpoint. See
    # ExploratoryRun.summary — the findings are the deliverable, not this.
    summary: str | None = Field(default=None)
    # See TestRun.export_findings — decided at run start, read at completion.
    export_findings: bool = Field(default=False)
    # ── machinery (identical to ExploratoryRun) ──
    status: str = Field(default=NonfunctionalRunStatus.PENDING)
    retry_count: int = Field(default=0)
    job_id: str | None = Field(default=None)
    last_heartbeat: datetime | None = Field(default=None)
    error: str | None = Field(default=None)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )

    sprint: Sprint | None = Relationship(back_populates="nonfunctional_runs")
    requirement: Optional["Requirement"] = Relationship(back_populates="nonfunctional_runs")
    targets: list["NonfunctionalTarget"] = Relationship(
        back_populates="nonfunctional_run",
        sa_relationship_kwargs={
            "cascade": "all, delete-orphan",
            "order_by": "NonfunctionalTarget.position",
        },
    )
    load_profiles: list["NonfunctionalLoadProfile"] = Relationship(
        back_populates="nonfunctional_run",
        sa_relationship_kwargs={
            "cascade": "all, delete-orphan",
            "order_by": "NonfunctionalLoadProfile.position",
        },
    )

    @property
    def base_url_env_vars(self) -> list[str]:
        """Decoded ``base_url_env_vars_csv`` — see ``ExploratoryRun``'s twin."""
        return [name for name in self.base_url_env_vars_csv.split(",") if name]

    @property
    def domains(self) -> list[str]:
        """Decoded ``domains_csv`` — the single accessor for the domain list."""
        return [domain for domain in self.domains_csv.split(",") if domain]

    @property
    def requirement_name(self) -> str:
        """Name of the requirement this run examines (serialized for run cards)."""
        return self.requirement.name if self.requirement is not None else ""

    @property
    def requirement_deleted(self) -> bool:
        """See ``TestExecution.requirement_deleted`` — badge wording only."""
        return self.requirement is None or self.requirement.archived

    @property
    def outdated_reasons(self) -> list[str]:
        """Upstream artifacts that changed since this run executed."""
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

    @property
    def bug_findings(self) -> list["NonfunctionalFinding"]:
        """Every finding in this run reporting the product being wrong."""
        # See TestRun.bug_findings for why this import is deferred.
        from backend.services.findings import iter_findings

        return [finding.row for finding in iter_findings(self, bugs_only=True)]


class NonfunctionalTarget(SQLModel, table=True):
    """One URL the run examined, and what each domain found there.

    A child row with its own status for resumability but no job machinery,
    exactly like ``ExploratorySession``.

    The per-domain outcome columns are the point of the table: decision 6
    says the full catalogue runs at every target, so each domain owes an
    answer here whether or not it found anything. A NULL outcome means the
    target was never reached, not that the domain was clean.
    """

    id: int | None = Field(default=None, primary_key=True)
    nonfunctional_run_id: int = Field(foreign_key="nonfunctionalrun.id", index=True)
    position: int
    url: str
    kind: str = Field(default=TargetKind.PAGE)
    status: str = Field(default=NonfunctionalChildStatus.PENDING)
    error: str | None = Field(default=None)
    # DomainOutcome per domain — None when that domain was not selected for
    # the run, so "not selected" and "found nothing" stay distinguishable.
    a11y_outcome: str | None = Field(default=None)
    security_outcome: str | None = Field(default=None)
    performance_outcome: str | None = Field(default=None)
    # Measured performance for this target: timings and Core Web Vitals.
    # Data only — decision 11 keeps performance out of findings entirely,
    # so nothing here ever becomes a defect or a ticket.
    metrics_json: str | None = Field(default=None)
    # One page screenshot, taken while the page is still on screen. Every
    # finding from this target copies the path: findings are created later,
    # at triage, when the page is long gone.
    screenshot_path: str | None = Field(default=None)
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )

    nonfunctional_run: Optional["NonfunctionalRun"] = Relationship(back_populates="targets")
    findings: list["NonfunctionalFinding"] = Relationship(
        back_populates="target",
        sa_relationship_kwargs={
            "cascade": "all, delete-orphan",
            "order_by": "NonfunctionalFinding.position",
        },
    )


class NonfunctionalLoadProfile(SQLModel, table=True):
    """One approved load profile and the traffic it actually applied.

    The second child type, and the one with a rule no other row in this
    application has: **a profile that already sent traffic is never
    re-sent**, whatever its status says. A restart re-examines targets
    freely — re-reading a page costs nothing — but re-issuing a profile
    duplicates real requests against someone's environment, and for a
    non-safe method that means duplicated writes. ``requests_sent > 0`` is
    the invariant; ``status`` is not.
    """

    id: int | None = Field(default=None, primary_key=True)
    nonfunctional_run_id: int = Field(foreign_key="nonfunctionalrun.id", index=True)
    position: int
    url: str
    method: str = Field(default=LoadMethod.GET)
    # Request body, with `$NAME` placeholders resolved against the sprint's
    # env vars **inside** the load runner. Stored with the placeholders, so
    # no credential ever lands in this column.
    body: str | None = Field(default=None)
    concurrency: int = Field(default=1)
    duration_seconds: int = Field(default=10)
    total_request_cap: int = Field(default=100)
    status: str = Field(default=NonfunctionalChildStatus.PENDING)
    # How many requests actually reached the host. The never-re-send
    # invariant reads this and nothing else.
    requests_sent: int = Field(default=0)
    # Aggregated LoadResult — percentiles, throughput, status distribution.
    # Data only, exactly like NonfunctionalTarget.metrics_json.
    results_json: str | None = Field(default=None)
    error: str | None = Field(default=None)
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )

    nonfunctional_run: Optional["NonfunctionalRun"] = Relationship(back_populates="load_profiles")


class NonfunctionalFinding(SQLModel, table=True):
    """One violation a tool found at one target.

    Deliberately keyed by ``(rule, url)`` upstream: every node that broke a
    rule on a page is listed *inside* one finding rather than filed as
    twenty. ``severity`` comes from axe's own ``impact`` or the passive
    security table — never from a model, which is why the triage response
    schema has no severity field at all.

    ``rule`` and ``domain`` are what make a finding traceable back to the
    tool that produced it; everything else is the shared ``FindingBase``
    shape the one frontend card renders.
    """

    id: int | None = Field(default=None, primary_key=True)
    nonfunctional_target_id: int = Field(foreign_key="nonfunctionaltarget.id", index=True)
    position: int
    domain: str  # NonfunctionalDomain value
    rule: str  # axe rule id, or the passive-security rule name
    finding_type: str  # FindingType value
    severity: str  # FindingSeverity value
    title: str
    steps_to_reproduce: str
    expected: str
    actual: str
    # Copied from the target's page screenshot — see NonfunctionalTarget.
    screenshot_path: str | None = Field(default=None)
    environment: str | None = Field(default=None)
    # ── issue-tracker receipt — see TestCaseExecution's identical block ──
    tracker_issue_key: str | None = Field(default=None)
    tracker_issue_url: str | None = Field(default=None)
    tracker_error: str | None = Field(default=None)
    tracker_target: str | None = Field(default=None)
    tracker_is_duplicate: bool = Field(default=False)
    defect_group_id: int | None = Field(default=None, foreign_key="defectgroup.id", index=True)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )

    target: Optional["NonfunctionalTarget"] = Relationship(back_populates="findings")

    @property
    def has_screenshot(self) -> bool:
        """See ``ExploratoryFinding.has_screenshot`` — the path is never serialized."""
        return self.screenshot_path is not None


# ── CI/CD export ──────────────────────────────────────────────────────
#
# A sprint's cached Playwright scripts become a pull request against the
# sprint's own repository: our verified scripts committed verbatim, plus CI
# files an LLM authors by reading the repo's existing conventions.


class CicdProvider(str, Enum):
    """Which CI system the exported job is written for.

    Both ship as a **GitHub pull request** — Jenkins differs in what is
    written (a Groovy stage rather than a workflow job), not in how it is
    delivered. That is why a provider switch does not invalidate the stored
    credential: it is a GitHub write token either way.
    """

    GITHUB_ACTIONS = "github_actions"
    JENKINS = "jenkins"


class CicdConfig(SQLModel, table=True):
    """One sprint's CI/CD export connection — provider plus a write token.

    Mirrors ``IssueTrackerConfig``, with one column deliberately absent:
    there is no ``target``.  The destination is always the sprint's own
    repository, derived from ``Repo.github_link``, so there is nothing for a
    form to name and nothing for a typo to redirect.

    The token is verified against the live repository's ``permissions.push``
    on **every** save — a stronger claim than the tracker's "can I file an
    issue here", and checked at save time so a read-only token is refused
    before an LLM call has been spent on it.  ``verified_at`` records when
    that last succeeded; it is displayed, never branched on, and there is
    deliberately no background re-verification.
    """

    id: int | None = Field(default=None, primary_key=True)
    sprint_id: int = Field(foreign_key="sprint.id", unique=True, index=True)
    provider: str  # CicdProvider value
    # Fernet-encrypted, exactly like Repo.github_token and
    # IssueTrackerConfig.api_token: never serialized, never logged,
    # decrypted only to build an outbound request. This one is
    # write-scoped, which makes it the highest-privilege credential the
    # application holds.
    access_token: str
    # Free text describing the CI environment the suite will run against
    # (self-hosted runner, container image, service dependencies). Handed to
    # the model as context; never parsed.
    ci_environment_hint: str | None = Field(default=None)
    verified_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )

    sprint: Sprint | None = Relationship(back_populates="cicd_config")


class CicdExportStatus(str, Enum):
    """Lifecycle status of one export attempt.

    The same four states as every other job-backed row, so ``RowSpec`` and
    ``SweepSpec`` apply unchanged.
    """

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class CicdExport(SQLModel, table=True):
    """One export attempt — the row an RQ job operates on.

    ::

                        POST /cicd-exports
                                │
                                ▼
                           ┌─────────┐   enqueue_rows best-effort
                           │ PENDING │◄────────────────────────────┐
                           └────┬────┘                             │
                                │ task picks it up                 │ retry_count
                                ▼                                  │ < MAX_AUTO_RETRIES
                           ┌─────────┐                             │
                        ┌─▶│ RUNNING │──── raise ─▶ record_failure ─┤
            restart     │  └────┬────┘                             │
            (uncapped)  │       │ commit + PR                      ▼
                        │       ▼                             ┌────────┐
                        │  ┌───────────┐                      │ FAILED │
                        │  │ COMPLETED │                      └────┬───┘
                        │  └───────────┘                           │
                        └──────────────────────────────────────────┘
                           (refused while RUNNING; a fresh branch each
                            attempt is what makes a retry idempotent)

          SWEEP INTERACTION — inactive_sprint_ok=True
            _sweep_inactive_sprints   skipped   ── a finished sprint may export
            finish_sprint loop        skipped   ── same reason
            _sweep_pending            RUNS, without the Sprint.active predicate
            _sweep_stale_heartbeats   RUNS      ── never had a Sprint join
                                                  ↑ full crash recovery retained

    Gating only the first two would strand a ``pending`` export forever on a
    finished sprint after a Redis outage: the inactive sweeps skip it, the
    heartbeat sweep only sees ``running`` rows, and ``_sweep_pending``'s
    ``Sprint.active`` predicate hides it. Restart re-pends it into the same
    hole.
    """

    id: int | None = Field(default=None, primary_key=True)
    sprint_id: int = Field(foreign_key="sprint.id", index=True)
    # Copied from CicdConfig at creation — the config is editable, and this
    # export was authored for one provider's conventions.
    provider: str  # CicdProvider value
    # What the user picked, as a JSON list of TestCase ids.
    #
    # Distinct from `items`, which are *receipts* written only after the
    # commit succeeds: between creation and completion the job needs to know
    # what was asked for, and on a failed export `items` is empty by design.
    # The job re-derives eligibility from the database and intersects, so a
    # case archived between selection and job start is skipped rather than
    # fatal.
    selected_case_ids_json: str | None = Field(default=None)
    # ── receipts, all written only after the commit succeeds ──
    branch_name: str | None = Field(default=None)
    commit_sha: str | None = Field(default=None)
    pr_number: int | None = Field(default=None)
    pr_url: str | None = Field(default=None)
    ci_file_paths_json: str | None = Field(default=None)  # JSON list of committed CI paths
    dropped_paths_json: str | None = Field(default=None)  # JSON list refused by the allowlist
    variable_names_json: str | None = Field(default=None)  # JSON list of CI variable names
    secret_names_json: str | None = Field(default=None)  # JSON list of CI secret names
    pr_title: str | None = Field(default=None)
    notes: str | None = Field(default=None)  # the model's own caveats, verbatim
    # ── machinery (identical to ExploratoryRun / TestExecution) ──
    status: str = Field(default=CicdExportStatus.PENDING)
    retry_count: int = Field(default=0)
    job_id: str | None = Field(default=None)
    last_heartbeat: datetime | None = Field(default=None)
    error: str | None = Field(default=None)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )

    sprint: Sprint | None = Relationship(back_populates="cicd_exports")
    items: list["CicdExportItem"] = Relationship(
        back_populates="cicd_export",
        sa_relationship_kwargs={
            "cascade": "all, delete-orphan",
            "order_by": "CicdExportItem.id",
        },
    )

    @property
    def selected_case_ids(self) -> list[int]:
        """The ids the user picked — decoded once, here."""
        return json.loads(self.selected_case_ids_json) if self.selected_case_ids_json else []

    @property
    def case_count(self) -> int:
        """How many test cases this export shipped.

        A property so the response coerces straight off the row rather than
        being composed field by field (``Repo.has_access_token``'s
        precedent).
        """
        return len(self.items)

    @property
    def ci_file_paths(self) -> list[str]:
        """Committed CI file paths — decoded once, here."""
        return json.loads(self.ci_file_paths_json) if self.ci_file_paths_json else []

    @property
    def dropped_paths(self) -> list[str]:
        """Paths the validation gate refused, so the page can name them."""
        return json.loads(self.dropped_paths_json) if self.dropped_paths_json else []

    @property
    def variable_names(self) -> list[str]:
        """CI **variable** names the team must create — never their values."""
        return json.loads(self.variable_names_json) if self.variable_names_json else []

    @property
    def secret_names(self) -> list[str]:
        """CI **secret** names the team must create — never their values."""
        return json.loads(self.secret_names_json) if self.secret_names_json else []


class CicdExportItem(SQLModel, table=True):
    """One test case shipped by one export — a receipt, not a work unit.

    Written only after the commit succeeds, which is why ``CICD_EXPORT_SPEC``
    carries no ``child_specs``: an export that fails part-way leaves no items
    to strand.

    ``case_title`` and ``requirement_name`` are **copied** rather than
    reached through the FK. Cases are archived wholesale on every plan
    revision and requirements are soft-deleted, so a receipt that joined for
    its text would start reading differently — or emptily — long after the
    PR it describes was merged.
    """

    __test__ = False  # this row is about a test case, not a test

    id: int | None = Field(default=None, primary_key=True)
    cicd_export_id: int = Field(foreign_key="cicdexport.id", index=True)
    test_case_id: int = Field(foreign_key="testcase.id", index=True)
    case_title: str
    requirement_name: str
    committed_path: str

    cicd_export: Optional["CicdExport"] = Relationship(back_populates="items")
