"""Row → plain-shape conversions for the nonfunctional run.

The counterpart of ``exploratory_utils.session_sheets``, and here for the
same reason: the summary prompt takes plain dataclasses so ``services/llm``
stays free of database imports, and the route and the task both need the
identical conversion — one from a request, one from a worker.

``parse_json_object`` is the other half of that: ``metrics_json`` and
``results_json`` are written by this application and read back by it, but a
row written by an older version (or truncated by a crash) must render as an
empty panel rather than a 500 on a page that polls every 2.5 seconds.
"""

from __future__ import annotations

import json
import logging

from backend.models.database import NonfunctionalDomain, NonfunctionalRun
from backend.services.llm_prompts import LoadProfileLike, TargetLike

logger = logging.getLogger(__name__)


def parse_json_object(raw: str | None) -> dict:
    """Decode a stored JSON blob, answering ``{}`` for anything unusable."""
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("Stored JSON could not be parsed — rendering it as empty")
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _outcomes(target) -> dict[str, str | None]:
    """Per-domain outcome, keyed by domain name.

    ``None`` means the domain was not selected for this run — which is a
    different statement from ``clean``, and the one the summary prompt is
    explicitly told not to confuse with it.
    """
    return {
        NonfunctionalDomain.ACCESSIBILITY.value: target.a11y_outcome,
        NonfunctionalDomain.SECURITY.value: target.security_outcome,
        NonfunctionalDomain.PERFORMANCE.value: target.performance_outcome,
    }


def target_summaries(run: NonfunctionalRun) -> list[TargetLike]:
    """Every examined URL, in the shape the summary prompt reads."""
    return [
        TargetLike(
            url=target.url,
            kind=target.kind,
            status=target.status,
            outcomes=_outcomes(target),
            metrics=parse_json_object(target.metrics_json),
            finding_count=len(target.findings),
        )
        for target in run.targets
    ]


def load_profile_summaries(run: NonfunctionalRun) -> list[LoadProfileLike]:
    """Every applied profile, in the shape the summary prompt reads.

    ``body`` is deliberately absent from ``LoadProfileLike``: a body may
    carry a ``$NAME`` whose value is a credential, and the summary has
    nothing to say about it.
    """
    return [
        LoadProfileLike(
            url=profile.url,
            method=profile.method,
            status=profile.status,
            requests_sent=profile.requests_sent,
            results=parse_json_object(profile.results_json),
        )
        for profile in run.load_profiles
    ]
