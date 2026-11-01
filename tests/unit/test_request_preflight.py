"""Actual request-boundary admission, transport identity and recovery regressions."""

import hashlib
import json
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from forgeharness.context.assembly import ContextAssembler
from forgeharness.context.request_budget import estimate_request
from forgeharness.domain.models import FinalAction, Message, MessageRole, ModelResult, RunStatus
from forgeharness.models.base import Model, ModelRequest
from forgeharness.models.openai_compatible import OpenAICompatibleConfig, OpenAICompatibleModel
from forgeharness.models.prepared import (
    PreparedModelCall,
    canonical_json,
    decide_prepared_model,
    prepare_model_call,
)
from forgeharness.models.scripted import ScriptedModel
from forgeharness.observability.trace import InMemoryTrace
from forgeharness.runtime.budget import RunBudget
from forgeharness.runtime.loop import AgentRuntime
from forgeharness.runtime.plan_execute import PlanExecuteRuntime
from forgeharness.tools.base import SchemaSource, ToolSpec
from forgeharness.tools.builtin import EchoTool
from forgeharness.tools.dispatcher import ToolDispatcher
from forgeharness.tools.policy import RiskBasedPolicy
from forgeharness.tools.registry import ToolRegistry


def request() -> ModelRequest:
    return ModelRequest(
        task_id="budget-run",
        messages=(Message(role=MessageRole.USER, content="read"),),
        tools=(EchoTool().spec,),
    )


def runtime(
    model: Model, budget: RunBudget, trace: InMemoryTrace, registry: ToolRegistry | None = None
) -> AgentRuntime:
    registry = registry or ToolRegistry()
    return AgentRuntime(
        model=model,
        budget=budget,
        trace=trace,
        registry=registry,
        dispatcher=ToolDispatcher(registry),
        policy=RiskBasedPolicy(),
    )


class WideTool(EchoTool):
    @property
    def spec(self) -> ToolSpec:
        spec = super().spec
        return spec.model_copy(
            update={
                "name": "wide",
                "input_schema": {**spec.input_schema, "description": "z" * 8_000},
                "schema_source": SchemaSource.EXTERNAL_JSON_SCHEMA,
            }
        )


def test_legacy_model_snapshot_is_detached_and_unknown_costs_are_explicit() -> None:
    raw = request()
    prepared = prepare_model_call(ScriptedModel([]), raw)
    raw.tools[0].input_schema["description"] = "changed after preparation"
    assert "changed after preparation" not in prepared.payload_json
    estimate = estimate_request(prepared, RunBudget())
    assert estimate.tool_tokens > 0 and estimate.message_tokens > 0
    assert not estimate.provider_window_known
    assert not estimate.provider_overhead_known
    assert not estimate.provider_output_limit_known
    assert prepared.payload["tools"][0]["name"] == "echo"


@pytest.mark.parametrize("payload", ["[]", '{"b": 1}', '{"b":NaN}'])
def test_prepared_payload_rejects_noncanonical_or_nonobject_json(payload: str) -> None:
    with pytest.raises((ValueError, ValidationError)):
        PreparedModelCall(request_json=request().model_dump_json(), payload_json=payload)


def test_budget_counts_options_reservations_and_the_smaller_declared_window() -> None:
    prepared = PreparedModelCall(
        request_json=request().model_dump_json(),
        payload_json=canonical_json({"messages": [], "tools": [], "custom": "x" * 500}),
        format="custom-fixture-v1",
        output_limit=120,
        context_window=250,
        provider_overhead_tokens=7,
    )
    estimate = estimate_request(
        prepared,
        RunBudget(max_context_tokens=2_000, reserve_output_tokens=80, request_overhead_tokens=5),
    )
    assert estimate.option_tokens >= 125
    assert estimate.limit == 250
    assert estimate.overhead_tokens == 12
    assert estimate.reserved_output_tokens == 120
    assert estimate.projected_tokens == estimate.input_tokens + 132
    assert not estimate.admitted
    assert estimate.provider_window_known and estimate.provider_overhead_known


