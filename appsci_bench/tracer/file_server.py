"""File serving for the tracer.

Serves files out of a run's **persisted** snapshots on the host filesystem
(`runs/<trace_id>/artifact/`, `runs/<trace_id>/agent_fs/`), with a traversal guard.

There is deliberately no live-container reader here any more. `get_file_from_container` /
`list_directory` / `sanitize_path` used to `docker exec` into a named container, and both routes that
called them were deleted: `sanitize_path` rejected every *absolute* path (it joined the leading "/"
back on as a path component, so `/workspace/x` normalised to `//workspace/x` and matched no allowed
prefix -- including the list route's own `/workspace` default), and the greedy `{container_name:path}`
route shadowed `/api/files/{container}/list` so the list route was unreachable at all. Nothing in the
dashboard called either. They were dead code that put a `docker exec` surface on an always-on server
that is now mounted same-origin with the experiments dashboard, so they are gone rather than fixed --
the snapshot readers below cover the same need without a container.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path, PurePosixPath

logger = logging.getLogger(__name__)


class FileServerError(Exception):
    """Error serving a file from a run's snapshot."""

    pass


def get_content_type(path: str) -> str:
    """Determine content type based on file extension.

    Args:
        path: File path.

    Returns:
        MIME content type.
    """
    extension = PurePosixPath(path).suffix.lower()

    content_types = {
        ".py": "text/x-python",
        ".js": "text/javascript",
        ".ts": "text/typescript",
        ".json": "application/json",
        ".yaml": "text/yaml",
        ".yml": "text/yaml",
        ".md": "text/markdown",
        ".txt": "text/plain",
        ".sh": "text/x-shellscript",
        ".bash": "text/x-shellscript",
        ".html": "text/html",
        ".css": "text/css",
        ".xml": "application/xml",
        ".sql": "text/x-sql",
        ".dockerfile": "text/x-dockerfile",
        ".toml": "text/x-toml",
        ".ini": "text/x-ini",
        ".cfg": "text/x-ini",
        ".conf": "text/plain",
        ".log": "text/plain",
    }

    # Handle Dockerfile without extension
    if PurePosixPath(path).name.lower() == "dockerfile":
        return "text/x-dockerfile"

    return content_types.get(extension, "text/plain")


# ---------------------------------------------------------------------------
# Persisted-artifact readers (host filesystem, no container required)
#
# The orchestrator snapshots the built artifact to runs/<trace_id>/artifact/
# via `docker cp` before tearing the container down. These helpers serve that
# durable copy, so the tracer can browse it long after the run's containers are
# gone -- no `docker exec`, no live container.
# ---------------------------------------------------------------------------

_MAX_ARTIFACT_FILE_SIZE = 1024 * 1024  # 1 MiB


def _resolve_within(base: Path, rel_path: str) -> Path:
    """Resolve ``rel_path`` under ``base``, refusing to escape it (traversal guard).

    The NUL check is not decoration. `?path=a%00b` is a perfectly transmissible query string, and
    every `os` call that takes a path raises a bare `ValueError("embedded null character")` on one --
    which is not a `FileServerError`, so it escaped the route's `except` and came back as an
    unhandled HTTP 500 with a traceback in the server log. Reproduced on both
    `/api/artifact/{id}/file` and `/api/agent_fs/{id}/file`. Checked before `resolve()` because
    `resolve()` is itself one of the calls that raises.
    """
    if "\x00" in rel_path:
        raise FileServerError("Path contains a NUL byte")
    base = base.resolve()
    target = (base / rel_path.lstrip("/")).resolve()
    if target != base and base not in target.parents:
        raise FileServerError(f"Path escapes artifact directory: {rel_path}")
    return target


def list_artifact_files(artifact_dir: Path | str) -> list[dict[str, object]]:
    """List every file in a persisted artifact snapshot (recursive, sorted).

    Symlinks are skipped rather than stat'd. A bare `stat()` FOLLOWS the link, so a link planted in
    the snapshot reported its *target's* size -- measured: a link to a 98,765-byte file outside the
    snapshot listed as `size: 98765`, i.e. a size oracle for arbitrary host paths on a server that
    is now mounted same-origin with the experiments dashboard. Skipping rather than
    `stat(follow_symlinks=False)` because a listed link is not readable either: `read_artifact_file`
    resolves before comparing, so a link out of the snapshot is refused, and a link *within* it
    points at a file `os.walk` already lists in its own right. Advertising an entry that cannot be
    opened is worse than omitting it.
    """
    artifact_dir = Path(artifact_dir)
    if not artifact_dir.is_dir():
        return []
    entries: list[dict[str, object]] = []
    for dirpath, _dirnames, filenames in os.walk(artifact_dir):
        for name in filenames:
            full = Path(dirpath) / name
            try:
                if full.is_symlink():
                    continue
                size = full.stat().st_size
            except OSError:
                continue
            entries.append({"path": str(full.relative_to(artifact_dir)), "size": size})
    entries.sort(key=lambda e: e["path"])
    return entries


def read_artifact_file(artifact_dir: Path | str, rel_path: str) -> tuple[str, str]:
    """Read one file from a persisted artifact snapshot. Returns (content, content_type)."""
    artifact_dir = Path(artifact_dir)
    target = _resolve_within(artifact_dir, rel_path)
    if not target.is_file():
        raise FileServerError(f"File not found: {rel_path}")
    if target.stat().st_size > _MAX_ARTIFACT_FILE_SIZE:
        raise FileServerError(f"File too large: {rel_path}")
    content = target.read_text(encoding="utf-8", errors="replace")
    return content, get_content_type(str(target))
