"""Reset the development database: drop all tables and recreate them empty.

Optionally also wipes the offline README storage directory.

Usage::

    python -m backend.reset_db                  # asks for confirmation
    python -m backend.reset_db --yes            # skip the prompt
    python -m backend.reset_db --yes --with-storage
"""

from __future__ import annotations

import argparse
import logging
import shutil
from pathlib import Path

from sqlalchemy.engine import make_url
from sqlmodel import SQLModel, create_engine

import backend.models.database  # noqa: F401  (registers all tables on SQLModel.metadata)
from backend.config import DATABASE_URL, STORAGE_LOCATION

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)-8s] reset_db: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger(__name__)


def reset_database() -> None:
    """Drop all registered tables and recreate them empty."""
    engine = create_engine(DATABASE_URL, echo=False)
    try:
        log.info("Dropping all tables ...")
        SQLModel.metadata.drop_all(engine)
        log.info("Recreating tables ...")
        SQLModel.metadata.create_all(engine)
    finally:
        engine.dispose()
    log.info("Database reset complete.")


def clear_storage() -> None:
    """Delete everything inside STORAGE_LOCATION (stored sprint READMEs)."""
    storage = Path(STORAGE_LOCATION)
    if not storage.is_dir():
        log.info("Storage directory %s does not exist — nothing to clear.", storage)
        return
    entries = list(storage.iterdir())
    for entry in entries:
        if entry.is_dir():
            shutil.rmtree(entry)
        else:
            entry.unlink()
    log.info("Removed %d item(s) from %s.", len(entries), storage)


def cli() -> None:
    parser = argparse.ArgumentParser(
        description="Drop and recreate all database tables (destructive)."
    )
    parser.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="skip the confirmation prompt",
    )
    parser.add_argument(
        "--with-storage",
        action="store_true",
        help=f"also wipe the offline README storage directory ({STORAGE_LOCATION})",
    )
    args = parser.parse_args()

    log.info(
        "Target database: %s",
        make_url(DATABASE_URL).render_as_string(hide_password=True),
    )

    if not args.yes:
        answer = input("This permanently deletes ALL data. Type 'yes' to continue: ")
        if answer.strip().lower() != "yes":
            log.info("Aborted.")
            return

    reset_database()
    if args.with_storage:
        clear_storage()


if __name__ == "__main__":
    cli()
