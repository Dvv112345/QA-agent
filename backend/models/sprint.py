"""Sprint database model — a named work unit linked to a Repo."""

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

from sqlmodel import Field, Relationship, SQLModel

if TYPE_CHECKING:
    from backend.models.repo import Repo


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

    repo: Optional["Repo"] = Relationship(back_populates="sprints")
