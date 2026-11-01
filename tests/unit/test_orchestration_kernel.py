"""Custom orchestrators run through the governed kernel, not around it."""

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from pydantic import BaseModel, ConfigDict

from forgeharness.domain.models import Usage
from forgeharness.models.base import ModelProtocolError
from forgeharness.models.openai_compatible import OpenAICompatibleConfig, OpenAICompatibleModel
from forgeharness.sdk import (
    Actor,
    ApplicationSpec,
    AuthorizationRequest,
    Capability,
    FinalAction,
    Harness,
    KernelBudgetExceeded,
    KernelToolDenied,
    Message,
    MessageRole,
    ModelRequest,
    ModelResult,
    ModelUsage,
    NoopVerifier,
    OrchestrationError,
    PolicyDecision,
    PolicyDecisionType,
    ResourceScope,
    RiskLevel,
    RunBudget,
    RunContext,
    RunKernel,
    RunStatus,
    ScopeError,
    ToolCall,
    ToolContext,
    ToolEffectClass,
    ToolOutput,
    ToolSpec,
    VerificationRequest,
    VerificationResult,
    compose_application,
    verify_trace,
)


class Inputs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = "data"


class EchoTool:
    input_model = Inputs

    def __init__(self, name: str = "read", *, risk: RiskLevel = RiskLevel.READ) -> None:
        self.calls = 0
        self.spec = ToolSpec(
            name=name,
            description="echo",
            input_schema=Inputs.model_json_schema(),
            risk=risk,
            effect_class=(
                ToolEffectClass.READ_ONLY if risk is RiskLevel.READ else ToolEffectClass.IDEMPOTENT
            ),
        )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolOutput:
        del context
        self.calls += 1
        return ToolOutput(ok=True, content=str(arguments.model_dump()["text"]))


class EchoModel:
    async def decide(self, request: ModelRequest) -> ModelResult:
        text = request.messages[-1].content or ""
        return ModelResult(action=FinalAction(content=f"echo:{text}"))


class YieldingTool(EchoTool):
    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolOutput:
        await asyncio.sleep(0)
        return await super().execute(arguments, context)


class NestedUsageTool(EchoTool):
    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolOutput:
        output = await super().execute(arguments, context)
        return output.model_copy(
            update={"usage": Usage(steps=2, tool_calls=2, input_tokens=5, output_tokens=7)}
        )


class ConcurrentCalls:
    def __init__(self, *, models: bool = False) -> None:
        self.models = models
        self.outcomes: list[object] = []

    async def run(self, *, kernel: RunKernel, task: str) -> object:
        del task
        if self.models:
            calls = [kernel.call_model(phase=f"model-{i}") for i in range(2)]
        else:
            calls = [
                kernel.call_tool(ToolCall(id=f"read-{i}", name="read", arguments={}))
                for i in range(2)
            ]
        self.outcomes = list(await asyncio.gather(*calls, return_exceptions=True))
        return await kernel.finish(final=FinalAction(content="bounded"))


class AllowAllPolicy:
    def evaluate(self, request: AuthorizationRequest) -> PolicyDecision:
        del request
        return PolicyDecision(type=PolicyDecisionType.ALLOW, reason="test allow")

    def can_approve(self, *, requester: RunContext, owner: RunContext, call: ToolCall) -> bool:
        del requester, owner, call
        return False


class ApprovalPolicy(AllowAllPolicy):
    def evaluate(self, request: AuthorizationRequest) -> PolicyDecision:
        del request
        return PolicyDecision(
            type=PolicyDecisionType.REQUIRE_APPROVAL,
            reason="approval required by test policy",
        )


class RejectingVerifier:
    async def verify(self, request: VerificationRequest) -> VerificationResult:
        del request
        return VerificationResult(passed=False, reason="rejected by test")


def context() -> RunContext:
    return RunContext(
        application_id="orchestrated",
        application_version="1",
        policy_version="1",
        run_id="run-1",
        session_id="session-1",
        actor=Actor(id="owner", roles=("use",)),
        scope=ResourceScope(tenant_id="tenant-a", resources=("case:1",)),
        correlation_id="correlation-1",
    )


def application(
    *,
    tools: tuple[EchoTool, ...] = (),
    budget: RunBudget | None = None,
    verifier: object | None = None,
) -> ApplicationSpec:
    return compose_application(
        id="orchestrated",
        version="1",
        policy_version="1",
        model_factory=EchoModel,
        capabilities=(
            Capability(
                "core",
                tools=tools,  # type: ignore[arg-type]
                instructions=("Be concise.",),
            ),
        ),
        policy=AllowAllPolicy(),
        verifier=verifier or NoopVerifier(),  # type: ignore[arg-type]
        budget=budget,
    )


