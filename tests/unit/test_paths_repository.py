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


def test_repository_map_limit_counts_python_files_after_many_other_files(tmp_path: Path) -> None:
    # More than list_files' default 200 entries precede the Python sources.
    # A vertical must not silently reuse that listing limit as its Python limit.
    others = tmp_path / "a-other-files"
    others.mkdir()
    for index in range(275):
        (others / f"{index:03}.txt").write_text("not Python source")
    sources = tmp_path / "z-python"
    sources.mkdir()
    (sources / "first.py").write_text("def first_symbol(): pass\n")
    (sources / "second.py").write_text("def second_symbol(): pass\n")
    (sources / "third.py").write_text("def third_symbol(): pass\n")

    mapped = PythonRepositoryMap(max_files=2).build(tmp_path)

    assert [item.path for item in mapped] == ["z-python/first.py", "z-python/second.py"]
    assert [[symbol.name for symbol in item.symbols] for item in mapped] == [
        ["first_symbol"],
        ["second_symbol"],
    ]
