"""Prompt-cache planning: prefix choice, marker rendering, and the bill it protects."""

from __future__ import annotations

import json

import httpx
import pytest

from forgeharness.domain.models import (
    FinalAction,
    Message,
    MessageRole,
    ModelResult,
    ModelUsage,
    ToolAction,
    ToolCall,
    Usage,
)
from forgeharness.models.base import ModelRequest
from forgeharness.models.caching import (
    CacheMode,
    CachePlan,
    cache_report,
    plan_cache,
    planner_for,
    render_payload,
)
from forgeharness.models.openai_compatible import (
    OpenAICompatibleConfig,
    OpenAICompatibleModel,
)
from forgeharness.models.prepared import PreparedModelCall, canonical_json, chat_inputs
from forgeharness.observability.trace import InMemoryTrace
from forgeharness.runtime.budget import RunBudget
from forgeharness.runtime.model_call import dispatch_model_once
from forgeharness.tools.builtin import EchoTool
from forgeharness.tools.registry import ToolRegistry

SYSTEM = Message(role=MessageRole.SYSTEM, content="You are a harness executor.")
POLICY = Message(role=MessageRole.SYSTEM, content="Tool policy: read before write.")
TASK = Message(role=MessageRole.USER, content="echo hello")


def make_request(*, extra: tuple[Message, ...] = (), tools: tuple | None = None) -> ModelRequest:
    return ModelRequest(
        task_id="task-1",
        messages=(SYSTEM, POLICY, TASK, *extra),
        tools=(EchoTool().spec,) if tools is None else tools,
    )


def prepare(request: ModelRequest) -> PreparedModelCall:
    """Build a prepared call exactly as the adapter does, without a transport."""
    return PreparedModelCall(
        request_json=canonical_json(request.model_dump(mode="json")),
        payload_json=canonical_json(
            {"model": "test-model", "temperature": 0.0, **chat_inputs(request)}
        ),
        format="chat-completions-v1",
    )


def test_off_mode_reports_the_prefix_without_claiming_a_cache() -> None:
    plan = plan_cache(make_request(), mode=CacheMode.OFF)

    assert plan.mode is CacheMode.OFF
    assert plan.cacheable is False
    assert plan.directives == ()
    assert plan.prefix_messages == 2
    assert plan.prefix_tokens > 0


def test_explicit_mode_marks_the_end_of_tools_and_the_end_of_the_system_block() -> None:
    plan = plan_cache(make_request(), mode=CacheMode.EXPLICIT)

    assert plan.cacheable is True
    assert [(d.target, d.message_index) for d in plan.directives] == [
        ("tools", None),
        ("system", 1),
    ]
    assert plan.breakpoints == 2
    assert plan.worth_marking is True


def test_prefix_mode_leaves_the_body_alone_because_the_provider_caches_implicitly() -> None:
    request = make_request()
    plan = plan_cache(request, mode=CacheMode.PREFIX)

    assert plan.cacheable is True
    assert plan.directives == ()
    assert render_payload(prepare(request), plan) == prepare(request).payload_json


def test_a_request_with_no_tools_and_no_system_message_has_nothing_to_mark() -> None:
    bare = ModelRequest(
        task_id="task-1",
        messages=(Message(role=MessageRole.USER, content="hello"),),
        tools=(),
    )
    plan = plan_cache(bare, mode=CacheMode.EXPLICIT)

    assert plan.prefix_messages == 0
    assert plan.directives == ()
    assert plan.cacheable is True
    assert plan.worth_marking is False


def test_rendering_marks_the_last_tool_and_upgrades_the_marked_message() -> None:
    request = make_request()
    prepared = prepare(request)

    body = json.loads(render_payload(prepared, plan_cache(request, mode=CacheMode.EXPLICIT)))

    assert body["tools"][-1]["cache_control"] == {"type": "ephemeral"}
    marked = body["messages"][1]
    assert marked["content"] == [
        {"type": "text", "text": POLICY.content, "cache_control": {"type": "ephemeral"}}
    ]
    # The unmarked system message keeps the plain string form.
    assert body["messages"][0]["content"] == SYSTEM.content


def test_rendering_never_rewrites_the_recorded_payload() -> None:
    request = make_request()
    prepared = prepare(request)
    before = prepared.payload_json

    render_payload(prepared, plan_cache(request, mode=CacheMode.EXPLICIT))

    # Rendering a candidate is pure. An adapter must then freeze that result in
    # the explicit-cache prepared format before admission; dispatch cannot render.
    assert prepared.payload_json == before
    assert "cache_control" not in before


