"""Validated domain models for agent execution."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

TaskId = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")]


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


class TraceEvent(FrozenModel):
    """A sequence-addressable event that avoids recording hidden reasoning."""

    task_id: TaskId
    sequence: int = Field(ge=1)
    type: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,63}$")
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    payload: dict[str, Any] = Field(default_factory=dict)
