"""RQ task functions.

This module is imported by ``QueueService.enqueue_word_count`` and resolved
by the RQ worker at execution time.  It must NOT import from
``backend.services.queue`` or ``backend.worker`` (no circular imports).
"""

from __future__ import annotations

import logging
import os
import time as _time

from rq import get_current_job

from backend.utils.word_utils import count_words_in_file, is_text_file

logger = logging.getLogger(__name__)


def count_words_task(
    job_id: str,
    md_path: str,
    zip_path: str,
    files: list[str],
    timeout: int,
) -> dict:
    """Count words in the markdown requirements and each extracted zip file.

    Processing order:
    1. Markdown file (always text, no binary check needed).
    2. Zip files in order, each checked for binary before counting.

    Progress is reported via ``job.meta["processed_files"]`` after each
    zip file is processed.

    Raises:
        FileNotFoundError: If any file in the *files* list is missing at
            processing time (marks the entire job as ``failed``).
        TimeoutError: If the cooperative timeout is exceeded.
    """
    job = get_current_job()
    start = _time.monotonic()

    # ── Step 1: Markdown ────────────────────────────────────────────────
    md_words = count_words_in_file(md_path)
    md_result = {"file": os.path.basename(md_path), "words": md_words}
    logger.info("[%s] Markdown: %d words", job_id, md_words)

    # ── Step 2: Zip files ───────────────────────────────────────────────
    zip_results: list[dict] = []
    processed = 0

    for rel_path in files:
        # Cooperative timeout check
        if _time.monotonic() - start > timeout:
            raise TimeoutError(
                f"Word-count job {job_id} timed out after {timeout}s "
                f"({processed}/{len(files)} files processed)"
            )

        full_path = os.path.join(zip_path, rel_path)

        if not os.path.exists(full_path):
            raise FileNotFoundError(f"File missing during processing: {full_path}")

        words = count_words_in_file(full_path) if is_text_file(full_path) else 0

        zip_results.append({"file": rel_path, "words": words})
        processed += 1

        if job:
            job.meta["processed_files"] = processed
            job.save_meta()

    total_words = md_words + sum(r["words"] for r in zip_results)

    return {
        "md_result": md_result,
        "zip_results": zip_results,
        "total_files": len(files),
        "processed_files": processed,
        "total_words": total_words,
    }
