"""Hosted execution governs the shared, exact prepared-model boundary."""

import asyncio
from collections.abc import Awaitable, Callable
from typing import cast

import pytest

from forgeharness.context.assembly import ContextAssembler
from forgeharness.domain.models import (
    FinalAction,
    Message,
    MessageRole,
    ModelResult,
    ModelUsage,
    Usage,
)
from forgeharness.models.base import Model, ModelProtocolError, ModelRequest
from forgeharness.models.prepared import PreparedModelCall, canonical_json, chat_inputs
from forgeharness.observability.trace import InMemoryTrace
from forgeharness.runtime.budget import RunBudget
from forgeharness.runtime.execution import AsyncExecutionBackend, ExecutionSuspended
from forgeharness.runtime.model_call import ModelCallKind, ModelCallOutcome, invoke_model_once

Dispatch = Callable[[], Awaitable[ModelCallOutcome]]
Handler = Callable[[Dispatch], Awaitable[ModelCallOutcome]]


class PreparedModel:
    def __init__(self, result: ModelResult | Exception | None = None) -> None:
        self.result = result or ModelResult(
            action=FinalAction(content="confirmed"),
            usage=ModelUsage(input_tokens=11, output_tokens=7),
            model_name="fixture-v1",
        )
        self.preparation_count = 0
        self.sent: list[PreparedModelCall] = []

    def prepare_request(self, request: ModelRequest) -> PreparedModelCall:
        self.preparation_count += 1
        return PreparedModelCall(
            request_json=canonical_json(request.model_dump(mode="json")),
            payload_json=canonical_json(
                {
                    **chat_inputs(request),
                    "model": "fixture-v1",
                    "temperature": 0.25,
                    "max_tokens": 64,
                }
            ),
            format="chat-completions-v1",
            output_limit=64,
            context_window=4096,
            provider_overhead_tokens=7,
        )

    async def decide_prepared(self, prepared: PreparedModelCall) -> ModelResult:
        self.sent.append(prepared)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    async def decide(self, request: ModelRequest) -> ModelResult:
        raise AssertionError("the exact prepared payload must be dispatched")


class RecordingBackend:
    """Only the model operation is needed by these focused boundary tests."""

    def __init__(self, handler: Handler | None = None) -> None:
        self.handler = handler
        self.calls: list[tuple[PreparedModelCall, Usage, str]] = []
        self.dispatch: Dispatch | None = None

    async def invoke_model(
        self,
        *,
        prepared: PreparedModelCall,
        usage: Usage,
        phase: str,
        dispatch: Dispatch,
    ) -> ModelCallOutcome:
        self.calls.append((prepared, usage, phase))
        self.dispatch = dispatch
        if self.handler is not None:
            return await self.handler(dispatch)
        return await dispatch()


async def invoke(
    model: Model,
    backend: RecordingBackend,
    *,
    usage: Usage | None = None,
    budget: RunBudget | None = None,
    trace: InMemoryTrace | None = None,
) -> ModelCallOutcome:
    limits = budget or RunBudget()
    return await invoke_model_once(
        model=model,
        request=ModelRequest(
            task_id="hosted-run",
            messages=(Message(role=MessageRole.USER, content="hello"),),
            tools=(),
        ),
        budget=limits,
        assembler=ContextAssembler(max_tokens=limits.max_context_tokens),
        trace=trace or InMemoryTrace("hosted-run"),
        usage=usage or Usage(),
        phase="hosted-test",
        execution_backend=cast(AsyncExecutionBackend, backend),
    )


async def test_backend_receives_exact_prepared_input_and_original_usage() -> None:
    model = PreparedModel()
    backend = RecordingBackend()
    usage = Usage(steps=2, tool_calls=3, input_tokens=20, output_tokens=5)
    trace = InMemoryTrace("hosted-run")

    outcome = await invoke(model, backend, usage=usage, trace=trace)

    assert model.preparation_count == 1
    assert len(backend.calls) == len(model.sent) == 1
    prepared, admitted_usage, phase = backend.calls[0]
    assert model.sent[0] is prepared
    assert prepared.payload["model"] == "fixture-v1"
    assert prepared.payload["temperature"] == 0.25
    assert prepared.output_limit == 64
    assert admitted_usage is usage
    assert phase == "hosted-test"
    assert prepared.request.task_id == "hosted-run"
    assert prepared.request.messages[0].content == "hello"
    assert trace.events[0].payload["prepared_payload"] == prepared.payload
    assert [event.type for event in trace.events] == ["model.request", "model.action"]
    assert outcome.kind is ModelCallKind.SUCCESS
    assert outcome.usage == Usage(steps=3, tool_calls=3, input_tokens=31, output_tokens=12)
    assert outcome.result is model.result


async def test_backend_can_refuse_sending_without_entering_provider() -> None:
    usage = Usage(steps=2)
    refused = ModelCallOutcome(kind=ModelCallKind.PREPARATION_FAILED, usage=usage, not_sent=True)

    async def refuse(dispatch: Dispatch) -> ModelCallOutcome:
        return refused

    model = PreparedModel()
    backend = RecordingBackend(refuse)
    trace = InMemoryTrace("hosted-run")

    outcome = await invoke(model, backend, usage=usage, trace=trace)

    assert outcome is refused
    assert not model.sent
    assert [event.type for event in trace.events] == ["model.request"]
    assert backend.dispatch is not None
    with pytest.raises(RuntimeError, match="single-use"):
        await backend.dispatch()
    assert not model.sent


