"""Tests for workspace confinement and Python symbol mapping."""

from pathlib import Path

import pytest

from forgeharness.context.repository import PythonRepositoryMap
from forgeharness.tools.paths import WorkspacePathError, resolve_workspace_path


def test_workspace_path_rejects_absolute_parent_and_symlink_escape(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    (workspace / "link").symlink_to(outside)

    with pytest.raises(WorkspacePathError, match="absolute"):
        resolve_workspace_path(workspace, str(outside))
    with pytest.raises(WorkspacePathError, match="escapes"):
        resolve_workspace_path(workspace, "../outside.txt")
    with pytest.raises(WorkspacePathError, match="escapes"):
        resolve_workspace_path(workspace, "link", must_exist=True)


def test_repository_map_extracts_symbols_and_reports_parse_errors(tmp_path: Path) -> None:
    (tmp_path / "valid.py").write_text(
        "class Service:\n    pass\n\ndef run():\n    return 1\n\nasync def fetch():\n    pass\n"
    )
    (tmp_path / "broken.py").write_text("def broken(:\n")

    result = PythonRepositoryMap().build(tmp_path)

    by_path = {item.path: item for item in result}
    assert [(symbol.kind, symbol.name) for symbol in by_path["valid.py"].symbols] == [
        ("class", "Service"),
        ("function", "run"),
        ("function", "fetch"),
    ]
    assert by_path["broken.py"].parse_error is not None
    assert by_path["broken.py"].parse_error.startswith("SyntaxError:")


def test_repository_map_limits_large_files_and_validates_limits(tmp_path: Path) -> None:
    (tmp_path / "large.py").write_text("x = 1\n")
    mapped = PythonRepositoryMap(max_file_bytes=1).build(tmp_path)

    assert mapped[0].parse_error == "file too large"
    with pytest.raises(ValueError, match="limits must be positive"):
        PythonRepositoryMap(max_files=0)
