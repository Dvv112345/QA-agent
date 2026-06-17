import logging
import os
import tempfile
import zipfile
from io import BytesIO
from pathlib import Path

logger = logging.getLogger(__name__)

# ASCII-safe characters used when drawing the text tree.
_TREE_BRANCH = "+-- "
_TREE_LAST = "\\-- "
_TREE_PIPE = "|   "
_TREE_SPACE = "    "


# Set of folders to be ignored
_IGNORE_FOLDERS = {"node_modules", "__pycache__"}


def _simple_tree_text(entries: list[str], max_depth: int = 100) -> str:
    """Build a human-readable indented tree from a sorted list of relative paths.

    Raises ``ValueError`` if the directory nesting exceeds *max_depth*.
    """
    if not entries:
        return ""

    # Build a nested dict structure first
    root: dict[str, dict] = {}
    for entry in sorted(entries):
        parts = entry.replace("\\", "/").split("/")
        node = root
        for part in parts:
            if part not in node:
                node[part] = {}
            node = node[part]

    def render(node: dict, is_last: list[bool], depth: int = 0) -> list[str]:
        if depth > max_depth:
            raise ValueError(
                f"Maximum tree depth ({max_depth}) exceeded. "
                "The archive may contain excessively nested directories."
            )
        lines: list[str] = []
        keys = list(node.keys())
        for i, key in enumerate(keys):
            last = i == len(keys) - 1
            connector = _TREE_LAST if last else _TREE_BRANCH
            indent = ""
            for was_last in is_last:
                indent += _TREE_SPACE if was_last else _TREE_PIPE
            lines.append(f"{indent}{connector}{key}")
            if node[key]:
                lines.extend(render(node[key], is_last + [last], depth + 1))
        return lines

    return "\n".join(render(root, [], 0))


def extract_zip(
    zip_bytes: bytes,
    target_dir: str,
    max_files: int,
    max_depth: int,
    chunk_size: int,
):
    """
    Extract a zip archive safely with comprehensive protections.

    Returns:
        tuple: (extracted_files, rejected_entries)
            - extracted_files: list of relative paths of extracted files
            - rejected_entries: list of (filename, reason) tuples

    Raises:
        ValueError: If archive exceeds limits or is invalid
        TimeoutError: If extraction takes too long
    """

    # Normalize target directory
    target_path = Path(target_dir).resolve()
    target_path.mkdir(parents=True, exist_ok=True)
    target_str = str(target_path)

    extracted_files = []
    total_bytes = 0

    try:
        with zipfile.ZipFile(BytesIO(zip_bytes)) as zf:
            entries = zf.infolist()

            # Check file count
            if len(entries) > max_files:
                raise ValueError(
                    f"Archive has {len(entries)} entries, exceeds limit of {max_files}"
                )

            for member in entries:
                member_path = Path(member.filename)

                # ── Security: absolute path detection ──────────────────
                if member_path.is_absolute():
                    raise ValueError(f"Archive contain file with absolute path: {member.filename}")

                # ── Security: path traversal detection ─────────────────
                target = (target_path / member_path).resolve()
                if not str(target).startswith(target_str + os.sep) or target == target_path:
                    raise ValueError(
                        f"Archive contain file that attempt path traversal: {member.filename}"
                    )

                # ── Depth check ────────────────────────────────────────
                depth = len(member_path.parts)
                if depth > max_depth:
                    raise ValueError(
                        f"Archive contain file with depth of {depth}, exceeds limit of {max_depth}"
                    )

                # ── Filter hidden / ignored directories ────────────────
                should_ignore = False
                for part in member_path.parts:
                    if part.startswith(".") or part in _IGNORE_FOLDERS:
                        should_ignore = True
                        break

                if should_ignore:
                    continue

                # Extract file or create directory
                if member.is_dir():
                    # Create directory
                    target.mkdir(parents=True, exist_ok=True)
                    extracted_files.append(member.filename)
                else:
                    # Create parent directories
                    target.parent.mkdir(parents=True, exist_ok=True)

                    # Extract with streaming to avoid memory issues
                    with zf.open(member) as src, open(target, "wb") as dst:
                        while chunk := src.read(chunk_size):
                            dst.write(chunk)
                            total_bytes += len(chunk)

                    extracted_files.append(member.filename)
                    logger.debug(f"Extracted: {member.filename} ({member.file_size:,} bytes)")

        # Log summary
        logger.info(
            f"Extraction successful: {len(extracted_files)} files, "
            f"{total_bytes / (1024 * 1024):.2f} MB"
        )

        return extracted_files

    except Exception as e:
        # Clean up on error
        logger.error(f"Error during extraction, cleaning up: {e}")
        _cleanup_extraction(target_path)
        raise


def _cleanup_extraction(target_path: Path) -> None:
    """
    Remove all contents of the extraction directory.
    Uses pathlib for safe path handling.
    """
    try:
        if not target_path.exists():
            return

        for item in target_path.iterdir():
            try:
                if item.is_file() or item.is_symlink():
                    item.unlink()
                elif item.is_dir():
                    # Recursively remove directory contents
                    import shutil

                    shutil.rmtree(item)
            except Exception as e:
                logger.warning(f"Failed to remove {item}: {e}")

        logger.debug(f"Cleaned up: {target_path}")

    except Exception as e:
        logger.warning(f"Cleanup failed for {target_path}: {e}")


def get_tree(root_dir: str, tree: list[str]):
    # Walk the extracted tree
    for dirpath, dirnames, filenames in os.walk(root_dir):
        rel = os.path.relpath(dirpath, root_dir)
        if rel == ".":
            for fn in sorted(filenames):
                tree.append(fn)
            for dn in sorted(dirnames):
                tree.append(f"{dn}/")
        else:
            for fn in sorted(filenames):
                tree.append(f"{rel.replace(os.sep, '/')}/{fn}")
            for dn in sorted(dirnames):
                tree.append(f"{rel.replace(os.sep, '/')}/{dn}/")


def extract_and_list_tree(
    zip_bytes: bytes, stored_path: str | None, max_files: int, max_depth: int, chunk_size: int
) -> tuple[list[str], str]:
    """Extract a zip archive to a temp directory and return its directory tree.

    Returns a tuple of ``(tree_list, tree_text)`` where *tree_list* is a
    sorted list of relative paths and *tree_text* is a human-readable
    indented tree string.

    The zip is extracted to a temporary directory that is cleaned up
    immediately after the tree is built (callers that need the extracted
    files should use a different path).

    Raises ``ValueError`` if the archive contains more than *max_files*
    entries or if the directory nesting exceeds *max_depth*.
    """
    tree: list[str] = []

    if not stored_path:
        with tempfile.TemporaryDirectory(prefix="qa_zip_") as tmpdir:
            extract_zip(zip_bytes, tmpdir, max_files, max_depth, chunk_size)
            get_tree(tmpdir, tree)
    else:
        get_tree(stored_path, tree)

    tree_text = _simple_tree_text([t.rstrip("/") for t in tree], max_depth=max_depth)
    return tree, tree_text