class TwoNodeDag:
    async def run(self, *, kernel: RunKernel, task: str) -> object:
        left, right = await asyncio.gather(
            kernel.call_model(
                messages=(Message(role=MessageRole.USER, content="left"),),
                phase="left",
            ),
            kernel.call_model(
                messages=(Message(role=MessageRole.USER, content="right"),),
                phase="right",
            ),
        )
        assert isinstance(left.action, FinalAction)
        assert isinstance(right.action, FinalAction)
        kernel.messages.append(Message(role=MessageRole.ASSISTANT, content=left.action.content))
        kernel.emit("dag.joined", {"nodes": ["left", "right"], "task": task})
        return await kernel.finish(
            final=FinalAction(content=f"{left.action.content}|{right.action.content}")
        )


async def test_per_call_tool_visibility_preserves_registry_trace_and_accounting(tmp_path):
    payloads = []

    def respond(request):
        payloads.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "model": "fixture",
                "choices": [{"message": {"content": "done"}}],
                "usage": {"prompt_tokens": 11, "completion_tokens": 3},
            },
        )

    class VisibilityFlow:
        async def run(self, *, kernel, task):
            del task
            await asyncio.gather(
                kernel.call_model(phase="final-only", include_tools=False),
                kernel.call_model(phase="default"),
            )
            result = await kernel.call_tool(ToolCall(id="read", name="read", arguments={}))
            assert result.ok
            return await kernel.finish(final=FinalAction(content="done"))

    tool = EchoTool()
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        model = OpenAICompatibleModel(
            OpenAICompatibleConfig(base_url="http://fixture/v1", api_key="test", model="fixture"),
            client=client,
        )
        controller = Harness(
            replace(application(tools=(tool,)), model_factory=lambda: model),
            state_dir=tmp_path,
            orchestrator=VisibilityFlow(),
        )
        result = await controller.run(context=context(), task="test")
    assert result.status is RunStatus.SUCCEEDED
    assert result.usage == Usage(steps=2, tool_calls=1, input_tokens=22, output_tokens=6)
    assert tool.calls == 1
    assert "tools" not in payloads[0] and "tool_choice" not in payloads[0]
    assert payloads[1]["tools"][0]["function"]["name"] == "read"
    assert payloads[1]["tool_choice"] == "auto"
    trace = controller.trace_path(context())
    assert verify_trace(trace).valid
    requests = {
        e["event"]["payload"]["phase"]: e["event"]["payload"]
        for e in map(json.loads, trace.read_text().splitlines())
        if e["event"]["type"] == "model.request"
    }
    assert requests["final-only"]["tools"] == []
    assert "tools" not in requests["final-only"]["prepared_payload"]
    assert requests["default"]["tools"][0]["name"] == "read"


@pytest.mark.parametrize("value", ["false", 0, None])
async def test_tool_visibility_rejects_non_boolean_without_provider_cost(tmp_path, value):
    calls = []

    class CountingModel:
        async def decide(self, request):
            calls.append(request)
            return ModelResult(action=FinalAction(content="unexpected"))

    class InvalidFlow:
        async def run(self, *, kernel, task):
            await kernel.call_model(include_tools=value)

    controller = Harness(
        replace(application(), model_factory=CountingModel),
        state_dir=tmp_path,
        orchestrator=InvalidFlow(),
    )
    result = await controller.run(context=context(), task="test")
    assert result.status is RunStatus.FAILED and "OrchestrationError" in result.error
    assert result.usage == Usage() and calls == []


async def test_omitting_model_tools_does_not_authorize_a_write(tmp_path):
    tool = EchoTool("write", risk=RiskLevel.WRITE)

    class HiddenWriteFlow:
        async def run(self, *, kernel, task):
            await kernel.call_model(include_tools=False)
            with pytest.raises(KernelToolDenied):
                await kernel.call_tool(ToolCall(id="write", name="write", arguments={}))
            return await kernel.finish(final=FinalAction(content="write denied"))

    result = await Harness(
        application(tools=(tool,)), state_dir=tmp_path, orchestrator=HiddenWriteFlow()
    ).run(context=context(), task="test")
    assert result.status is RunStatus.SUCCEEDED and result.usage.steps == 1
    assert result.usage.tool_calls == 0 and tool.calls == 0


