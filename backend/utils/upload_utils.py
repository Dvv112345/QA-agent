"""Bounded reads for multipart uploads."""

from __future__ import annotations

from fastapi import HTTPException, UploadFile

from backend.config import MAX_UPLOAD_SIZE_MB


def read_upload_capped(upload: UploadFile, *, label: str) -> bytes:
    """Read an upload, rejecting anything over ``MAX_UPLOAD_SIZE_MB``.

    Reads one byte past the cap so an oversized body is rejected without
    ever materialising more than the cap in memory — the whole point of
    the helper, and the detail each call site used to restate.

    ``label`` names the file in the error ("README file", "PRD file").
    """
    max_bytes = MAX_UPLOAD_SIZE_MB * 1024 * 1024
    content = upload.file.read(max_bytes + 1)
    if len(content) > max_bytes:
        raise HTTPException(
            status_code=422,
            detail=f"{label} exceeds the {MAX_UPLOAD_SIZE_MB} MB upload limit.",
        )
    return content
