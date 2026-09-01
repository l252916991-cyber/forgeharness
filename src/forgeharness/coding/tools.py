"""Workspace-confined tools for repository exploration, editing, and verification."""

from __future__ import annotations

import asyncio
import hashlib
import os
import stat
import tempfile
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from forgeharness.tools.base import RiskLevel, Tool, ToolContext, ToolOutput, ToolSpec
from forgeharness.tools.paths import resolve_workspace_path
from forgeharness.tools.process import run_command


class StrictInput(BaseModel):
    """Reject model-supplied fields a tool does not understand."""

    model_config = ConfigDict(extra="forbid")


class ListFilesInput(StrictInput):
    """Arguments for bounded recursive file listing."""

    path: str = "."
    max_results: int = Field(default=200, ge=1, le=2_000)


class ListFilesTool:
    """List regular files below a workspace-relative directory."""

    input_model = ListFilesInput
    spec = ToolSpec(
        name="list_files",
        description="List repository files below a relative path.",
        input_schema=ListFilesInput.model_json_schema(),
        risk=RiskLevel.READ,
    )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolOutput:
        """Return a sorted, bounded repository-relative file list."""
        values = ListFilesInput.model_validate(arguments)
        root = context.workspace.resolve(strict=True)
        target = resolve_workspace_path(root, values.path, must_exist=True)
        if not target.is_dir():
            return ToolOutput(ok=False, content=f"not a directory: {values.path}")
        paths = sorted(
            str(path.relative_to(root))
            for path in target.rglob("*")
            if path.is_file() and not path.is_symlink() and ".git" not in path.parts
        )
        selected = paths[: values.max_results]
        suffix = "\n[results truncated]" if len(paths) > len(selected) else ""
        return ToolOutput(ok=True, content="\n".join(selected) + suffix)


class ReadFileInput(StrictInput):
    """Arguments for a bounded line-oriented file read."""

    path: str
    start_line: int = Field(default=1, ge=1)
    end_line: int | None = Field(default=None, ge=1)


class ReadFileTool:
    """Read a UTF-8 file with stable line numbers and size limits."""

    input_model = ReadFileInput
    spec = ToolSpec(
        name="read_file",
        description="Read at most 400 lines from a workspace-relative UTF-8 file.",
        input_schema=ReadFileInput.model_json_schema(),
        risk=RiskLevel.READ,
    )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolOutput:
        """Read an explicit line interval without following escaping symlinks."""
        values = ReadFileInput.model_validate(arguments)
        path = resolve_workspace_path(context.workspace, values.path, must_exist=True)
        if not path.is_file():
            return ToolOutput(ok=False, content=f"not a regular file: {values.path}")
        if path.stat().st_size > 1_000_000:
            return ToolOutput(ok=False, content="file exceeds 1 MB read limit")
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            return ToolOutput(ok=False, content="file is not valid UTF-8")
        requested_end = values.end_line or min(len(lines), values.start_line + 399)
        end = min(requested_end, values.start_line + 399, len(lines))
        if end < values.start_line:
            return ToolOutput(ok=False, content="start_line is beyond end of file")
        content = "\n".join(
            f"{index:>6} | {lines[index - 1]}" for index in range(values.start_line, end + 1)
        )
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        content += f"\n[sha256: {digest}]"
        return ToolOutput(
            ok=True,
            content=content,
            metadata={"sha256": digest},
        )


class SearchCodeInput(StrictInput):
    """Arguments for literal ripgrep search."""

    query: str = Field(min_length=1, max_length=500)
    path: str = "."
    max_output_bytes: int = Field(default=30_000, ge=1_000, le=100_000)


class SearchCodeTool:
    """Search repository text with a fixed, non-shell ripgrep command."""

    input_model = SearchCodeInput
    spec = ToolSpec(
        name="search_code",
        description="Search for a literal string and return file, line, column, and matching text.",
        input_schema=SearchCodeInput.model_json_schema(),
        risk=RiskLevel.READ,
    )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolOutput:
        """Execute literal search after validating its relative target path."""
        values = SearchCodeInput.model_validate(arguments)
        target = resolve_workspace_path(context.workspace, values.path, must_exist=True)
        relative = str(target.relative_to(context.workspace.resolve(strict=True))) or "."
        result = await run_command(
            (
                "rg",
                "--line-number",
                "--column",
                "--no-heading",
                "--color=never",
                "--fixed-strings",
                "--glob",
                "!.git/**",
                "--",
                values.query,
                relative,
            ),
            workspace=context.workspace,
            max_output_bytes=values.max_output_bytes,
        )
        ok = result.exit_code in (0, 1)
        content = result.output
        if result.exit_code == 1:
            content = "no matches"
        if result.truncated:
            content += "\n[output truncated]"
        return ToolOutput(
            ok=ok,
            content=content,
            metadata={"exit_code": result.exit_code, "truncated": result.truncated},
        )


