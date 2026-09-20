"""Provider-neutral model protocol."""

from __future__ import annotations

from typing import Protocol

from forgeharness.domain.models import FrozenModel, Message, ModelResult, ModelUsage
from forgeharness.tools.base import ToolSpec

# Violations a re-ask can plausibly fix, because the response arrived intact but
# broke the action protocol. A transport or parse failure is excluded: re-sending
# an identical prompt is not obviously better there. Kept as an explicit set so
# widening recoverability is a deliberate decision rather than a side effect.
RECOVERABLE_VIOLATION_CODES = frozenset({"multiple_tool_calls_not_allowed"})


class ModelRequest(FrozenModel):
    """Validated inputs available to a model for one agent step."""

    task_id: str
    messages: tuple[Message, ...]
    tools: tuple[ToolSpec, ...]


class ModelProtocolError(RuntimeError):
    """A model response the Harness cannot execute as a single action.

    ``code`` is a stable, machine-readable identifier so a trace, a classification
    and a runtime decision can name the exact violation instead of matching a
    human message. Provider responses are untrusted input, so the code is assigned
    by the adapter rather than parsed out of the response.

    ``recoverable`` says whether feeding the violation back to the model as an
    observation could plausibly let it produce a valid action. ``usage`` preserves
    the tokens the rejected request actually consumed, which would otherwise be
    lost with the exception and make cost accounting understate a run.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str = "model_protocol_error",
        received_tool_calls: int | None = None,
        usage: ModelUsage | None = None,
        recoverable: bool | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.received_tool_calls = received_tool_calls
        self.usage = usage
        self.recoverable = (
            code in RECOVERABLE_VIOLATION_CODES if recoverable is None else recoverable
        )


class Model(Protocol):
    """Interface implemented by real providers and deterministic doubles."""

    async def decide(self, request: ModelRequest) -> ModelResult:
        """Return one validated action for the current run state."""
        ...
