"""Definition of database models"""

from datetime import datetime, timezone
from enum import Enum

from sqlmodel import Field, Relationship, SQLModel


class Repo(SQLModel, table=True):
    """A GitHub repository registered for QA analysis."""

    id: int | None = Field(default=None, primary_key=True)
    github_link: str
    github_token: str | None = Field(default=None)
    name: str
    description: str | None = Field(default=None)
    active: bool = Field(default=True)
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


class RequirementStatus(str, Enum):
    """Lifecycle status of a requirement's clarity analysis."""

    PENDING = "pending"
    ANALYZING = "analyzing"
    NEEDS_CLARIFICATION = "needs_clarification"
    READY = "ready"
    CONFIRMED = "confirmed"
    FAILED = "failed"


class Requirement(SQLModel, table=True):
    """A single requirement attached to a sprint, analyzed for QA clarity."""

    id: int | None = Field(default=None, primary_key=True)
    sprint_id: int = Field(foreign_key="sprint.id")
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
