import logging
import os
import tempfile
import zipfile
from io import BytesIO

logger = logging.getLogger(__name__)

# ASCII-safe characters used when drawing the text tree.
_TREE_BRANCH = "+-- "
_TREE_LAST = "\\-- "
_TREE_PIPE = "|   "
_TREE_SPACE = "    "


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


def extract_and_list_tree(
    zip_bytes: bytes, max_files: int = 10_000, max_depth: int = 100
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

    with tempfile.TemporaryDirectory(prefix="qa_zip_") as tmpdir:
        tmpdir_real = os.path.realpath(tmpdir)

        with zipfile.ZipFile(BytesIO(zip_bytes)) as zf:
            entries = zf.infolist()
            if len(entries) > max_files:
                raise ValueError(
                    f"Zip archive contains {len(entries)} entries, "
                    f"which exceeds the limit of {max_files}."
                )

            for member in entries:
                target = os.path.realpath(os.path.join(tmpdir, member.filename))

                # --- Zip-slip protection ---
                if not target.startswith(tmpdir_real + os.sep) and target != tmpdir_real:
                    logger.warning(
                        "Rejected path-traversal entry in zip: %s → %s",
                        member.filename,
                        target,
                    )
                    continue

                # Create parent directories for zero-byte entries
                if member.is_dir():
                    os.makedirs(target, exist_ok=True)
                else:
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    with zf.open(member) as src, open(target, "wb") as dst:
                        dst.write(src.read())

        # Walk the extracted tree
        for dirpath, dirnames, filenames in os.walk(tmpdir):
            rel = os.path.relpath(dirpath, tmpdir)
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

    tree_text = _simple_tree_text([t.rstrip("/") for t in tree], max_depth=max_depth)
    return tree, tree_text
