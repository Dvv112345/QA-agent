"""Shared exploratory-testing helpers used by both routes and worker tasks.

Lives here rather than in the task module so the web process never imports
task code (the same reason tasks are enqueued by dotted string path), and
rather than in ``services/llm_prompts.py`` so that module stays free of
database imports.  Mirrors how ``utils/readme_utils.py`` is shared.
"""

from __future__ import annotations

from backend.models.database import ExploratoryRun
from backend.services.llm_prompts import ExploratorySessionLike, FindingLike


def session_sheets(run: ExploratoryRun) -> list[ExploratorySessionLike]:
    """Convert a run's session rows into plain data for the summary prompt."""
    return [
        ExploratorySessionLike(
            charter=session.charter,
            sfdipot_areas=session.sfdipot_areas,
            status=session.status,
            actions_used=session.actions_used,
            stop_reason=session.stop_reason,
            session_notes=session.session_notes,
            findings=[
                FindingLike(
                    finding_type=finding.finding_type,
                    severity=finding.severity,
                    title=finding.title,
                    expected=finding.expected,
                    actual=finding.actual,
                )
                for finding in session.findings
            ],
        )
        for session in run.sessions
    ]