class ReadToolOrchestrator:
    def __init__(self, tool_name: str) -> None:
        self.tool_name = tool_name
        self.output: ToolOutput | None = None

    async def run(self, *, kernel: RunKernel, task: str) -> object:
        del task
        self.output = await kernel.call_tool(
            ToolCall(id="call-1", name=self.tool_name, arguments={"text": "tool-data"})
        )
        return await kernel.finish(final=FinalAction(content=self.output.content))


class WriteToolOrchestrator:
    async def run(self, *, kernel: RunKernel, task: str) -> object:
        del task
        try:
            await kernel.call_tool(
                ToolCall(id="call-1", name="write", arguments={"text": "tool-data"})
            )
        except KernelToolDenied as exc:
            return await kernel.finish(status=RunStatus.FAILED, error=str(exc))
        return await kernel.finish(final=FinalAction(content="unexpected"))


class ApprovalToolOrchestrator:
    async def run(self, *, kernel: RunKernel, task: str) -> object:
        del task
        try:
            await kernel.call_tool(
                ToolCall(id="call-1", name="read", arguments={"text": "tool-data"})
            )
        except KernelToolDenied as exc:
            return await kernel.finish(status=RunStatus.FAILED, error=str(exc))
        return await kernel.finish(final=FinalAction(content="unexpected"))


class TwoCallOrchestrator:
    async def run(self, *, kernel: RunKernel, task: str) -> object:
        del task
        await kernel.call_model(
            messages=(Message(role=MessageRole.USER, content="first"),), phase="first"
        )
        try:
            await kernel.call_model(
                messages=(Message(role=MessageRole.USER, content="second"),), phase="second"
            )
        except KernelBudgetExceeded as exc:
            return await kernel.finish(status=RunStatus.FAILED, error=str(exc))
        return await kernel.finish(final=FinalAction(content="unexpected"))


class RejectingOrchestrator:
    async def run(self, *, kernel: RunKernel, task: str) -> object:
        del task
        result = await kernel.call_model(
            messages=(Message(role=MessageRole.USER, content="draft"),), phase="draft"
        )
        assert isinstance(result.action, FinalAction)
        return await kernel.finish(final=FinalAction(content=result.action.content))


class UnfinishedOrchestrator:
    async def run(self, *, kernel: RunKernel, task: str) -> object:
        del task
        await kernel.call_model(
            messages=(Message(role=MessageRole.USER, content="draft"),), phase="draft"
        )
        return kernel.checkpoint()


class RaisingOrchestrator:
    async def run(self, *, kernel: RunKernel, task: str) -> object:
        del kernel, task
        raise RuntimeError("boom")


@pytest.mark.parametrize("effect", [ToolEffectClass.IDEMPOTENT, ToolEffectClass.NON_IDEMPOTENT])
async def test_read_risk_cannot_hide_a_side_effect(tmp_path: Path, effect: ToolEffectClass) -> None:
    tool = EchoTool("read")
    tool.spec = tool.spec.model_copy(update={"effect_class": effect})
    harness = Harness(
        application(tools=(tool,)), state_dir=tmp_path, orchestrator=ApprovalToolOrchestrator()
    )
    result = await harness.run(context=context(), task="not a read")
    assert result.status is RunStatus.FAILED
    assert "read-only" in (result.error or "")
    assert tool.calls == 0


async def test_concurrent_tool_admission_does_not_overspend(tmp_path: Path) -> None:
    tool = YieldingTool("read")
    flow = ConcurrentCalls()
    harness = Harness(
        application(tools=(tool,), budget=RunBudget(max_tool_calls=1)),
        state_dir=tmp_path,
        orchestrator=flow,
    )
    result = await harness.run(context=context(), task="two competing reads")
    assert result.status is RunStatus.SUCCEEDED
    assert tool.calls == result.usage.tool_calls == 1
    assert sum(isinstance(item, KernelBudgetExceeded) for item in flow.outcomes) == 1
    assert len(result.tool_evidence) == 1
    assert verify_trace(harness.trace_path(context())).valid


