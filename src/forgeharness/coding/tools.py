"""Workspace-confined tools for repository exploration, editing, and verification.

Every tool reaches the workspace through a :class:`~forgeharness.sdk.FileBackend`
instead of calling ``Path`` directly. The behaviour is unchanged for the host case -- the
default factory is :func:`~forgeharness.sdk.local_backend` -- but a caller can now pass
:func:`~forgeharness.sdk.container_backend_factory` and have the same seven tools operate
inside a sandbox, with no change to the tools themselves.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from forgeharness.sdk import (
    BackendFactory,
    FileBackend,
    FileConflict,
    FileTooLarge,
    MessageSpill,
    NotRegularFile,
    ObservationCompressor,
    RiskLevel,
    SpillError,
    Tool,
    ToolContext,
    ToolEffectClass,
    ToolOutput,
    ToolSpec,
    WorkspacePathError,
    local_backend,
    observation_spill_dir,
)

# Directories the harness writes its own state into. They are not repository
# content, so a listing that showed them would make every run look dirty.
_HARNESS_STATE_DIRECTORIES = frozenset({".git", ".forgeharness"})


class StrictInput(BaseModel):
    """Reject model-supplied fields a tool does not understand."""

    model_config = ConfigDict(extra="forbid")


class _BackendTool:
    """Shared plumbing: resolve the call's workspace, then hand it to the factory.

    The workspace is only known per call, so tools hold a factory rather than a
    backend instance. ``local_backend`` is the default because it is exactly what these
    tools did before the boundary existed.
    """

    spec: ToolSpec
    input_model: type[BaseModel]

    def __init__(self, *, backend: BackendFactory | None = None) -> None:
        self._backend_factory = backend or local_backend

    def _backend(self, context: ToolContext) -> FileBackend:
        return self._backend_factory(context.require_workspace())

    def _metadata(self, backend: FileBackend, **extra: Any) -> dict[str, Any]:
        # Recording the backend name is what makes a trace able to answer "did this run
        # in the sandbox or on the host?", which is otherwise unanswerable after the fact.
        return {"backend": backend.name, **extra}


class ListFilesInput(StrictInput):
    """Arguments for bounded recursive file listing."""

    path: str = "."
    max_results: int = Field(default=200, ge=1, le=2_000)


class ListFilesTool(_BackendTool):
    """List regular files below a workspace-relative directory."""

    input_model = ListFilesInput
    spec = ToolSpec(
        requires_workspace=True,
        name="list_files",
        description="List repository files below a relative path.",
        input_schema=ListFilesInput.model_json_schema(),
        risk=RiskLevel.READ,
        effect_class=ToolEffectClass.READ_ONLY,
    )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolOutput:
        """Return a sorted, bounded repository-relative file list."""
        values = ListFilesInput.model_validate(arguments)
        backend = self._backend(context)
        try:
            entries = backend.list_files(
                path=values.path,
                limit=values.max_results + 1,
                skip_directories=_HARNESS_STATE_DIRECTORIES,
            )
        except NotRegularFile:
            return ToolOutput(ok=False, content=f"not a directory: {values.path}")
        selected = entries[: values.max_results]
        suffix = "\n[results truncated]" if len(entries) > len(selected) else ""
        return ToolOutput(
            ok=True,
            content="\n".join(entry.path for entry in selected) + suffix,
            metadata=self._metadata(backend, file_count=len(selected)),
        )


class ReadFileInput(StrictInput):
    """Arguments for a bounded line-oriented file read."""

    path: str
    start_line: int = Field(default=1, ge=1)
    end_line: int | None = Field(default=None, ge=1)


class ReadFileTool(_BackendTool):
    """Read a UTF-8 file with stable line numbers and size limits."""

    input_model = ReadFileInput
    spec = ToolSpec(
        requires_workspace=True,
        name="read_file",
        description="Read at most 400 lines from a workspace-relative UTF-8 file.",
        input_schema=ReadFileInput.model_json_schema(),
        risk=RiskLevel.READ,
        effect_class=ToolEffectClass.READ_ONLY,
    )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolOutput:
        """Read an explicit line interval without following escaping symlinks."""
        values = ReadFileInput.model_validate(arguments)
        backend = self._backend(context)
        try:
            result = backend.read_text(values.path, max_bytes=1_000_000)
        except FileTooLarge:
            return ToolOutput(ok=False, content="file exceeds 1 MB read limit")
        except NotRegularFile:
            return ToolOutput(ok=False, content=f"not a regular file: {values.path}")
        except UnicodeDecodeError:
            return ToolOutput(ok=False, content="file is not valid UTF-8")
        lines = result.text.splitlines()
        requested_end = values.end_line or min(len(lines), values.start_line + 399)
        end = min(requested_end, values.start_line + 399, len(lines))
        if end < values.start_line:
            return ToolOutput(ok=False, content="start_line is beyond end of file")
        content = "\n".join(
            f"{index:>6} | {lines[index - 1]}" for index in range(values.start_line, end + 1)
        )
        content += f"\n[sha256: {result.digest}]"
        return ToolOutput(
            ok=True,
            content=content,
            metadata=self._metadata(backend, sha256=result.digest),
        )


class SearchCodeInput(StrictInput):
    """Arguments for literal ripgrep search."""

    query: str = Field(min_length=1, max_length=500)
    path: str = "."
    max_output_bytes: int = Field(default=30_000, ge=1_000, le=100_000)


class SearchCodeTool(_BackendTool):
    """Search literal repository text through the backend's bounded file boundary."""

    input_model = SearchCodeInput
    spec = ToolSpec(
        requires_workspace=True,
        name="search_code",
        description="Search for a literal string and return file, line, column, and matching text.",
        input_schema=SearchCodeInput.model_json_schema(),
        risk=RiskLevel.READ,
        effect_class=ToolEffectClass.READ_ONLY,
    )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolOutput:
        """Execute literal search after validating its relative target path."""
        values = SearchCodeInput.model_validate(arguments)
        backend = self._backend(context)
        result = await backend.search(
            values.query, path=values.path, max_output_bytes=values.max_output_bytes
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
            metadata=self._metadata(
                backend, exit_code=result.exit_code, truncated=result.truncated
            ),
        )


