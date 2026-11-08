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


# HTTP 4xx answers that do not prove the request went unprocessed: 408 means the
# server gave up waiting and 429 is throttling a later attempt may pass. Every
# other 4xx is a refusal issued before any inference.
_NON_DEFINITIVE_CLIENT_STATUSES = frozenset({408, 429})


def is_definitive_rejection(status_code: int) -> bool:
    """Whether an HTTP status proves the provider refused the request unprocessed."""
    return 400 <= status_code < 500 and status_code not in _NON_DEFINITIVE_CLIENT_STATUSES


class ModelRequestRejected(RuntimeError):
    """The provider definitively refused a request before any inference (ADR 050).

    Only raised for statuses ``is_definitive_rejection`` accepts, such as 400,
    401, 402 or 404: the response itself proves nothing was processed, so nothing
    was billed and nothing about the call is unknown. Server errors, timeouts,
    408 and 429 are not this exception; a durable caller treats those as unknown.
    The provider's response body is never kept: it is untrusted and may echo input.
    """

    code = "provider_rejected"
    recoverable = False

    def __init__(self, status_code: int) -> None:
        if not is_definitive_rejection(status_code):
            raise ValueError("not a definitive provider rejection status")
        super().__init__(f"provider rejected the request with HTTP {status_code}")
        self.status_code = status_code


class Model(Protocol):
    """Interface implemented by real providers and deterministic doubles."""

    async def decide(self, request: ModelRequest) -> ModelResult:
        """Return one validated action for the current run state."""
        ...
