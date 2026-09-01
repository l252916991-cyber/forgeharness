"""Integration-style tests for confined Coding Agent tools."""

import hashlib
import shutil
import sys
from pathlib import Path

import pytest

from forgeharness.coding.policy import CodingToolPolicy
from forgeharness.coding.tools import (
    GitDiffInput,
    GitDiffTool,
    ListFilesInput,
    ListFilesTool,
    ReadFileInput,
    ReadFileTool,
    RunTestsInput,
    RunTestsTool,
    SearchCodeInput,
    SearchCodeTool,
    WriteFileInput,
    WriteFileTool,
)
from forgeharness.domain.models import ToolCall
from forgeharness.tools.base import RiskLevel, ToolContext, ToolSpec
from forgeharness.tools.policy import PolicyDecisionType
from forgeharness.tools.process import run_command


def context(workspace: Path) -> ToolContext:
    return ToolContext(task_id="task", workspace=workspace)


async def test_list_read_and_search_tools(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    path = tmp_path / "src" / "service.py"
    path.write_text("def calculate():\n    return 41\n")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "secret").write_text("calculate")

    listing = await ListFilesTool().execute(ListFilesInput(), context(tmp_path))
    read = await ReadFileTool().execute(ReadFileInput(path="src/service.py"), context(tmp_path))
    search = await SearchCodeTool().execute(SearchCodeInput(query="calculate"), context(tmp_path))

    assert listing.content == "src/service.py"
    assert "1 | def calculate():" in read.content
    assert read.metadata["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert "src/service.py:1:5:def calculate():" in search.content
    assert ".git" not in search.content


async def test_read_tool_rejects_non_file_and_out_of_range(tmp_path: Path) -> None:
    (tmp_path / "folder").mkdir()
    path = tmp_path / "short.py"
    path.write_text("one\n")

    directory = await ReadFileTool().execute(ReadFileInput(path="folder"), context(tmp_path))
    beyond = await ReadFileTool().execute(
        ReadFileInput(path="short.py", start_line=5), context(tmp_path)
    )

    assert directory.ok is False
    assert beyond.content == "start_line is beyond end of file"


async def test_write_tool_requires_matching_hash_and_writes_atomically(tmp_path: Path) -> None:
    path = tmp_path / "value.txt"
    path.write_text("old")
    tool = WriteFileTool()

    missing = await tool.execute(WriteFileInput(path="value.txt", content="new"), context(tmp_path))
    stale = await tool.execute(
        WriteFileInput(path="value.txt", content="new", expected_sha256="0" * 64),
        context(tmp_path),
    )
    current_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    written = await tool.execute(
        WriteFileInput(path="value.txt", content="new", expected_sha256=current_hash),
        context(tmp_path),
    )

    assert missing.content == "existing file requires expected_sha256"
    assert stale.content == "file changed since it was read"
    assert written.ok is True
    assert path.read_text() == "new"


async def test_write_tool_creates_new_file_and_rejects_hash(tmp_path: Path) -> None:
    tool = WriteFileTool()
    rejected = await tool.execute(
        WriteFileInput(path="new.txt", content="new", expected_sha256="0" * 64),
        context(tmp_path),
    )
    created = await tool.execute(
        WriteFileInput(path="nested/new.txt", content="new"), context(tmp_path)
    )

    assert rejected.content == "new file must not set expected_sha256"
    assert created.ok is True
    assert (tmp_path / "nested" / "new.txt").read_text() == "new"


async def test_fixed_test_command_and_git_diff(tmp_path: Path) -> None:
    (tmp_path / "test_ok.py").write_text("def test_ok():\n    assert 1 + 1 == 2\n")
    tests = await RunTestsTool((sys.executable, "-m", "pytest", "-q")).execute(
        RunTestsInput(), context(tmp_path)
    )

    assert tests.ok is True
    assert "1 passed" in tests.content

    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    assert (await run_command(("git", "init", "-q"), workspace=tmp_path)).exit_code == 0
    assert (await run_command(("git", "add", "test_ok.py"), workspace=tmp_path)).exit_code == 0
    committed = await run_command(
        (
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            "base",
        ),
        workspace=tmp_path,
    )
    assert committed.exit_code == 0
    (tmp_path / "test_ok.py").write_text("def test_ok():\n    assert True\n")
    diff = await GitDiffTool().execute(GitDiffInput(), context(tmp_path))

    assert diff.ok is True
    assert "+    assert True" in diff.content


def test_coding_policy_allows_fixed_tests_but_denies_other_process() -> None:
    policy = CodingToolPolicy()
    run_tests = policy.evaluate(
        task_id="task",
        spec=RunTestsTool(("pytest",)).spec,
        call=ToolCall(id="one", name="run_tests"),
    )
    other = policy.evaluate(
        task_id="task",
        spec=ToolSpec(name="shell", description="shell", input_schema={}, risk=RiskLevel.PROCESS),
        call=ToolCall(id="two", name="shell"),
    )

    assert run_tests.type == PolicyDecisionType.ALLOW
    assert other.type == PolicyDecisionType.DENY