async def test_explicit_cache_is_admitted_before_dispatch_and_never_rewritten() -> None:
    from forgeharness.context.request_budget import estimate_request

    captured = []
    model = build_model(
        {"choices": [{"message": {"content": "done"}}]}, captured, cache_mode=CacheMode.EXPLICIT
    )
    try:
        prepared = model.prepare_request(make_request())
        estimate = estimate_request(prepared, RunBudget())
        assert prepared.format == "chat-completions-cache-explicit-v1"
        assert prepared.payload["tools"][-1]["cache_control"] == {"type": "ephemeral"}
        assert estimate.payload_sha256 == prepared.payload_sha256
        plain_estimate = estimate_request(prepare(make_request()), RunBudget())
        assert estimate.input_tokens > plain_estimate.input_tokens
        # Even a trusted host changing adapter configuration after admission may
        # not change the already authorized wire snapshot.
        model._config = model._config.model_copy(update={"cache_mode": CacheMode.OFF})
        await model.decide_prepared(prepared)
        assert captured[0].content == prepared.payload_json.encode()
    finally:
        await model._client.aclose()


@pytest.mark.parametrize("mutation", ["missing", "changed_text", "changed_marker", "extra_marker"])
async def test_explicit_prepared_format_rejects_unbound_content_or_markers(mutation) -> None:
    model = build_model(
        {"choices": [{"message": {"content": "done"}}]}, [], cache_mode=CacheMode.EXPLICIT
    )
    try:
        prepared = model.prepare_request(make_request())
        body = prepared.payload
        if mutation == "missing":
            body["tools"][-1].pop("cache_control")
        elif mutation == "changed_text":
            body["messages"][1]["content"][0]["text"] = "not authorized"
        elif mutation == "changed_marker":
            body["tools"][-1]["cache_control"]["type"] = "1h"
        else:
            body["messages"][0]["cache_control"] = {"type": "ephemeral"}
        with pytest.raises(ValueError, match="prepared chat inputs"):
            PreparedModelCall.model_validate(
                {**prepared.model_dump(), "payload_json": canonical_json(body)}
            )
    finally:
        await model._client.aclose()


def test_the_prefix_survives_an_appended_turn() -> None:
    first = make_request()
    second = make_request(
        extra=(
            Message(
                role=MessageRole.ASSISTANT,
                content="",
                tool_calls=(ToolCall(id="call-1", name="echo", arguments={"text": "hello"}),),
            ),
            Message(
                role=MessageRole.TOOL,
                content="hello",
                tool_call_id="call-1",
                tool_name="echo",
            ),
            Message(role=MessageRole.USER, content="now do something else"),
        )
    )

    plan_a = plan_cache(first, mode=CacheMode.EXPLICIT)
    plan_b = plan_cache(second, mode=CacheMode.EXPLICIT)
    body_a = json.loads(render_payload(prepare(first), plan_a))
    body_b = json.loads(render_payload(prepare(second), plan_b))

    assert plan_a.prefix_messages == plan_b.prefix_messages
    assert body_a["tools"] == body_b["tools"]
    assert body_a["messages"][:2] == body_b["messages"][:2]


def test_tool_order_is_registry_sorted_so_capability_trimming_cannot_scramble_it() -> None:
    registry = ToolRegistry()
    for tool in reversed(sorted((EchoTool(),), key=lambda t: t.spec.name)):
        registry.register(tool)
    echo_only = ModelRequest(task_id="t", messages=(SYSTEM, TASK), tools=registry.specs())

    assert [spec.name for spec in echo_only.tools] == [spec.name for spec in registry.specs()]
    assert [spec.name for spec in echo_only.tools] == sorted(spec.name for spec in echo_only.tools)


def test_cache_report_is_integer_only_so_traces_compare_exactly() -> None:
    report = cache_report(plan_cache(make_request(), mode=CacheMode.EXPLICIT))

    assert report["mode"] == "explicit"
    assert report["breakpoints"] == 2
    assert isinstance(report["cached_fraction_milli"], int)
    assert all(not isinstance(value, float) for value in report.values())


def test_planner_for_binds_the_mode_into_the_shape_the_runtime_probes() -> None:
    planner = planner_for(CacheMode.EXPLICIT)

    plan = planner(make_request())

    assert isinstance(plan, CachePlan)
    assert plan.breakpoints == 2