class WriteFileInput(StrictInput):
    """Arguments for optimistic, atomic UTF-8 file replacement."""

    path: str
    content: str = Field(max_length=1_000_000)
    expected_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class WriteFileTool:
    """Atomically create or replace one workspace file after external approval."""

    input_model = WriteFileInput
    spec = ToolSpec(
        name="write_file",
        description=(
            "Create or atomically replace a UTF-8 file. Existing files require the SHA-256 "
            "returned by read_file."
        ),
        input_schema=WriteFileInput.model_json_schema(),
        risk=RiskLevel.WRITE,
    )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolOutput:
        """Apply an optimistic write without following symlink targets."""
        values = WriteFileInput.model_validate(arguments)
        path = resolve_workspace_path(context.workspace, values.path)
        if path.exists() and path.is_symlink():
            return ToolOutput(ok=False, content="refusing to replace a symlink")
        if path.exists():
            if not path.is_file():
                return ToolOutput(ok=False, content="target is not a regular file")
            current_hash = hashlib.sha256(path.read_bytes()).hexdigest()
            if values.expected_sha256 is None:
                return ToolOutput(ok=False, content="existing file requires expected_sha256")
            if current_hash != values.expected_sha256:
                return ToolOutput(ok=False, content="file changed since it was read")
            mode = stat.S_IMODE(path.stat().st_mode)
        else:
            if values.expected_sha256 is not None:
                return ToolOutput(ok=False, content="new file must not set expected_sha256")
            path.parent.mkdir(parents=True, exist_ok=True)
            mode = 0o644
        encoded = values.content.encode("utf-8")
        await asyncio.to_thread(_atomic_write, path, encoded, mode)
        return ToolOutput(
            ok=True,
            content=f"wrote {len(encoded)} bytes to {values.path}",
            metadata={"sha256": hashlib.sha256(encoded).hexdigest()},
        )


class RunTestsInput(StrictInput):
    """The test command is operator-configured, so the model supplies no arguments."""


class RunTestsTool:
    """Run one fixed argv vector selected when the Coding Agent is composed."""

    input_model = RunTestsInput

    def __init__(self, command: tuple[str, ...]) -> None:
        if not command:
            raise ValueError("test command must not be empty")
        self._command = command
        self.spec = ToolSpec(
            name="run_tests",
            description=f"Run the configured test command: {command[0]} …",
            input_schema=RunTestsInput.model_json_schema(),
            risk=RiskLevel.PROCESS,
        )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolOutput:
        """Run the fixed command with a sanitized environment and bounded output."""
        RunTestsInput.model_validate(arguments)
        result = await run_command(self._command, workspace=context.workspace)
        content = result.output
        if result.truncated:
            content += "\n[output truncated]"
        return ToolOutput(
            ok=result.exit_code == 0,
            content=content,
            metadata={"exit_code": result.exit_code, "truncated": result.truncated},
        )


class GitDiffInput(StrictInput):
    """Arguments for a bounded repository diff."""

    max_output_bytes: int = Field(default=50_000, ge=1_000, le=200_000)


class GitDiffTool:
    """Return the uncommitted patch without invoking shell interpolation."""

    input_model = GitDiffInput
    spec = ToolSpec(
        name="git_diff",
        description="Return the current Git working-tree diff.",
        input_schema=GitDiffInput.model_json_schema(),
        risk=RiskLevel.READ,
    )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolOutput:
        """Run a read-only Git diff command."""
        values = GitDiffInput.model_validate(arguments)
        result = await run_command(
            ("git", "diff", "--no-ext-diff", "--"),
            workspace=context.workspace,
            max_output_bytes=values.max_output_bytes,
        )
        content = result.output
        if result.truncated:
            content += "\n[output truncated]"
        return ToolOutput(
            ok=result.exit_code == 0,
            content=content,
            metadata={"exit_code": result.exit_code, "truncated": result.truncated},
        )


def coding_tools(*, test_command: tuple[str, ...]) -> tuple[Tool, ...]:
    """Return the first audited set of native Coding Agent capabilities."""
    return (
        ListFilesTool(),
        ReadFileTool(),
        SearchCodeTool(),
        WriteFileTool(),
        RunTestsTool(test_command),
        GitDiffTool(),
    )


def _atomic_write(path: Path, content: bytes, mode: int) -> None:
    """Synchronously fsync and replace one file from a worker thread."""
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temporary:
        temporary.write(content)
        temporary.flush()
        os.fsync(temporary.fileno())
        temporary_path = Path(temporary.name)
    try:
        temporary_path.chmod(mode)
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)