async def test_large_tool_schema_is_rejected_before_any_model_call(tmp_path: Path) -> None:
    model = ScriptedModel([ModelResult(action=FinalAction(content="unused"))])
    registry = ToolRegistry()
    registry.register(WideTool())
    trace = InMemoryTrace("budget-run")
    result = await runtime(model, RunBudget(max_context_tokens=500), trace, registry).run(
        task_id="budget-run", task="read", workspace=tmp_path
    )
    assert result.status is RunStatus.FAILED
    assert model.requests == [] and result.usage.steps == 0
    rejected = [e for e in trace.events if e.type == "context.preflight_rejected"]
    assert rejected[0].payload["preflight"]["tool_tokens"] > 2_000
    assert not any(e.type == "model.request" for e in trace.events)


async def test_tool_disclosure_is_rebudgeted_on_the_next_model_step(tmp_path: Path) -> None:
    from forgeharness.domain.models import ToolAction, ToolCall

    registry = ToolRegistry()
    registry.register(EchoTool())

    class RevealModel(ScriptedModel):
        async def decide(self, request: ModelRequest) -> ModelResult:
            result = await super().decide(request)
            registry.register(WideTool())
            return result

    model = RevealModel(
        [
            ModelResult(
                action=ToolAction(call=ToolCall(id="r", name="echo", arguments={"text": "ok"}))
            )
        ]
    )
    result = await runtime(
        model, RunBudget(max_context_tokens=500), InMemoryTrace("budget-run"), registry
    ).run(task_id="budget-run", task="read", workspace=tmp_path)
    assert result.status is RunStatus.FAILED
    assert len(model.requests) == 1 and result.usage.steps == 1
    assert result.usage.tool_calls == 1


async def test_output_reservation_alone_can_prevent_dispatch(tmp_path: Path) -> None:
    model = ScriptedModel([ModelResult(action=FinalAction(content="unused"))])
    result = await runtime(
        model, RunBudget(max_context_tokens=100, reserve_output_tokens=100), InMemoryTrace("t")
    ).run(task_id="t", task="read", workspace=tmp_path)
    assert result.status is RunStatus.FAILED
    assert model.requests == []


async def test_model_preparation_cannot_rewrite_runtime_messages(tmp_path: Path) -> None:
    class Rewriter(ScriptedModel):
        def prepare_request(self, raw: ModelRequest) -> PreparedModelCall:
            changed = raw.model_copy(update={"task_id": "different"})
            return PreparedModelCall(
                request_json=changed.model_dump_json(),
                payload_json=canonical_json(changed.model_dump(mode="json")),
            )

        async def decide_prepared(self, prepared: PreparedModelCall) -> ModelResult:
            return await self.decide(prepared.request)

    model = Rewriter([])
    trace = InMemoryTrace("t")
    result = await runtime(model, RunBudget(), trace).run(
        task_id="t", task="read", workspace=tmp_path
    )
    assert result.status is RunStatus.FAILED
    assert result.error == "model request preparation failed"
    assert trace.events[-1].payload["error_type"] == "model_preparation_failed"
    assert model.requests == []


