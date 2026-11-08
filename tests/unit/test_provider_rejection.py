"""A definitive provider refusal settles as a failure; uncertain ones stay unknown (ADR 050)."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from examples.collaboration_demo import assignment, demo_plan, demo_report
from forgeharness.models.base import ModelRequestRejected, is_definitive_rejection
from forgeharness.models.openai_compatible import OpenAICompatibleConfig, OpenAICompatibleModel
from forgeharness.models.prepared import prepare_model_call
from forgeharness.observability.trace import InMemoryTrace
from forgeharness.runtime.model_call import ModelCallKind, dispatch_model_once
from forgeharness.sdk import (
    Actor,
    Collaboration,
    FinalAction,
    Message,
    MessageRole,
    ModelDispatcher,
    ModelRequest,
    ModelResult,
    ModelUsage,
    ParallelExecution,
    ResourceScope,
    RunBudget,
    RunContext,
    RunStatus,
    Usage,
    verify_trace,
)


@pytest.mark.parametrize("status", [400, 401, 402, 403, 404, 413, 422])
def test_client_errors_that_prove_no_processing_are_definitive(status: int) -> None:
    assert is_definitive_rejection(status)
    assert ModelRequestRejected(status).status_code == status


@pytest.mark.parametrize("status", [200, 302, 408, 429, 500, 502, 503])
def test_timeouts_throttling_and_server_errors_are_not(status: int) -> None:
    assert not is_definitive_rejection(status)
    with pytest.raises(ValueError, match="not a definitive"):
        ModelRequestRejected(status)


def _request() -> ModelRequest:
    return ModelRequest(
        task_id="t", messages=(Message(role=MessageRole.USER, content="hello"),), tools=()
    )


async def _adapter_error(status: int) -> BaseException:
    transport = httpx.MockTransport(lambda request: httpx.Response(status, text="no balance"))
    async with httpx.AsyncClient(transport=transport) as client:
        model = OpenAICompatibleModel(
            OpenAICompatibleConfig(
                base_url="https://provider.invalid/v1", api_key="test-key", model="m"
            ),
            client=client,
        )
        with pytest.raises(Exception) as caught:
            await model.decide(_request())
    return caught.value


async def test_the_adapter_names_a_refusal_and_keeps_other_http_errors() -> None:
    refused = await _adapter_error(402)
    assert isinstance(refused, ModelRequestRejected) and refused.status_code == 402
    assert "no balance" not in str(refused)  # the untrusted body is never kept
    for status in (429, 503):
        assert isinstance(await _adapter_error(status), httpx.HTTPStatusError)


class _Raising:
    def __init__(self, failure: Exception) -> None:
        self.failure = failure

    async def decide(self, request: ModelRequest) -> ModelResult:
        raise self.failure


@pytest.mark.parametrize(
    ("failure", "rejected"),
    [(ModelRequestRejected(402), True), (TimeoutError("lost"), False)],
)
async def test_dispatch_marks_only_a_refusal_as_a_received_answer(
    failure: Exception, rejected: bool
) -> None:
    trace = InMemoryTrace("t")
    model = _Raising(failure)
    outcome = await dispatch_model_once(
        model=model,
        prepared=prepare_model_call(model, _request()),
        budget=RunBudget(),
        trace=trace,
        usage=Usage(),
        phase="react",
    )
    assert outcome.kind is ModelCallKind.MODEL_FAILED and outcome.usage == Usage()
    assert outcome.rejected is rejected and outcome.response_received is rejected
    failed = next(e for e in trace.events if e.type == "model.failed")
    assert failed.payload.get("status_code") == (402 if rejected else None)
    assert ("status_code" in failed.payload) is rejected


class RefusingModel:
    """The demo team, except the ``tests`` specialist's call fails as given."""

    def __init__(self, failure: Exception) -> None:
        self.failure = failure
        self.requests: list[str] = []

    async def decide(self, request: ModelRequest) -> ModelResult:
        task_id = assignment(request)
        self.requests.append(task_id)
        if task_id == "tests":
            raise self.failure
        return ModelResult(
            action=FinalAction(content=demo_report(task_id).model_dump_json()),
            usage=ModelUsage(input_tokens=20, output_tokens=10),
        )


def _context() -> RunContext:
    return RunContext(
        application_id="collaboration",
        application_version="demo-v1",
        policy_version="collaboration-readonly-v1",
        run_id="refusal",
        session_id="session",
        correlation_id="correlation",
        actor=Actor(id="host"),
        scope=ResourceScope(tenant_id="local"),
    )


async def _run(root: Path, model: RefusingModel, *, parallel: bool):  # type: ignore[no-untyped-def]
    budget = RunBudget(
        max_steps=3, max_tool_calls=0, max_input_tokens=9_000, max_output_tokens=1_500
    )

    def team(**extra: object) -> Collaboration:
        return Collaboration(
            plan=demo_plan(),
            model_factory=lambda: model,
            budget=budget,
            state_dir=root / "state",
            authorize_run=lambda ctx: True,
            validate_resources=lambda ctx, versions: True,
            **extra,  # type: ignore[arg-type]
        )

    if not parallel:
        group = team()
        return await group.harness.run(context=_context(), task=group.plan.goal), group, None
    with ModelDispatcher(state_dir=root / "capacity", provider_id="test", capacity=2) as dispatcher:
        group = team(
            execution=ParallelExecution(max_in_flight=2, model_concurrency_safe=True),
            dispatcher=dispatcher,
        )
        result = await group.harness.run(context=_context(), task=group.plan.goal)
        return result, group, dispatcher.unresolved


@pytest.mark.parametrize("parallel", [False, True])
async def test_a_refused_specialist_fails_with_zero_usage_and_released_reservation(
    tmp_path: Path, parallel: bool
) -> None:
    model = RefusingModel(ModelRequestRejected(402))
    result, group, unresolved = await _run(tmp_path, model, parallel=parallel)
    assert result.status is RunStatus.FAILED
    assert not (result.error or "").startswith("needs_reconciliation")
    tests = next(d for d in group.inspect(_context()).delegations if d.task_id == "tests")
    assert tests.status == "failed" and tests.usage == Usage() and tests.reservation == Usage()
    assert tests.error == "provider rejected the request (HTTP 402)"
    assert "synthesis" not in model.requests  # a failed specialist still blocks the coordinator
    if parallel:
        assert unresolved == ()  # the capacity slot was confirmed ended, not held
    await group.harness.resume(context=_context())
    assert model.requests.count("tests") == 1  # a settled refusal is never re-sent
    traces = list((tmp_path / "state").rglob("*.jsonl"))
    assert traces and all(verify_trace(path).valid for path in traces)


@pytest.mark.parametrize("parallel", [False, True])
async def test_an_uncertain_failure_still_waits_for_reconciliation(
    tmp_path: Path, parallel: bool
) -> None:
    model = RefusingModel(TimeoutError("synthetic provider response loss"))
    result, group, _ = await _run(tmp_path, model, parallel=parallel)
    assert result.error == "needs_reconciliation:specialist.tests"
    tests = next(d for d in group.inspect(_context()).delegations if d.task_id == "tests")
    assert tests.status == "unknown" and tests.reservation == demo_plan().agents[1].max_usage
