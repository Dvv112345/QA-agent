"""Definition of database models"""

from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from sqlmodel import Field, Relationship, SQLModel

from backend.config import MAX_CLARIFICATION_ROUNDS, MAX_TEST_ENV_REVISION_ROUNDS


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
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )

    repo: Repo | None = Relationship(back_populates="sprints")
    requirements: list["Requirement"] = Relationship(back_populates="sprint")
    test_environment: Optional["TestEnvironmentAccess"] = Relationship(
        back_populates="sprint", sa_relationship_kwargs={"uselist": False}
    )

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

    sprint: Sprint | None = Relationship(back_populates="requirements")

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