async def test_backend_suspension_propagates_and_invalidates_dispatch() -> None:
    suspended = ExecutionSuspended("receipt reconciliation required")

    async def suspend(dispatch: Dispatch) -> ModelCallOutcome:
        raise suspended

    model = PreparedModel()
    backend = RecordingBackend(suspend)
    trace = InMemoryTrace("hosted-run")

    with pytest.raises(ExecutionSuspended) as caught:
        await invoke(model, backend, trace=trace)

    assert caught.value is suspended
    assert not model.sent
    assert [event.type for event in trace.events] == ["model.request"]
    assert backend.dispatch is not None
    with pytest.raises(RuntimeError, match="single-use"):
        await backend.dispatch()


async def test_suspension_after_provider_response_is_not_a_model_failure() -> None:
    async def suspend_after_send(dispatch: Dispatch) -> ModelCallOutcome:
        await dispatch()
        raise ExecutionSuspended("response commit needs reconciliation")

    model = PreparedModel()
    trace = InMemoryTrace("hosted-run")

    with pytest.raises(ExecutionSuspended, match="response commit"):
        await invoke(model, RecordingBackend(suspend_after_send), trace=trace)

    assert len(model.sent) == 1
    assert [event.type for event in trace.events] == ["model.request", "model.action"]


async def test_backend_cannot_dispatch_twice() -> None:
    async def send_twice(dispatch: Dispatch) -> ModelCallOutcome:
        await dispatch()
        return await dispatch()

    model = PreparedModel()
    trace = InMemoryTrace("hosted-run")

    with pytest.raises(RuntimeError, match="single-use"):
        await invoke(model, RecordingBackend(send_twice), trace=trace)

    assert len(model.sent) == 1
    assert [event.type for event in trace.events] == ["model.request", "model.action"]


async def test_concurrent_dispatch_attempts_only_enter_provider_once() -> None:
    async def send_concurrently(dispatch: Dispatch) -> ModelCallOutcome:
        results = await asyncio.gather(dispatch(), dispatch(), return_exceptions=True)
        assert isinstance(results[0], ModelCallOutcome)
        assert isinstance(results[1], RuntimeError)
        return results[0]

    model = PreparedModel()

    outcome = await invoke(model, RecordingBackend(send_concurrently))

    assert outcome.kind is ModelCallKind.SUCCESS
    assert len(model.sent) == 1


async def test_failed_dispatch_cannot_be_retried_by_backend() -> None:
    async def retry_failure(dispatch: Dispatch) -> ModelCallOutcome:
        first = await dispatch()
        assert first.kind is ModelCallKind.MODEL_FAILED
        return await dispatch()

    model = PreparedModel(RuntimeError("connection outcome unknown"))

    with pytest.raises(RuntimeError, match="single-use"):
        await invoke(model, RecordingBackend(retry_failure))

    assert len(model.sent) == 1


async def test_cached_result_does_not_send_or_recalculate_usage() -> None:
    cached = ModelCallOutcome(
        kind=ModelCallKind.SUCCESS,
        result=ModelResult(action=FinalAction(content="recovered")),
        usage=Usage(steps=3, input_tokens=45, output_tokens=19),
        response_received=True,
    )

    async def recover(dispatch: Dispatch) -> ModelCallOutcome:
        return cached

    model = PreparedModel()
    backend = RecordingBackend(recover)

    outcome = await invoke(model, backend)

    assert outcome is cached
    assert not model.sent
    assert backend.dispatch is not None
    with pytest.raises(RuntimeError, match="single-use"):
        await backend.dispatch()


async def test_preparation_failure_never_enters_backend_or_provider() -> None:
    class BrokenPreparation(PreparedModel):
        def prepare_request(self, request: ModelRequest) -> PreparedModelCall:
            raise ValueError("invalid adapter preparation")

    model = BrokenPreparation()
    backend = RecordingBackend()
    trace = InMemoryTrace("hosted-run")
    usage = Usage(steps=2, input_tokens=13)

    outcome = await invoke(model, backend, usage=usage, trace=trace)

    assert outcome.kind is ModelCallKind.PREPARATION_FAILED
    assert outcome.usage is usage
    assert not backend.calls
    assert not model.sent
    assert [event.type for event in trace.events] == ["model.preparation_failed"]


async def test_context_overflow_never_enters_backend_or_provider() -> None:
    model = PreparedModel()
    backend = RecordingBackend()
    usage = Usage(steps=2)

    outcome = await invoke(model, backend, usage=usage, budget=RunBudget(max_context_tokens=1))

    assert outcome.kind is ModelCallKind.CONTEXT_OVERFLOW
    assert outcome.usage is usage
    assert not backend.calls
    assert not model.sent


@pytest.mark.parametrize("recoverable", [False, True])
async def test_backend_keeps_original_protocol_accounting(recoverable: bool) -> None:
    violation = ModelProtocolError(
        "rejected response",
        usage=ModelUsage(input_tokens=80, output_tokens=12),
        recoverable=recoverable,
    )
    model = PreparedModel(violation)
    backend = RecordingBackend()
    usage = Usage(steps=2, tool_calls=3, input_tokens=20, output_tokens=5)
    trace = InMemoryTrace("hosted-run")

    outcome = await invoke(model, backend, usage=usage, trace=trace)

    assert len(model.sent) == 1
    assert outcome.violation is violation
    assert outcome.response_received
    assert [event.type for event in trace.events] == ["model.request", "model.failed"]
    if recoverable:
        assert outcome.kind is ModelCallKind.PROTOCOL_VIOLATION
        assert outcome.usage == Usage(steps=3, tool_calls=3, input_tokens=100, output_tokens=17)
    else:
        assert outcome.kind is ModelCallKind.MODEL_FAILED
        assert outcome.usage is usage
