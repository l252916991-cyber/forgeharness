"""Full serialized-input estimates and explicit output/opaque-overhead reservations."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import Field

from forgeharness.context.compiler import CharacterTokenEstimator, ContextBudgetError
from forgeharness.domain.models import FrozenModel
from forgeharness.models.base import Model, ModelRequest
from forgeharness.models.prepared import PreparedModelCall, canonical_json, prepare_model_call

if TYPE_CHECKING:
    from forgeharness.context.assembly import AssembledContext, ContextAssembler
    from forgeharness.observability.trace import TraceRecorder
    from forgeharness.runtime.budget import RunBudget


class RequestBudgetEstimate(FrozenModel):
    """Heuristic admission evidence, not real tokenizer usage or a billing estimate."""

    estimator: str = "characters-per-four-v1"
    input_tokens: int = Field(ge=0)
    message_tokens: int = Field(ge=0)
    tool_tokens: int = Field(ge=0)
    option_tokens: int = Field(ge=0)
    overhead_tokens: int = Field(ge=0)
    reserved_output_tokens: int = Field(ge=0)
    projected_tokens: int = Field(ge=0)
    limit: int = Field(ge=1)
    host_limit: int = Field(ge=1)
    provider_window_known: bool
    provider_overhead_known: bool
    provider_output_limit_known: bool
    format: str
    payload_sha256: str
    admitted: bool


def estimate_request(prepared: PreparedModelCall, budget: RunBudget) -> RequestBudgetEstimate:
    """Size every serialized field; components are standalone and need not sum."""
    estimator = CharacterTokenEstimator()
    payload = prepared.payload
    messages = payload.pop("messages", [])
    tools = payload.pop("tools", [])
    input_tokens = estimator.estimate(prepared.payload_json)
    overhead = budget.request_overhead_tokens + (prepared.provider_overhead_tokens or 0)
    reserved = max(budget.reserve_output_tokens, prepared.output_limit or 0)
    limit = min(budget.max_context_tokens, prepared.context_window or budget.max_context_tokens)
    projected = input_tokens + overhead + reserved
    return RequestBudgetEstimate(
        input_tokens=input_tokens,
        message_tokens=estimator.estimate(canonical_json(messages)),
        tool_tokens=estimator.estimate(canonical_json(tools)),
        option_tokens=estimator.estimate(canonical_json(payload)),
        overhead_tokens=overhead,
        reserved_output_tokens=reserved,
        projected_tokens=projected,
        limit=limit,
        host_limit=budget.max_context_tokens,
        provider_window_known=prepared.context_window is not None,
        provider_overhead_known=prepared.provider_overhead_tokens is not None,
        provider_output_limit_known=prepared.output_limit is not None,
        format=prepared.format,
        payload_sha256=prepared.payload_sha256,
        admitted=projected <= limit,
    )


def prepare_request(
    model: Model,
    request: ModelRequest,
    budget: RunBudget,
    assembler: ContextAssembler,
    trace: TraceRecorder,
    *,
    phase: str,
) -> tuple[AssembledContext, PreparedModelCall, RequestBudgetEstimate]:
    """Reserve non-message costs and reassemble intact history, at most three times."""
    reserved = 0
    for _ in range(3):
        assembled = assembler.assemble(request.messages, reserved_tokens=reserved)
        prepared = prepare_model_call(
            model, request.model_copy(update={"messages": assembled.messages})
        )
        estimate = estimate_request(prepared, budget)
        if estimate.admitted:
            return assembled, prepared, estimate
        trace.append(
            "context.preflight_rejected",
            {
                "phase": phase,
                "preflight": estimate.model_dump(mode="json"),
                "request": prepared.request.model_dump(mode="json"),
                "prepared_payload": prepared.payload,
            },
        )
        # The assembler counted message contents; reserve all remaining costs,
        # including message framing and a potentially smaller provider window.
        reserved = max(
            reserved + 1,
            budget.max_context_tokens
            - estimate.limit
            + estimate.projected_tokens
            - assembled.estimated_tokens,
        )
    raise ContextBudgetError("full prepared model request exceeds the declared context budget")
