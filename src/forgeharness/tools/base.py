"""Tool contracts shared by native and protocol adapters."""

from __future__ import annotations

import json
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from forgeharness.domain.models import BusinessReceipt, FrozenModel, RunContext, Usage


class RiskLevel(StrEnum):
    """Coarse side-effect level used as policy input."""

    READ = "read"
    WRITE = "write"
    PROCESS = "process"
    NETWORK = "network"


class ToolEffectClass(StrEnum):
    """What a tool's side effect permits during recovery.

    Recovery must never guess whether a side effect happened, so the default is the
    strictest class: a tool has to *earn* a weaker guarantee by declaring it, and a
    tool that ignores the idempotency key must not claim to be idempotent.
    """

    # No external side effect; safe to execute again.
    READ_ONLY = "read_only"
    # Has a side effect, but the implementation genuinely honours a stable
    # idempotency key, so repeating with the same key produces no second effect.
    IDEMPOTENT = "idempotent"
    # Repeated execution cannot be shown to be safe. The default.
    NON_IDEMPOTENT = "non_idempotent"


class SchemaSource(StrEnum):
    """Authority used to validate a tool's advertised input schema."""

    PYDANTIC = "pydantic"
    EXTERNAL_JSON_SCHEMA = "external_json_schema"


class ToolSpec(FrozenModel):
    """Model-visible tool metadata and Harness-visible risk metadata."""

    name: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,63}$")
    description: str = Field(min_length=1, max_length=1000)
    input_schema: dict[str, Any]
    risk: RiskLevel = RiskLevel.READ
    schema_source: SchemaSource = SchemaSource.PYDANTIC
    # Fail-closed default: an undeclared tool is treated as unsafe to replay.
    effect_class: ToolEffectClass = ToolEffectClass.NON_IDEMPOTENT
    # Per-tool execution ceiling. Declared here so a slow tool can own its number
    # instead of everything sharing one constant.
    timeout_seconds: float = Field(default=30.0, gt=0)
    requires_workspace: bool = False
    # Declares that this tool's results depend on governed sources (a published
    # revision, a case record, a product row). Receipts from such a tool contribute
    # version-bound `SourceRef` entries that must be revalidated before the stored
    # output is served again (ADR 016). The compatibility default leaves existing
    # tools unchanged; hosts must explicitly mark every source-reading tool.
    governed_sources: bool = False
    schema_version: int = Field(default=1, ge=1)


class ToolErrorCode(StrEnum):
    """Small, frozen taxonomy of tool failures the model can act on.

    Deliberately short. More codes are added only when a real Bad Case needs one,
    because a large taxonomy nobody classifies correctly is worse than a small one
    that is always right.
    """

    PATH_NOT_FOUND = "path_not_found"
    INVALID_ARGUMENTS = "invalid_arguments"
    TIMEOUT = "timeout"
    # The tool's backend reported a temporary failure (connection reset, rate limit,
    # a 5xx). Added for a real Bad Case: an unclassified "transient" error was
    # reported as non-recoverable, and a compliant model correctly refused to retry.
    TRANSIENT = "transient_error"
    # The fallback for anything unclassified. Explicit so "unknown" is a recorded
    # fact rather than an absence of data.
    TOOL_EXECUTION_ERROR = "tool_execution_error"


class TransientToolError(RuntimeError):
    """Raised by a tool whose backend failed in a way that may succeed on repetition.

    Raising it is a claim about the *backend*, not about safety: whether repeating
    the call is safe is decided from the tool's declared effect class, so a
    non-idempotent tool raising this is still reported as having an unknown outcome.
    """


class ToolFailure(FrozenModel):
    """A structured, model-facing description of a tool failure.

    This is the *recovery interface*: what went wrong, whether another attempt
    could help, and what to do instead. Host diagnostics are deliberately excluded
    — no exception text, no absolute paths, no tracebacks — because they leak the
    environment into the model's context and are not actionable. Harness-facing
    detail lives in the trace instead.
    """

    code: ToolErrorCode
    message: str = Field(min_length=1)
    recoverable: bool
    # Values come from the model's own arguments, never parsed out of an exception
    # message, so a host path cannot travel through this field.
    details: dict[str, str] = Field(default_factory=dict)
    suggestion: str | None = None

    def as_observation(self) -> str:
        """Render the canonical model-visible observation.

        Deterministic (sorted keys) so an identical failure always produces
        byte-identical feedback, which keeps replay and progress detection stable.
        """
        error: dict[str, Any] = {
            "code": self.code.value,
            "recoverable": self.recoverable,
            "message": self.message,
        }
        if self.details:
            error["details"] = self.details
        if self.suggestion:
            error["suggestion"] = self.suggestion
        return json.dumps({"ok": False, "error": error}, sort_keys=True, separators=(",", ":"))


class ExecutionAllowance(FrozenModel):
    """Parent-owned resources a nested tool may consume during this dispatch."""

    remaining_steps: int = Field(ge=0)
    remaining_tool_calls: int = Field(ge=0)
    remaining_input_tokens: int = Field(ge=0)
    remaining_output_tokens: int = Field(ge=0)


class ToolContext(BaseModel):
    """Runtime-owned values passed to a tool after policy approval."""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid", frozen=True)

    task_id: str
    workspace: Path | None = None
    run_context: RunContext | None = None
    attempt_id: str | None = None
    allowance: ExecutionAllowance | None = None
    # Stable for the lifetime of one logical call: recovery must reuse the same
    # values, never derive new ones. A tool that does not consume `idempotency_key`
    # must not be declared `idempotent`.
    logical_call_id: str | None = None
    idempotency_key: str | None = None

    def require_workspace(self) -> Path:
        """Fail closed for file/process tools without a host-owned workspace."""
        if self.workspace is None:
            raise ValueError("tool requires a workspace")
        if self.run_context is not None and self.workspace.resolve() != self.workspace:
            raise ValueError("trusted workspace root changed")
        return self.workspace


class ToolOutput(FrozenModel):
    """Structured result returned to the model and trace."""

    ok: bool
    content: str
    # Present when the tool or dispatch failed: the classified failure the model
    # reads and the evaluator inspects. Absent on success.
    error: ToolFailure | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    usage: Usage | None = None
    receipt: BusinessReceipt | None = None


class Tool(Protocol):
    """Executable capability registered with the Harness."""

    @property
    def spec(self) -> ToolSpec:
        """Return stable metadata for validation, policy, and model requests."""
        ...

    @property
    def input_model(self) -> type[BaseModel]:
        """Return the Pydantic model that validates invocation arguments."""
        ...

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolOutput:
        """Execute already validated and policy-approved arguments."""
        ...
