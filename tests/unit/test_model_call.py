"""Shared governed model invocation: one accounting rule for every orchestrator."""

from forgeharness.context.assembly import ContextAssembler
from forgeharness.domain.models import (
    FinalAction,
    Message,
    MessageRole,
    ModelResult,
    ModelUsage,
    Usage,
)
from forgeharness.models.base import ModelProtocolError, ModelRequest
from forgeharness.observability.trace import InMemoryTrace
from forgeharness.runtime.budget import RunBudget
from forgeharness.runtime.model_call import ModelCallKind, invoke_model_once
from forgeharness.sdk import ScriptedModel


class RaisingModel:
    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    async def decide(self, request: ModelRequest) -> ModelResult:
        del request
        raise self.exc


def request(content: str = "hello") -> ModelRequest:
    return ModelRequest(
        task_id="t",
        messages=(Message(role=MessageRole.USER, content=content),),
        tools=(),
    )


async def call(
    model,
    *,
    budget: RunBudget | None = None,
    usage: Usage | None = None,
    req: ModelRequest | None = None,
):
    trace = InMemoryTrace("t")
    outcome = await invoke_model_once(
        model=model,
        request=req or request(),
        budget=budget or RunBudget(),
        assembler=ContextAssembler(max_tokens=(budget or RunBudget()).max_context_tokens),
        trace=trace,
        usage=usage or Usage(),
        phase="test",
    )
    return outcome, trace


async def test_success_charges_step_and_provider_tokens() -> None:
    model = ScriptedModel(
        [
            ModelResult(
                action=FinalAction(content="ok"),
                usage=ModelUsage(input_tokens=5, output_tokens=3),
            )
        ]
    )

    outcome, trace = await call(model, usage=Usage(tool_calls=2))

    assert outcome.kind is ModelCallKind.SUCCESS
    assert outcome.result is not None
    assert outcome.usage == Usage(steps=1, tool_calls=2, input_tokens=5, output_tokens=3)
    types = [event.type for event in trace.events]
    assert types == ["model.request", "model.action"]
    action = trace.events[1].payload
    assert action["phase"] == "test"
    assert action["budget"]["steps"] == RunBudget().max_steps - 1


async def test_recoverable_violation_charges_rejected_tokens() -> None:
    model = RaisingModel(
        ModelProtocolError(
            "two tool calls",
            code="multiple_tool_calls_not_allowed",
            received_tool_calls=2,
            usage=ModelUsage(input_tokens=80, output_tokens=12),
            recoverable=True,
        )
    )

    outcome, trace = await call(model)

    assert outcome.kind is ModelCallKind.PROTOCOL_VIOLATION
    assert outcome.usage == Usage(steps=1, input_tokens=80, output_tokens=12)
    assert outcome.violation is not None
    failed = next(e for e in trace.events if e.type == "model.failed")
    assert failed.payload["usage"] == {"input_tokens": 80, "output_tokens": 12}
    assert failed.payload["error_code"] == "multiple_tool_calls_not_allowed"
    assert failed.payload["phase"] == "test"


async def test_non_recoverable_failure_charges_nothing() -> None:
    model = RaisingModel(
        ModelProtocolError("provider exploded", code="provider_error", recoverable=False)
    )

    outcome, trace = await call(model, usage=Usage(steps=2))

    assert outcome.kind is ModelCallKind.MODEL_FAILED
    assert outcome.usage == Usage(steps=2)
    assert outcome.error is not None and "model failed" in outcome.error
    assert outcome.error_type == "ModelProtocolError"
    assert any(e.type == "model.failed" for e in trace.events)


async def test_generic_exception_is_model_failed() -> None:
    outcome, _ = await call(RaisingModel(RuntimeError("boom")))

    assert outcome.kind is ModelCallKind.MODEL_FAILED
    assert outcome.error_type == "RuntimeError"


async def test_context_overflow_is_classified_and_traced() -> None:
    budget = RunBudget(max_context_tokens=1)
    outcome, trace = await call(
        ScriptedModel([ModelResult(action=FinalAction(content="ok"))]),
        budget=budget,
        req=request("a" * 500),
    )

    assert outcome.kind is ModelCallKind.CONTEXT_OVERFLOW
    assert outcome.usage == Usage()
    assert outcome.error is not None and "context budget exceeded" in outcome.error
    overflow = next(e for e in trace.events if e.type == "context.overflow")
    assert overflow.payload["phase"] == "test"
