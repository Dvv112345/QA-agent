"""Sprint utility helpers — directory generation, validation, etc."""

import logging
import os
import uuid

from sqlmodel import Session, select

from backend.models.sprint import Sprint

logger = logging.getLogger(__name__)


def generate_sprint_directory(session: Session, storage_location: str) -> tuple[str, str]:
    """Generate a unique directory name and create it atomically on disk.

    Checks both the database and the filesystem to guarantee uniqueness.
    Returns ``(directory_name, full_path)``.

    The directory is created inside the loop via ``os.makedirs`` with
    ``exist_ok=False`` to eliminate the TOCTOU race between checking
    and creating.
    """
    while True:
        directory = uuid.uuid4().hex
        existing = session.exec(select(Sprint).where(Sprint.directory == directory)).first()
        if existing is not None:
            continue
        dir_path = os.path.join(storage_location, directory)
        try:
            os.makedirs(dir_path, exist_ok=False)
        except FileExistsError:
            continue
        return directory, dir_path