async def test_concurrent_models_do_not_replace_usage_or_double_spend(tmp_path: Path) -> None:
    class YieldingModel(EchoModel):
        calls = 0

        async def decide(self, request: ModelRequest) -> ModelResult:
            self.calls += 1
            await asyncio.sleep(0)
            return await super().decide(request)

    model = YieldingModel()
    app = compose_application(
        id="orchestrated",
        version="1",
        policy_version="1",
        model_factory=lambda: model,
        capabilities=(Capability("core", instructions=("Check the shared budget.",)),),
        policy=AllowAllPolicy(),
        verifier=NoopVerifier(),
        budget=RunBudget(max_steps=1),
    )
    flow = ConcurrentCalls(models=True)
    harness = Harness(app, state_dir=tmp_path, orchestrator=flow)
    result = await harness.run(context=context(), task="two competing models")
    assert result.status is RunStatus.SUCCEEDED
    assert model.calls == result.usage.steps == 1
    assert sum(isinstance(item, KernelBudgetExceeded) for item in flow.outcomes) == 1


@pytest.mark.parametrize("limit, expected", [(10, RunStatus.SUCCEEDED), (1, RunStatus.FAILED)])
async def test_nested_tool_usage_is_charged_and_cannot_disappear(
    tmp_path: Path,
    limit: int,
    expected: RunStatus,
) -> None:
    tool = NestedUsageTool("read")
    harness = Harness(
        application(tools=(tool,), budget=RunBudget(max_tool_calls=limit)),
        state_dir=tmp_path,
        orchestrator=ReadToolOrchestrator("read"),
    )
    result = await harness.run(context=context(), task="nested allowance")
    assert result.status is expected
    assert tool.calls == 1
    assert result.usage == Usage(steps=2, tool_calls=3, input_tokens=5, output_tokens=7)
    assert len(result.tool_evidence) == 1


async def test_two_node_dag_uses_kernel_and_finishes(tmp_path: Path) -> None:
    harness = Harness(application(), state_dir=tmp_path, orchestrator=TwoNodeDag())

    result = await harness.run(context=context(), task="root")

    assert result.status is RunStatus.SUCCEEDED
    assert result.final_output == "echo:left|echo:right"
    assert result.usage.steps == 2
    assert harness.inspect(context()).status is RunStatus.SUCCEEDED

    trace = harness.trace_path(context())
    assert verify_trace(trace).valid
    events = [line for line in trace.read_text().splitlines() if "model.request" in line]
    assert len(events) == 2
    assert "dag.joined" in trace.read_text()


@pytest.mark.parametrize("nested", [False, True])
async def test_tool_token_usage_may_equal_its_limit_without_overspending(
    tmp_path: Path,
    nested: bool,
) -> None:
    tool = NestedUsageTool("read") if nested else EchoTool("read")
    budget = RunBudget(max_input_tokens=5 if nested else 0, max_output_tokens=7 if nested else 0)
    harness = Harness(
        application(tools=(tool,), budget=budget),
        state_dir=tmp_path,
        orchestrator=ReadToolOrchestrator("read"),
    )
    result = await harness.run(context=context(), task="tool-only budget boundary")
    assert result.status is RunStatus.SUCCEEDED
    assert result.usage.input_tokens == budget.max_input_tokens
    assert result.usage.output_tokens == budget.max_output_tokens
    assert tool.calls == 1


async def test_read_only_tool_call_is_policy_checked(tmp_path: Path) -> None:
    tool = EchoTool("read")
    orchestrator = ReadToolOrchestrator("read")
    harness = Harness(application(tools=(tool,)), state_dir=tmp_path, orchestrator=orchestrator)

    result = await harness.run(context=context(), task="use the tool")

    assert result.status is RunStatus.SUCCEEDED
    assert result.final_output == "tool-data"
    assert tool.calls == 1
    assert result.tool_evidence[0].content == "tool-data"
    trace = harness.trace_path(context()).read_text()
    assert "policy.decided" in trace
    assert "tool.completed" in trace


async def test_write_tool_is_rejected_before_execution(tmp_path: Path) -> None:
    tool = EchoTool("write", risk=RiskLevel.WRITE)
    harness = Harness(
        application(tools=(tool,)),
        state_dir=tmp_path,
        orchestrator=WriteToolOrchestrator(),
    )

    result = await harness.run(context=context(), task="write")

    assert result.status is RunStatus.FAILED
    assert result.error is not None
    assert "read-only" in result.error
    assert tool.calls == 0


async def test_approval_required_tool_is_denied(tmp_path: Path) -> None:
    tool = EchoTool("read")
    spec = compose_application(
        id="orchestrated",
        version="1",
        policy_version="1",
        model_factory=EchoModel,
        capabilities=(Capability("tools", tools=(tool,)),),
        policy=ApprovalPolicy(),
        verifier=NoopVerifier(),
    )
    harness = Harness(
        spec,
        state_dir=tmp_path,
        orchestrator=ApprovalToolOrchestrator(),
    )

    result = await harness.run(context=context(), task="approval")

    assert result.status is RunStatus.FAILED
    assert result.error is not None
    assert "tool not allowed" in result.error
    assert tool.calls == 0


