"""Capability composition contracts: order, uniqueness and Harness integration."""

from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict

from forgeharness.sdk import (
    Actor,
    ApplicationSpec,
    AuthorizationRequest,
    Capability,
    ContextMaterial,
    CredentialMask,
    FinalAction,
    Harness,
    ModelResult,
    NoopVerifier,
    PolicyDecision,
    PolicyDecisionType,
    ResourceScope,
    RiskLevel,
    RunBudget,
    RunContext,
    RunStatus,
    ScriptedModel,
    ToolAction,
    ToolCall,
    ToolContext,
    ToolEffectClass,
    ToolOutput,
    ToolSpec,
    compose_application,
)


class Inputs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = "data"


class EchoTool:
    input_model = Inputs

    def __init__(self, name: str = "echo") -> None:
        self.calls = 0
        self.spec = ToolSpec(
            name=name,
            description="echo",
            input_schema=Inputs.model_json_schema(),
            risk=RiskLevel.READ,
            effect_class=ToolEffectClass.READ_ONLY,
        )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolOutput:
        del context
        self.calls += 1
        return ToolOutput(ok=True, content=str(arguments.model_dump()["text"]))


class AllowAllPolicy:
    def evaluate(self, request: AuthorizationRequest) -> PolicyDecision:
        del request
        return PolicyDecision(type=PolicyDecisionType.ALLOW, reason="test allow")

    def can_approve(self, *, requester: RunContext, owner: RunContext, call: ToolCall) -> bool:
        del requester, owner, call
        return False


class StaticProvider:
    def __init__(self, name: str) -> None:
        self.name = name

    async def provide(self, *, task: str, context: RunContext) -> tuple[ContextMaterial, ...]:
        del task, context
        return ()


def context() -> RunContext:
    return RunContext(
        application_id="composed",
        application_version="1",
        policy_version="1",
        run_id="run-1",
        session_id="session-1",
        actor=Actor(id="owner", roles=("use",)),
        scope=ResourceScope(tenant_id="tenant-a", resources=("case:1",)),
        correlation_id="correlation-1",
    )


def compose(capabilities: tuple[Capability, ...], **changes: object) -> ApplicationSpec:
    values: dict[str, object] = {
        "id": "composed",
        "version": "1",
        "policy_version": "1",
        "model_factory": lambda: ScriptedModel([ModelResult(action=FinalAction(content="ok"))]),
        "capabilities": capabilities,
        "policy": AllowAllPolicy(),
        "verifier": NoopVerifier(),
    }
    values.update(changes)
    return compose_application(**values)  # type: ignore[arg-type]


def test_capabilities_compose_in_declaration_order() -> None:
    first_tool = EchoTool("first")
    second_tool = EchoTool("second")
    root_provider = StaticProvider("root")
    first_provider = StaticProvider("first")
    second_provider = StaticProvider("second")

    app = compose(
        (
            Capability(
                "first",
                tools=(first_tool,),
                instructions=("first instruction", "first detail"),
                context_providers=(first_provider,),
            ),
            Capability(
                "second",
                tools=(second_tool,),
                instructions=("second instruction",),
                context_providers=(second_provider,),
            ),
        ),
        instructions=("root instruction",),
        context_providers=(root_provider,),
    )

    assert app.tools == (first_tool, second_tool)
    assert app.instructions == (
        "root instruction",
        "first instruction",
        "first detail",
        "second instruction",
    )
    assert app.context_providers == (root_provider, first_provider, second_provider)
    assert isinstance(app, ApplicationSpec)


def test_duplicate_tool_names_across_capabilities_fail() -> None:
    with pytest.raises(ValueError, match="duplicate tool name"):
        compose(
            (
                Capability("first", tools=(EchoTool("same"),)),
                Capability("second", tools=(EchoTool("same"),)),
            )
        )


def test_duplicate_tool_names_inside_one_capability_fail() -> None:
    with pytest.raises(ValueError, match="duplicate tool name"):
        Capability("duplicate", tools=(EchoTool("same"), EchoTool("same")))


def test_duplicate_capability_names_fail() -> None:
    with pytest.raises(ValueError, match="duplicate capability name"):
        compose(
            (
                Capability("same", tools=(EchoTool("first"),)),
                Capability("same", tools=(EchoTool("second"),)),
            )
        )


def test_empty_capability_fails() -> None:
    with pytest.raises(ValueError, match="contributes nothing"):
        Capability("empty")


def test_composition_requires_a_capability() -> None:
    with pytest.raises(ValueError, match="at least one capability"):
        compose(())


def test_application_governance_inputs_are_forwarded() -> None:
    budget = RunBudget(max_steps=3)
    transform = CredentialMask()

    def authorize(_: RunContext) -> bool:
        return True

    app = compose(
        (Capability("instructions", instructions=("Only use supplied evidence.",)),),
        budget=budget,
        evidence_transform=transform,
        authorize_run=authorize,
    )

    assert app.budget is budget
    assert app.evidence_transform is transform
    assert app.authorize_run is authorize


async def test_composed_application_runs_through_harness(tmp_path: Path) -> None:
    tool = EchoTool()
    app = compose(
        (
            Capability(
                "echo",
                tools=(tool,),
                instructions=("Use the echo tool.",),
            ),
        ),
        model_factory=lambda: ScriptedModel(
            [
                ModelResult(
                    action=ToolAction(
                        call=ToolCall(id="call-1", name="echo", arguments={"text": "hi"})
                    )
                ),
                ModelResult(action=FinalAction(content="done")),
            ]
        ),
    )
    harness = Harness(app, state_dir=tmp_path)

    result = await harness.run(context=context(), task="echo hi")

    assert result.status is RunStatus.SUCCEEDED
    assert tool.calls == 1
    assert result.tool_evidence[0].content == "hi"

    trace_text = harness.trace_path(context()).read_text()
    assert "application.composition" in trace_text
    assert app.composition is not None
    assert app.composition.digest in trace_text


def test_composition_is_recorded_and_deterministic() -> None:
    def build() -> ApplicationSpec:
        return compose(
            (
                Capability("alpha", tools=(EchoTool("shared"),), instructions=("a",)),
                Capability("beta", instructions=("b",)),
            )
        )

    first, second = build(), build()
    reordered = compose(
        (
            Capability("beta", instructions=("b",)),
            Capability("alpha", tools=(EchoTool("shared"),), instructions=("a",)),
        )
    )
    renamed = compose(
        (
            Capability("alpha", tools=(EchoTool("shared"),), instructions=("a!",)),
            Capability("beta", instructions=("b",)),
        )
    )

    assert first.composition is not None
    assert first.composition.capability_names == ("alpha", "beta")
    assert first.composition.digest == second.composition.digest
    assert reordered.composition is not None
    assert reordered.composition.digest != first.composition.digest
    assert renamed.composition is not None
    assert renamed.composition.digest != first.composition.digest


def test_direct_application_spec_has_no_composition() -> None:
    spec = ApplicationSpec(
        id="direct",
        version="1",
        policy_version="1",
        model_factory=lambda: ScriptedModel([ModelResult(action=FinalAction(content="ok"))]),
        tools=(),
        policy=AllowAllPolicy(),
        verifier=NoopVerifier(),
    )

    assert spec.composition is None
