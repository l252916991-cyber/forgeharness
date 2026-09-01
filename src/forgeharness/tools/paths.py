"""Workspace path normalization shared by filesystem tools."""

from __future__ import annotations

from pathlib import Path


class WorkspacePathError(ValueError):
    """A requested path escapes or ambiguously addresses the task workspace."""


def resolve_workspace_path(workspace: Path, relative: str, *, must_exist: bool = False) -> Path:
    """Resolve a relative path and prove that its target remains under the workspace."""
    requested = Path(relative)
    if requested.is_absolute():
        raise WorkspacePathError("absolute paths are not allowed")
    root = workspace.resolve(strict=True)
    candidate = (root / requested).resolve(strict=must_exist)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise WorkspacePathError("path escapes the task workspace") from exc
    return candidate
