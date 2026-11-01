"""One shared, governed model invocation for every orchestration style.

Both the ReAct ``AgentRuntime`` and the custom-orchestrator ``RunKernel`` call
models through :func:`invoke_model_once`, so preparation, request budgeting,
trace events and — critically — token accounting cannot drift between the two
paths. The function performs mechanics only: loop policy (violation feedback,
retry limits, termination) stays with the caller.

Accounting semantics (identical to the historical ReAct behavior):

* success charges one step plus the provider-reported tokens;
* a recoverable protocol violation charges one step plus the tokens the rejected
  request actually consumed — the inference happened, so the cost is real;
* a non-recoverable model failure charges nothing (the run ends immediately);
* preparation failures charge nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from forgeharness.context.assembly import ContextAssembler
from forgeharness.context.compiler import ContextBudgetError
from forgeharness.context.request_budget import prepare_request
from forgeharness.domain.models import ModelResult, Usage
from forgeharness.models.base import Model, ModelProtocolError, ModelRequest
from forgeharness.models.caching import cache_report
from forgeharness.models.prepared import PreparedModelCall, decide_prepared_model
from forgeharness.observability.trace import TraceRecorder
from forgeharness.runtime.budget import RunBudget

if TYPE_CHECKING:
    from forgeharness.runtime.execution import AsyncExecutionBackend


class ModelCallKind(StrEnum):
    """Outcome classification for one governed model invocation."""

    SUCCESS = "success"
    CONTEXT_OVERFLOW = "context_overflow"
    PREPARATION_FAILED = "preparation_failed"
    MODEL_FAILED = "model_failed"
    PROTOCOL_VIOLATION = "protocol_violation"


@dataclass(frozen=True)
class ModelCallOutcome:
    """Result of one invocation: exactly one branch is meaningful per ``kind``.

    ``usage`` is the caller's cumulative usage after applying this call's
    accounting, so both orchestration styles charge identical amounts.
    ``violation`` preserves the original exception for callers that feed
    recoverable violations back to the model.
    """

    kind: ModelCallKind
    usage: Usage = field(default_factory=Usage)
    result: ModelResult | None = None
    error: str | None = None
    error_type: str | None = None
    violation: Exception | None = None
    response_received: bool = False
    not_sent: bool = False


def usage_payload(usage: object) -> dict[str, int] | None:
    """Serialize a rejected request's token usage for the trace, if reported."""
    if usage is None:
        return None
    input_tokens = getattr(usage, "input_tokens", None)
    output_tokens = getattr(usage, "output_tokens", None)
    if input_tokens is None and output_tokens is None:
        return None
    return {"input_tokens": int(input_tokens or 0), "output_tokens": int(output_tokens or 0)}


def rejected_input_tokens(exc: Exception) -> int:
    """Input tokens the rejected request consumed, when the provider reported them."""
    usage = getattr(exc, "usage", None)
    value = getattr(usage, "input_tokens", 0) if usage is not None else 0
    return int(value or 0)


def rejected_output_tokens(exc: Exception) -> int:
    """Output tokens the rejected request produced, when the provider reported them."""
    usage = getattr(exc, "usage", None)
    value = getattr(usage, "output_tokens", 0) if usage is not None else 0
    return int(value or 0)


def _budget_remaining(budget: RunBudget, usage: Usage) -> dict[str, int]:
    return {
        "steps": max(0, budget.max_steps - usage.steps),
        "tool_calls": max(0, budget.max_tool_calls - usage.tool_calls),
        "input_tokens": max(0, budget.max_input_tokens - usage.input_tokens),
        "output_tokens": max(0, budget.max_output_tokens - usage.output_tokens),
    }


def prepare_model_once(
    *,
    model: Model,
    request: ModelRequest,
    budget: RunBudget,
    assembler: ContextAssembler,
    trace: TraceRecorder,
    usage: Usage,
    phase: str,
) -> PreparedModelCall | ModelCallOutcome:
    """Prepare and durably trace one exact input without entering a provider.

    Emits the canonical event sequence: ``context.overflow`` or
    ``model.preparation_failed`` on preparation failure; otherwise
    ``context.compacted`` (when compaction happened), ``model.request``, then
    either ``model.action`` (success) or ``model.failed`` (provider/protocol
    error). Every payload carries ``phase`` so projections can pair events
    without guessing from order.
    """

    try:
        assembled, prepared, estimate = prepare_request(
            model, request, budget, assembler, trace, phase=phase
        )
    except ContextBudgetError as exc:
        trace.append("context.overflow", {"phase": phase, "error": str(exc)})
        return ModelCallOutcome(
            kind=ModelCallKind.CONTEXT_OVERFLOW,
            usage=usage,
            error=f"context budget exceeded: {exc}",
        )
    except Exception as exc:
        trace.append(
            "model.preparation_failed",
            {"phase": phase, "error_type": type(exc).__name__},
        )
        return ModelCallOutcome(
            kind=ModelCallKind.PREPARATION_FAILED,
            usage=usage,
            error="model request preparation failed",
            error_type="model_preparation_failed",
        )
    if assembled.compacted:
        trace.append(
            "context.compacted",
            {
                "phase": phase,
                "sent_messages": len(assembled.messages),
                "dropped_messages": assembled.dropped_messages,
                "estimated_tokens": assembled.estimated_tokens,
                "max_tokens": budget.max_context_tokens,
                "decisions": [
                    decision.model_dump(mode="json")
                    for decision in assembled.decisions
                    if not decision.included
                ],
            },
        )
    prepared_request = prepared.request
    # The phase tag lets a projection pair each request with its own action and
    # attribute latency without guessing from event order.
    trace.append(
        "model.request",
        {
            "phase": phase,
            **prepared_request.model_dump(mode="json"),
            "preflight": estimate.model_dump(mode="json"),
            "prepared_payload": prepared.payload,
        },
    )
    return prepared


