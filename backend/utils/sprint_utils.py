"""Sprint utility helpers — directory generation, validation, etc."""

import logging
import os
import uuid

logger = logging.getLogger(__name__)


def generate_sprint_directory(storage_location: str) -> tuple[str, str]:
    """Generate a unique directory name and create it atomically on disk.

    Returns ``(directory_name, full_path)``.

    ``os.makedirs`` with ``exist_ok=False`` is the whole uniqueness check:
    it is atomic, so there is no TOCTOU window, and a name free on disk is
    free in the database too — every directory this returns is created
    here.  A prior ``SELECT`` guarded the same collision one step less
    reliably and cost a query per sprint creation.
    """
    while True:
        directory = uuid.uuid4().hex
        dir_path = os.path.join(storage_location, directory)
        try:
            os.makedirs(dir_path, exist_ok=False)
        except FileExistsError:
            continue
        return directory, dir_path