class WriteFileInput(StrictInput):
    """Arguments for optimistic, atomic UTF-8 file replacement."""

    path: str
    content: str = Field(max_length=1_000_000)
    expected_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class WriteFileTool(_BackendTool):
    """Atomically create or replace one workspace file after external approval."""

    input_model = WriteFileInput
    spec = ToolSpec(
        requires_workspace=True,
        name="write_file",
        description=(
            "Create or atomically replace a UTF-8 file. Existing files require the SHA-256 "
            "returned by read_file."
        ),
        input_schema=WriteFileInput.model_json_schema(),
        risk=RiskLevel.WRITE,
        # Deliberately NOT idempotent: its optimistic expected_sha256 check makes a
        # repeat fail rather than duplicate, which is not idempotency under a key.
        effect_class=ToolEffectClass.NON_IDEMPOTENT,
    )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolOutput:
        """Apply an optimistic write without following symlink targets."""
        values = WriteFileInput.model_validate(arguments)
        backend = self._backend(context)
        try:
            result = backend.write_text(
                values.path, values.content, expected_digest=values.expected_sha256
            )
        except FileConflict as exc:
            # The backend owns the optimistic-lock contract, so its message is the
            # actionable one: it names which of the three preconditions failed.
            return ToolOutput(ok=False, content=str(exc))
        except WorkspacePathError as exc:
            return ToolOutput(ok=False, content=str(exc))
        except NotRegularFile:
            return ToolOutput(ok=False, content="target is not a regular file")
        return ToolOutput(
            ok=True,
            content=f"wrote {result.size} bytes to {values.path}",
            metadata=self._metadata(backend, sha256=result.digest, created=result.created),
        )


class RunTestsInput(StrictInput):
    """The test command is operator-configured, so the model supplies no arguments."""


class RunTestsTool(_BackendTool):
    """Run one fixed argv vector selected when the Coding Agent is composed."""

    input_model = RunTestsInput

    def __init__(self, command: tuple[str, ...], *, backend: BackendFactory | None = None) -> None:
        if not command:
            raise ValueError("test command must not be empty")
        self._command = command
        super().__init__(backend=backend)
        self.spec = ToolSpec(
            requires_workspace=True,
            name="run_tests",
            description=f"Run the configured test command: {command[0]} …",
            input_schema=RunTestsInput.model_json_schema(),
            risk=RiskLevel.PROCESS,
            # A command may have effects of its own, so it cannot be replayed.
            effect_class=ToolEffectClass.NON_IDEMPOTENT,
        )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolOutput:
        """Run the fixed command with a sanitized environment and bounded output."""
        RunTestsInput.model_validate(arguments)
        backend = self._backend(context)
        result = await backend.run(self._command)
        content = result.output
        if result.truncated:
            content += "\n[output truncated]"
        return ToolOutput(
            ok=result.exit_code == 0,
            content=content,
            metadata=self._metadata(
                backend, exit_code=result.exit_code, truncated=result.truncated
            ),
        )


class GitDiffInput(StrictInput):
    """Arguments for a bounded repository diff."""

    max_output_bytes: int = Field(default=50_000, ge=1_000, le=200_000)


class GitDiffTool(_BackendTool):
    """Return the uncommitted patch without invoking shell interpolation."""

    input_model = GitDiffInput
    spec = ToolSpec(
        requires_workspace=True,
        name="git_diff",
        description="Return the current Git working-tree diff.",
        input_schema=GitDiffInput.model_json_schema(),
        risk=RiskLevel.READ,
        effect_class=ToolEffectClass.READ_ONLY,
    )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolOutput:
        """Run a read-only Git diff command."""
        values = GitDiffInput.model_validate(arguments)
        backend = self._backend(context)
        result = await backend.run(
            ("git", "diff", "--no-ext-diff", "--"), max_output_bytes=values.max_output_bytes
        )
        content = result.output
        if result.truncated:
            content += "\n[output truncated]"
        return ToolOutput(
            ok=result.exit_code == 0,
            content=content,
            metadata=self._metadata(
                backend, exit_code=result.exit_code, truncated=result.truncated
            ),
        )