async def dispatch_model_once(
    *,
    model: Model,
    prepared: PreparedModelCall,
    budget: RunBudget,
    trace: TraceRecorder,
    usage: Usage,
    phase: str,
) -> ModelCallOutcome:
    """Send a prepared input once; the caller owns the durable sending permit."""
    # The cache plan is model-visible input: it decides what the provider bills at
    # the cached rate. Probing for the planner rather than requiring it keeps every
    # adapter that has no cache concept on the same code path.
    planner = getattr(model, "cache_plan", None)
    if callable(planner):
        trace.append(
            "model.cache",
            {"phase": phase, **cache_report(planner(prepared.request))},
        )
    try:
        result = await decide_prepared_model(model, prepared)
    except Exception as exc:
        # A provider may raise a typed protocol error carrying a stable code;
        # recording it keeps the reason machine-readable instead of leaving only
        # a class name and a prose message.
        recoverable = getattr(exc, "recoverable", False) is True
        trace.append(
            "model.failed",
            {
                "phase": phase,
                "error_type": type(exc).__name__,
                "error_code": getattr(exc, "code", None),
                "received_tool_calls": getattr(exc, "received_tool_calls", None),
                "recoverable": getattr(exc, "recoverable", None),
                # A rejected request still consumed tokens; recording them keeps
                # cost accounting honest and lets a replay rebuild the same
                # exception without contacting a provider.
                "usage": usage_payload(getattr(exc, "usage", None)),
                "error": str(exc),
            },
        )
        if not recoverable:
            return ModelCallOutcome(
                kind=ModelCallKind.MODEL_FAILED,
                usage=usage,
                error=f"model failed: {type(exc).__name__}: {exc}",
                error_type=type(exc).__name__,
                violation=exc,
                response_received=isinstance(exc, ModelProtocolError),
            )
        # The response arrived intact but broke the single-action protocol, so a
        # caller may plausibly let the model fix it. The inference really
        # happened, so the step and its tokens are charged here, once, for every
        # orchestration style.
        charged = Usage(
            steps=usage.steps + 1,
            tool_calls=usage.tool_calls,
            input_tokens=usage.input_tokens + rejected_input_tokens(exc),
            output_tokens=usage.output_tokens + rejected_output_tokens(exc),
        )
        return ModelCallOutcome(
            kind=ModelCallKind.PROTOCOL_VIOLATION,
            usage=charged,
            violation=exc,
            response_received=True,
        )
    charged = Usage(
        steps=usage.steps + 1,
        tool_calls=usage.tool_calls,
        input_tokens=usage.input_tokens + result.usage.input_tokens,
        output_tokens=usage.output_tokens + result.usage.output_tokens,
    )
    cache_read = getattr(model, "last_cache_read_tokens", 0)
    if isinstance(cache_read, int) and cache_read > 0:
        # Only emitted when the provider actually reported a hit, so the absence of
        # the event means "no cache accounting", not "nothing was cached".
        trace.append(
            "model.cache.observed",
            {
                "phase": phase,
                "cached_input_tokens": cache_read,
                "input_tokens": result.usage.input_tokens,
            },
        )
    trace.append(
        "model.action",
        {
            "phase": phase,
            "kind": result.action.kind,
            "action": result.action.model_dump(mode="json"),
            "model_name": result.model_name,
            "input_tokens": result.usage.input_tokens,
            "output_tokens": result.usage.output_tokens,
            "budget": _budget_remaining(budget, charged),
        },
    )
    return ModelCallOutcome(
        kind=ModelCallKind.SUCCESS, usage=charged, result=result, response_received=True
    )


async def invoke_model_once(
    *,
    model: Model,
    request: ModelRequest,
    budget: RunBudget,
    assembler: ContextAssembler,
    trace: TraceRecorder,
    usage: Usage,
    phase: str,
    execution_backend: AsyncExecutionBackend | None = None,
) -> ModelCallOutcome:
    """Prepare once, then optionally obtain a hosted execution send permit."""
    prepared = prepare_model_once(
        model=model,
        request=request,
        budget=budget,
        assembler=assembler,
        trace=trace,
        usage=usage,
        phase=phase,
    )
    if isinstance(prepared, ModelCallOutcome):
        return prepared
    if execution_backend is None:
        return await dispatch_model_once(
            model=model, prepared=prepared, budget=budget, trace=trace, usage=usage, phase=phase
        )

    dispatched = False
    active = True

    async def dispatch() -> ModelCallOutcome:
        nonlocal dispatched
        if dispatched or not active:
            raise RuntimeError(
                "model dispatch closure is single-use and must be awaited in backend"
            )
        # Set before any await so concurrent backend attempts cannot both send.
        dispatched = True
        return await dispatch_model_once(
            model=model, prepared=prepared, budget=budget, trace=trace, usage=usage, phase=phase
        )

    try:
        return await execution_backend.invoke_model(
            prepared=prepared, usage=usage, phase=phase, dispatch=dispatch
        )
    finally:
        # Cached/refused/suspended invocations must not retain a future send permit.
        active = False


__all__ = [
    "ModelCallKind",
    "ModelCallOutcome",
    "dispatch_model_once",
    "invoke_model_once",
    "prepare_model_once",
    "rejected_input_tokens",
    "rejected_output_tokens",
    "usage_payload",
]