async def test_admitted_http_body_is_identical_to_the_traced_payload(tmp_path: Path) -> None:
    captured: list[httpx.Request] = []

    async def handler(raw: httpx.Request) -> httpx.Response:
        captured.append(raw)
        return httpx.Response(200, json={"choices": [{"message": {"content": "done"}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        model = OpenAICompatibleModel(
            OpenAICompatibleConfig(
                base_url="https://model.invalid/v1",
                api_key="credential-never-in-trace",
                model="fixture",
                max_tokens=64,
                context_window_tokens=2_048,
                provider_overhead_tokens=9,
            ),
            client=client,
        )
        trace = InMemoryTrace("t")
        result = await runtime(model, RunBudget(reserve_output_tokens=32), trace).run(
            task_id="t", task="read", workspace=tmp_path
        )
    assert result.status is RunStatus.SUCCEEDED
    event = next(e for e in trace.events if e.type == "model.request")
    sent = captured[0].content
    assert sent == canonical_json(event.payload["prepared_payload"]).encode()
    assert hashlib.sha256(sent).hexdigest() == event.payload["preflight"]["payload_sha256"]
    assert event.payload["preflight"]["reserved_output_tokens"] == 64
    assert event.payload["preflight"]["limit"] == 2_048
    assert captured[0].headers["content-type"] == "application/json"
    assert "credential-never-in-trace" not in json.dumps([e.payload for e in trace.events])


async def test_prepared_http_dispatch_does_not_regenerate_the_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[bytes] = []

    def handler(raw: httpx.Request) -> httpx.Response:
        captured.append(raw.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "done"}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        model = OpenAICompatibleModel(
            OpenAICompatibleConfig(
                base_url="https://model.invalid", api_key="fixture", model="test"
            ),
            client=client,
        )
        prepared = prepare_model_call(model, request())

        def forbidden(*args: object) -> None:
            raise AssertionError("payload regenerated after preflight")

        monkeypatch.setattr(model, "_request_body", forbidden)
        await decide_prepared_model(model, prepared)
    assert captured == [prepared.payload_json.encode()]


async def test_planner_context_overflow_never_calls_either_model(tmp_path: Path) -> None:
    planner, worker = ScriptedModel([]), ScriptedModel([])
    trace = InMemoryTrace("t")
    result = await PlanExecuteRuntime(
        planner=planner,
        executor=runtime(worker, RunBudget(max_context_tokens=10), trace),
        trace=trace,
    ).run(task_id="t", task="read", workspace=tmp_path)
    assert result.status is RunStatus.FAILED
    assert planner.requests == worker.requests == []
    assert result.usage.steps == 0


def test_exact_estimated_boundary_and_host_reservation_win() -> None:
    prepared = prepare_model_call(ScriptedModel([]), request())
    cost = estimate_request(prepared, RunBudget()).input_tokens
    assert estimate_request(prepared, RunBudget(max_context_tokens=cost)).admitted
    assert not estimate_request(prepared, RunBudget(max_context_tokens=cost - 1)).admitted
    assert not estimate_request(
        prepared, RunBudget(max_context_tokens=cost, request_overhead_tokens=1)
    ).admitted


def test_message_assembly_rejects_invalid_reservation() -> None:
    with pytest.raises(ValueError, match="reserved_tokens"):
        ContextAssembler(max_tokens=10).assemble([], reserved_tokens=-1)


@pytest.mark.parametrize("kind", ["changed-content", "undeclared-tool", "understated-output"])
def test_chat_profile_cannot_hide_input_changes_or_output_reservation(kind: str) -> None:
    from forgeharness.models.prepared import chat_inputs

    raw = request() if kind != "undeclared-tool" else request().model_copy(update={"tools": ()})
    body = chat_inputs(raw)
    if kind == "changed-content":
        body["messages"] = [{"role": "user", "content": "rewritten"}]
    elif kind == "undeclared-tool":
        body["tools"] = [{"type": "function", "function": {"name": "surprise"}}]
    else:
        body["max_tokens"] = 1_000
    with pytest.raises(ValueError):
        PreparedModelCall(
            request_json=raw.model_dump_json(),
            payload_json=canonical_json(body),
            format="chat-completions-v1",
            output_limit=10 if kind == "understated-output" else None,
        )


async def test_sdk_rejects_credentials_inside_a_custom_serialized_payload(tmp_path: Path) -> None:
    from forgeharness.sdk import (
        Actor,
        ApplicationSpec,
        Harness,
        NoopVerifier,
        ResourceScope,
        RunContext,
    )
    from forgeharness.tools.policy import PolicyDecision, PolicyDecisionType

    class Policy:
        def evaluate(self, request: object) -> PolicyDecision:
            return PolicyDecision(type=PolicyDecisionType.DENY, reason="no tools")

        def can_approve(self, **kwargs: object) -> bool:
            return False

    class SecretPayloadModel(ScriptedModel):
        def prepare_request(self, raw: ModelRequest) -> PreparedModelCall:
            return PreparedModelCall(
                request_json=raw.model_dump_json(),
                payload_json=canonical_json({"api_key": "fixture-private-value"}),
                format="custom-v1",
            )

        async def decide_prepared(self, prepared: PreparedModelCall) -> ModelResult:
            return await self.decide(prepared.request)

    model = SecretPayloadModel([])
    app = ApplicationSpec(
        id="fixture",
        version="1",
        policy_version="1",
        model_factory=lambda: model,
        tools=(),
        policy=Policy(),
        verifier=NoopVerifier(),
    )
    ctx = RunContext(
        application_id="fixture",
        application_version="1",
        policy_version="1",
        run_id="r",
        session_id="s",
        correlation_id="c",
        actor=Actor(id="user"),
        scope=ResourceScope(tenant_id="shop"),
    )
    harness = Harness(app, state_dir=tmp_path)
    result = await harness.run(context=ctx, task="read")
    assert result.status is RunStatus.FAILED and model.requests == []
    assert "fixture-private-value" not in harness.trace_path(ctx).read_text()