def build_model(
    payload: dict[str, object],
    captured: list[httpx.Request],
    *,
    cache_mode: CacheMode,
) -> OpenAICompatibleModel:
    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json=payload)

    return OpenAICompatibleModel(
        OpenAICompatibleConfig(
            base_url="https://model.example/v1/",
            api_key="secret",
            model="test-model",
            cache_mode=cache_mode,
        ),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


async def test_adapter_sends_cache_markers_only_when_asked() -> None:
    captured: list[httpx.Request] = []
    explicit = build_model(
        {"choices": [{"message": {"content": "done"}}]}, captured, cache_mode=CacheMode.EXPLICIT
    )
    off = build_model(
        {"choices": [{"message": {"content": "done"}}]}, captured, cache_mode=CacheMode.OFF
    )

    await explicit.decide(make_request())
    await off.decide(make_request())

    marked = json.loads(captured[0].content)
    plain = json.loads(captured[1].content)
    assert marked["tools"][-1]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in json.dumps(plain)


async def test_adapter_reports_cached_prompt_tokens_in_either_provider_spelling() -> None:
    captured: list[httpx.Request] = []
    openai_style = build_model(
        {
            "choices": [{"message": {"content": "done"}}],
            "usage": {
                "prompt_tokens": 900,
                "completion_tokens": 10,
                "prompt_tokens_details": {"cached_tokens": 800},
            },
        },
        captured,
        cache_mode=CacheMode.PREFIX,
    )
    anthropic_style = build_model(
        {
            "choices": [{"message": {"content": "done"}}],
            "usage": {
                "prompt_tokens": 40,
                "completion_tokens": 2,
                "cache_read_input_tokens": 760,
            },
        },
        captured,
        cache_mode=CacheMode.PREFIX,
    )

    await openai_style.decide(make_request())
    await anthropic_style.decide(make_request())

    assert openai_style.last_cache_read_tokens == 800
    # A gateway may report fewer prompt tokens *because* the rest were cached, so the
    # documented cache figure is authoritative and still surfaces.
    assert anthropic_style.last_cache_read_tokens == 760


async def test_adapter_defaults_to_off_so_an_unknown_server_never_sees_the_marker() -> None:
    captured: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "done"}}]})

    model = OpenAICompatibleModel(
        OpenAICompatibleConfig(base_url="https://model.example/v1/", api_key="s", model="m"),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    result = await model.decide(make_request())

    assert isinstance(result.action, ToolAction) is False
    assert "cache_control" not in captured[0].content.decode()
    assert model.cache_plan(make_request()).mode is CacheMode.OFF


class _CacheAwareStub:
    """A model that exposes cache bookkeeping, standing in for a real adapter."""

    def __init__(self, *, cached: int) -> None:
        self.last_cache_read_tokens = cached

    def cache_plan(self, request: ModelRequest) -> CachePlan:
        return plan_cache(request, mode=CacheMode.EXPLICIT)

    async def decide(self, request: ModelRequest) -> ModelResult:
        return ModelResult(
            action=FinalAction(content="done"),
            usage=ModelUsage(input_tokens=900, output_tokens=5),
        )


async def test_the_runtime_records_the_plan_and_the_observed_hit() -> None:
    trace = InMemoryTrace("task-1")
    usage = Usage()
    prepared = prepare(make_request())

    outcome = await dispatch_model_once(
        model=_CacheAwareStub(cached=800),
        prepared=prepared,
        budget=RunBudget(max_steps=2),
        trace=trace,
        usage=usage,
        phase="executor",
    )

    events = {event.type: event.payload for event in trace.events}
    assert events["model.cache"]["mode"] == "explicit"
    assert events["model.cache"]["breakpoints"] == 2
    assert events["model.cache.observed"]["cached_input_tokens"] == 800
    assert outcome.usage.input_tokens == 900


async def test_a_model_without_cache_concepts_records_no_cache_events() -> None:
    trace = InMemoryTrace("task-1")

    class _Plain:
        async def decide(self, request: ModelRequest) -> ModelResult:
            return ModelResult(
                action=FinalAction(content="done"),
                usage=ModelUsage(input_tokens=10, output_tokens=1),
            )

    await dispatch_model_once(
        model=_Plain(),
        prepared=prepare(make_request()),
        budget=RunBudget(max_steps=2),
        trace=trace,
        usage=Usage(),
        phase="executor",
    )

    assert [event.type for event in trace.events] == ["model.action"]


@pytest.mark.parametrize("mode", [CacheMode.OFF, CacheMode.PREFIX, CacheMode.EXPLICIT])
def test_planning_never_mutates_the_request(mode: CacheMode) -> None:
    request = make_request()
    before = request.model_dump(mode="json")

    plan_cache(request, mode=mode)

    assert request.model_dump(mode="json") == before
