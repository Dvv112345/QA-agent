"""Tests for backend/routes/jobs.py — GET /api/jobs/{job_id}/status."""

from unittest.mock import patch

import pytest


class TestJobStatusEndpoint:
    """Tests for ``GET /api/jobs/{job_id}/status``."""

    @pytest.mark.asyncio
    async def test_unknown_job_returns_status_unknown(self, async_client):
        """When Redis has no record of a job, return status 'unknown'."""
        with patch("backend.routes.jobs.queue_service.get_job_status", return_value=None):
            response = await async_client.get("/api/jobs/fake-job/status")
            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "unknown"
            assert data["total_files"] == 0
            assert data["processed_files"] == 0

    @pytest.mark.asyncio
    async def test_queued_job_returns_progress(self, async_client):
        """A queued job should return total_files from meta."""
        job_data = {
            "status": "queued",
            "total_files": 10,
            "processed_files": 0,
            "error": None,
            "result": None,
        }
        with patch("backend.routes.jobs.queue_service.get_job_status", return_value=job_data):
            response = await async_client.get("/api/jobs/job-1/status")
            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "queued"
            assert data["total_files"] == 10
            assert data["processed_files"] == 0

    @pytest.mark.asyncio
    async def test_finished_job_returns_results(self, async_client):
        """When finished, the response includes md_result, zip_results, total_words."""
        job_data = {
            "status": "finished",
            "total_files": 2,
            "processed_files": 2,
            "error": None,
            "result": {
                "md_result": {"file": "requirements.md", "words": 42},
                "zip_results": [
                    {"file": "main.py", "words": 10},
                    {"file": "utils.py", "words": 5},
                ],
                "total_files": 2,
                "processed_files": 2,
                "total_words": 57,
            },
        }
        with patch("backend.routes.jobs.queue_service.get_job_status", return_value=job_data):
            response = await async_client.get("/api/jobs/job-done/status")
            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "finished"
            assert data["md_result"] == {"file": "requirements.md", "words": 42}
            assert len(data["zip_results"]) == 2
            assert data["total_words"] == 57

    @pytest.mark.asyncio
    async def test_failed_job_returns_error(self, async_client):
        """A failed job returns the error string."""
        job_data = {
            "status": "failed",
            "total_files": 5,
            "processed_files": 2,
            "error": "File missing during processing: /tmp/zip/missing.py",
            "result": None,
        }
        with patch("backend.routes.jobs.queue_service.get_job_status", return_value=job_data):
            response = await async_client.get("/api/jobs/job-fail/status")
            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "failed"
            assert "missing.py" in data["error"]

    @pytest.mark.asyncio
    async def test_started_job_shows_intermediate_progress(self, async_client):
        """While started, progress shows processed_files out of total_files."""
        job_data = {
            "status": "started",
            "total_files": 20,
            "processed_files": 7,
            "error": None,
            "result": None,
        }
        with patch("backend.routes.jobs.queue_service.get_job_status", return_value=job_data):
            response = await async_client.get("/api/jobs/job-mid/status")
            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "started"
            assert data["total_files"] == 20
            assert data["processed_files"] == 7