class ReadObservationInput(StrictInput):
    """Arguments for paging through a stored observation."""

    reference: str = Field(pattern=r"^obs://[0-9a-f]{24}$")
    offset: int = Field(default=0, ge=0)
    limit: int | None = Field(default=20_000, ge=1, le=200_000)


class ReadObservationTool:
    """Read back the original text behind a compressed observation's reference.

    Compression keeps the projection in context and the original on disk. Without a
    way back, the model has to re-run the tool whose output was compressed — paying
    for the work twice to recover a line the projection dropped. Paging is exposed
    because pulling a whole 400 KB log back would undo the compression that made room
    for it in the first place.

    This reads the harness's own spill directory rather than repository content, so it
    deliberately bypasses the file backend: routing it through a sandbox would be wrong,
    the spill is the harness's state, not the workspace's files.
    """

    input_model = ReadObservationInput
    spec = ToolSpec(
        requires_workspace=True,
        name="read_observation",
        description=(
            "Read part of an original tool observation that was compressed. "
            "Pass the obs:// reference shown in the compressed header."
        ),
        input_schema=ReadObservationInput.model_json_schema(),
        risk=RiskLevel.READ,
        effect_class=ToolEffectClass.READ_ONLY,
    )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolOutput:
        """Return one window of the stored original."""
        values = ReadObservationInput.model_validate(arguments)
        root = context.require_workspace().resolve(strict=True)
        compressor = ObservationCompressor(spill_dir=observation_spill_dir(root))
        try:
            content = compressor.read_spilled(
                values.reference, offset=values.offset, limit=values.limit
            )
        except (SpillError, ValueError) as exc:
            # A missing reference is the model's mistake to correct, so it is reported
            # as an observation rather than raised: raising would end the run.
            return ToolOutput(
                ok=False,
                content=f"cannot read {values.reference}: {exc}",
                metadata={"error_code": type(exc).__name__},
            )
        return ToolOutput(
            ok=True,
            content=content,
            metadata={
                "reference": values.reference,
                "offset": values.offset,
                "returned_chars": len(content),
            },
        )


class ReadCompactedContextInput(StrictInput):
    """Arguments for paging through the messages a compaction folded away."""

    reference: str = Field(pattern=r"^ctx://[0-9a-f]{24}$")
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=20, ge=1, le=200)


class ReadCompactedContextTool:
    """Read back the original messages behind a compaction reference.

    Folding old turns into a summary is the only way a long run fits, and the summary is
    a lossy projection: the line it drops can be the one that mattered. The observation
    compressor already answers this for tool output; without the same answer for message
    history, a model that lost a detail had to re-run the work that produced it. The
    reference in the summary header points at the stored originals, and this pages them.

    Windowed by message index rather than bytes because what a model needs back is "the
    turn where the user said what to build", not an arbitrary slice of JSON.
    """

    input_model = ReadCompactedContextInput
    spec = ToolSpec(
        requires_workspace=True,
        name="read_compacted_context",
        description=(
            "Read messages that context compaction folded into a summary. "
            "Pass the ctx:// reference shown in the compacted-context header."
        ),
        input_schema=ReadCompactedContextInput.model_json_schema(),
        risk=RiskLevel.READ,
        effect_class=ToolEffectClass.READ_ONLY,
    )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolOutput:
        """Return one window of the stored original messages."""
        values = ReadCompactedContextInput.model_validate(arguments)
        spill = MessageSpill(context.require_workspace())
        try:
            content = spill.read(values.reference, offset=values.offset, limit=values.limit)
        except (SpillError, ValueError) as exc:
            return ToolOutput(
                ok=False,
                content=f"cannot read {values.reference}: {exc}",
                metadata={"error_code": type(exc).__name__},
            )
        return ToolOutput(
            ok=True,
            content=content,
            metadata={
                "reference": values.reference,
                "offset": values.offset,
                "limit": values.limit,
            },
        )


def coding_tools(
    *, test_command: tuple[str, ...], backend: BackendFactory | None = None
) -> tuple[Tool, ...]:
    """Return the first audited set of native Coding Agent capabilities."""
    return (
        ListFilesTool(backend=backend),
        ReadFileTool(backend=backend),
        SearchCodeTool(backend=backend),
        WriteFileTool(backend=backend),
        RunTestsTool(test_command, backend=backend),
        GitDiffTool(backend=backend),
        ReadObservationTool(),
        ReadCompactedContextTool(),
    )


__all__ = [
    "GitDiffInput",
    "GitDiffTool",
    "ListFilesInput",
    "ListFilesTool",
    "ReadCompactedContextInput",
    "ReadCompactedContextTool",
    "ReadFileInput",
    "ReadFileTool",
    "ReadObservationInput",
    "ReadObservationTool",
    "RunTestsInput",
    "RunTestsTool",
    "SearchCodeInput",
    "SearchCodeTool",
    "StrictInput",
    "WriteFileInput",
    "WriteFileTool",
    "coding_tools",
]
