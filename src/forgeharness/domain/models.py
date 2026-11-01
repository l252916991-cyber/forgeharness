"""Validated domain models for agent execution."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

TaskId = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")]

# The recovery-semantics generation a run is written under, defined once so the
# runtime, the checkpoint payload, and the evaluation report cannot disagree.
#
# 0 = pre-journal: no durable record of tool side effects.
# 1 = invocation journal with effect-class recovery (ADR 009, M10-A).
# 2 = (1) plus runtime retry with attempt records and deterministic backoff (M10-B)
#     and the whole-run soft deadline (M10-C).
# 3 = scoped SDK binding, durable approval admission, ordered evidence, and safe
#     stopped-worker replay using current authorization and physical attempt IDs.
#
# The point of the number is that two generations must not share it, so a report
# can never be read as if the runtime behaved the same way. **Known record defect:**
# the M10-B and M10-C dev regressions were run before this bump and therefore carry
# `1` even though they exercised retry and deadline semantics. They are historical
# files and are deliberately not rewritten; only reports from the freeze onward
# carry `2`.
CURRENT_RECOVERY_SEMANTICS_VERSION = 3


class FrozenModel(BaseModel):
    """Base class for immutable domain values."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class MessageRole(StrEnum):
    """Roles used in the provider-neutral conversation history."""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class ToolCall(FrozenModel):
    """A model-requested invocation before policy evaluation."""

    id: str = Field(min_length=1, max_length=128)
    name: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,63}$")
    arguments: dict[str, Any] = Field(default_factory=dict)


class Message(FrozenModel):
    """One provider-neutral message, including structured tool metadata."""

    role: MessageRole
    content: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None
    tool_name: str | None = None

    @model_validator(mode="after")
    def validate_role_fields(self) -> Message:
        """Reject role-specific fields that would create ambiguous transcripts."""
        if self.role == MessageRole.ASSISTANT and self.tool_calls:
            if self.tool_call_id is not None or self.tool_name is not None:
                raise ValueError("assistant tool calls cannot contain tool result metadata")
            return self
        if self.role == MessageRole.TOOL:
            if not self.tool_call_id or not self.tool_name:
                raise ValueError("tool messages require tool_call_id and tool_name")
            if self.tool_calls:
                raise ValueError("tool messages cannot request tools")
            return self
        if self.tool_calls or self.tool_call_id is not None or self.tool_name is not None:
            raise ValueError(f"{self.role.value} messages cannot contain tool metadata")
        if self.content is None:
            raise ValueError(f"{self.role.value} messages require content")
        return self


class ToolAction(FrozenModel):
    """A decision to invoke exactly one tool."""

    kind: Literal["tool"] = "tool"
    call: ToolCall


class FinalAction(FrozenModel):
    """A decision that ends the run with a user-visible result."""

    kind: Literal["final"] = "final"
    content: str


AgentAction = Annotated[ToolAction | FinalAction, Field(discriminator="kind")]


class ModelUsage(FrozenModel):
    """Provider-reported token usage for one decision."""

    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)


class ModelResult(FrozenModel):
    """One provider response after it has been parsed and validated."""

    action: AgentAction
    usage: ModelUsage = ModelUsage()
    model_name: str = "unknown"


class RunStatus(StrEnum):
    """Terminal and suspended states exposed by the Harness."""

    CREATED = "created"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    EXHAUSTED = "exhausted"
    CANCELLED = "cancelled"


class Usage(FrozenModel):
    """Cumulative resource usage controlled by the Harness."""

    steps: int = Field(default=0, ge=0)
    tool_calls: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)


class PendingApproval(FrozenModel):
    """An exact action suspended for external approval."""

    call: ToolCall
    reason: str


class Actor(FrozenModel):
    """Identity asserted by the trusted host, never extracted from task text."""

    id: str = Field(min_length=1, max_length=200)
    roles: tuple[str, ...] = ()


class ResourceScope(FrozenModel):
    """Host-authorized workspace and business resources."""

    tenant_id: str = Field(min_length=1, max_length=128)
    resources: tuple[str, ...] = ()
    workspace: Path | None = None

    @model_validator(mode="after")
    def canonical_workspace(self) -> ResourceScope:
        if self.workspace is not None and self.workspace != self.workspace.resolve():
            raise ValueError("workspace must be an absolute canonical path")
        return self


class RunContext(FrozenModel):
    """Versioned, trusted execution binding persisted with every SDK run."""

    application_id: TaskId
    application_version: str = Field(min_length=1)
    run_id: TaskId
    session_id: str = Field(min_length=1)
    actor: Actor
    scope: ResourceScope
    policy_version: str = Field(min_length=1)
    correlation_id: str = Field(min_length=1)
    contract_version: Literal[1] = 1


class BusinessReceipt(FrozenModel):
    """Application evidence; accepted/queued does not mean completed."""

    resource_id: str = Field(min_length=1)
    status: str = Field(min_length=1)
    version: str = Field(min_length=1)
    evidence_refs: tuple[str, ...] = ()
    schema_version: Literal[1] = 1


class ToolEvidence(FrozenModel):
    """Ordered structured outcome persisted across approval and recovery."""

    name: str
    ok: bool
    content: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    logical_call_id: str | None = None
    receipt: BusinessReceipt | None = None


class RunResult(FrozenModel):
    """Complete observable result of one runtime invocation."""

    task_id: TaskId
    status: RunStatus
    messages: tuple[Message, ...]
    usage: Usage
    checkpoint_revision: int = Field(default=0, ge=0)
    final_output: str | None = None
    error: str | None = None
    pending_approval: PendingApproval | None = None
    # Which recovery semantics this run was produced under. 0 means it predates the
    # invocation journal, so no durable record of its tool side effects exists.
    # Recovery must fail closed on such a run rather than infer from the trace,
    # because a missing `tool.completed` event never proves a tool did not run.
    recovery_semantics_version: int = Field(default=0, ge=0)
    # R2 writes v2 so older installations reject rather than treating graph
    # checkpoints as resumable ReAct transcripts. Existing runs remain v1.
    state_version: Literal[1, 2] = 1
    run_context: RunContext | None = None
    tool_evidence: tuple[ToolEvidence, ...] = ()
    authorized_call_id: str | None = None


class TraceEvent(FrozenModel):
    """A sequence-addressable event that avoids recording hidden reasoning."""

    task_id: TaskId
    sequence: int = Field(ge=1)
    type: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,63}$")
    event_version: Literal[1] = 1
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    payload: dict[str, Any] = Field(default_factory=dict)