async def test_step_budget_is_enforced(tmp_path: Path) -> None:
    harness = Harness(
        application(budget=RunBudget(max_steps=1)),
        state_dir=tmp_path,
        orchestrator=TwoCallOrchestrator(),
    )

    result = await harness.run(context=context(), task="two calls")

    assert result.status is RunStatus.FAILED
    assert result.error is not None
    assert "step budget" in result.error
    assert result.usage.steps == 1


async def test_verifier_rejection_fails_the_run(tmp_path: Path) -> None:
    harness = Harness(
        application(verifier=RejectingVerifier()),
        state_dir=tmp_path,
        orchestrator=RejectingOrchestrator(),
    )

    result = await harness.run(context=context(), task="draft")

    assert result.status is RunStatus.FAILED
    assert result.error is not None
    assert "verification rejected" in result.error
    assert "verification.rejected" in harness.trace_path(context()).read_text()


async def test_unfinished_orchestrator_fails_closed(tmp_path: Path) -> None:
    harness = Harness(
        application(),
        state_dir=tmp_path,
        orchestrator=UnfinishedOrchestrator(),
    )

    result = await harness.run(context=context(), task="draft")

    assert result.status is RunStatus.FAILED
    assert result.error is not None
    assert "without finishing" in result.error


async def test_orchestrator_exception_fails_closed(tmp_path: Path) -> None:
    harness = Harness(application(), state_dir=tmp_path, orchestrator=RaisingOrchestrator())

    result = await harness.run(context=context(), task="boom")

    assert result.status is RunStatus.FAILED
    assert result.error is not None
    assert "RuntimeError" in result.error
    assert "orchestrator.failed" in harness.trace_path(context()).read_text()


async def test_custom_orchestrator_resume_is_refused(tmp_path: Path) -> None:
    harness = Harness(application(), state_dir=tmp_path, orchestrator=TwoNodeDag())
    await harness.run(context=context(), task="root")

    with pytest.raises(ScopeError, match="do not support resume"):
        await harness.resume(context=context())
    with pytest.raises(ScopeError, match="do not support approval"):
        harness.approve(context=context(), approver=context())


class ViolatingModel:
    """Breaks the single-action protocol but reports what it consumed."""

    async def decide(self, request: ModelRequest) -> ModelResult:
        del request
        raise ModelProtocolError(
            "two tool calls",
            code="multiple_tool_calls_not_allowed",
            received_tool_calls=2,
            usage=ModelUsage(input_tokens=80, output_tokens=12),
            recoverable=True,
        )


class ViolationOrchestrator:
    async def run(self, *, kernel: RunKernel, task: str) -> object:
        del task
        try:
            await kernel.call_model(messages=(Message(role=MessageRole.USER, content="go"),))
        except OrchestrationError as exc:
            return await kernel.finish(status=RunStatus.FAILED, error=str(exc))
        return await kernel.finish(final=FinalAction(content="unexpected"))


async def test_protocol_violation_charges_rejected_tokens(tmp_path: Path) -> None:
    spec = compose_application(
        id="orchestrated",
        version="1",
        policy_version="1",
        model_factory=ViolatingModel,
        capabilities=(Capability("instructions", instructions=("go",)),),
        policy=AllowAllPolicy(),
        verifier=NoopVerifier(),
    )
    harness = Harness(spec, state_dir=tmp_path, orchestrator=ViolationOrchestrator())

    result = await harness.run(context=context(), task="violation")

    assert result.status is RunStatus.FAILED
    assert result.error is not None
    assert "model protocol violation: multiple_tool_calls_not_allowed" in result.error
    # The rejected inference really happened: one step and its tokens are charged,
    # identical to the ReAct path (test_replay asserts the same amounts).
    assert result.usage.steps == 1
    assert result.usage.input_tokens == 80
    assert result.usage.output_tokens == 12

    events = [
        json.loads(line)["event"] for line in harness.trace_path(context()).read_text().splitlines()
    ]
    failed = next(e for e in events if e["type"] == "model.failed")
    assert failed["payload"]["usage"] == {"input_tokens": 80, "output_tokens": 12}
    assert failed["payload"]["error_code"] == "multiple_tool_calls_not_allowed"
