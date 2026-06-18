"""Job status endpoint for polling word-count progress."""

from __future__ import annotations

from fastapi import APIRouter

from backend.models.types import FileWordCount, JobStatusResponse
from backend.services.queue import get_queue_service

router = APIRouter()


def _build_response(job_id: str, job_data: dict | None) -> JobStatusResponse:
    """Build a ``JobStatusResponse`` from raw Redis job data.

    When *job_data* is ``None`` the job is unknown (never enqueued or
    expired from Redis).
    """
    if job_data is None:
        return JobStatusResponse(job_id=job_id, status="unknown")

    status = job_data["status"]
    resp = JobStatusResponse(
        job_id=job_id,
        status=status,
        total_files=job_data.get("total_files", 0),
        processed_files=job_data.get("processed_files", 0),
        error=job_data.get("error"),
    )

    if status == "finished":
        result = job_data.get("result") or {}
        md = result.get("md_result")
        zip_items = result.get("zip_results")
        resp.md_result = FileWordCount(**md) if md else None
        resp.zip_results = [FileWordCount(**z) for z in zip_items] if zip_items else None
        resp.total_words = result.get("total_words")

    return resp


@router.get("/jobs/{job_id}/status", response_model=JobStatusResponse)
async def get_job_status(job_id: str):
    """Return the current status and progress of a word-count job.

    Poll this endpoint every 5 seconds after a successful upload.  The
    frontend computes the percentage from ``processed_files / total_files``.
    When ``status`` is ``"finished"`` the response includes ``md_result``,
    ``zip_results``, and ``total_words``.
    """
    job_data = get_queue_service().get_job_status(job_id)
    return _build_response(job_id, job_data)
