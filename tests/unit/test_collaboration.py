"""Scoped specialist contracts, governed accounting and durable recovery."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from examples.collaboration_demo import assignment, demo_plan, demo_report
from forgeharness.runtime.structured import parse_structured_output, validate_output_schema
from forgeharness.sdk import (
    Actor,
    Collaboration,
    CollaborationAgent,
    CollaborationPlan,
    CollaborationVerifier,
    DurableNode,
    FinalAction,
    ModelRequest,
    ModelResult,
    ModelUsage,
    ReconciledNode,
    ResourceScope,
    RunBudget,
    RunContext,
    RunStatus,
    ScopeError,
    ToolAction,
    ToolCall,
    Usage,
    VerificationRequest,
    verify_trace,
)
from forgeharness.sdk.collaboration import _NoToolsPolicy

DEMO = Path(__file__).resolve().parents[2] / "examples/collaboration_demo.py"


class RecordingModel:
    def __init__(self, *, invalid: str = "", loss: bool = False) -> None:
        self.requests: list[ModelRequest] = []
        self.invalid = invalid
        self.loss = loss

    async def decide(self, request: ModelRequest) -> ModelResult:
        self.requests.append(request)
        task_id = assignment(request)
        if self.loss and task_id == "tests":
            raise TimeoutError("synthetic provider response loss")
        content = demo_report(task_id).model_dump_json()
        if task_id == "logic":
            if self.invalid == "citation":
                content = content.replace('"implementation"', '"test-contract"')
            elif self.invalid == "task":
                content = content.replace('"task_id":"logic"', '"task_id":"tests"')
            elif self.invalid == "json":
                content = "not JSON"
            elif self.invalid in {"empty", "duplicate"}:
                raw = demo_report(task_id).model_dump()
                if self.invalid == "empty":
                    raw.update(findings=(), unresolved=())
                else:
                    raw["findings"][0]["evidence_ids"] = ("implementation", "implementation")
                content = json.dumps(raw)
            elif self.invalid == "tool":
                return ModelResult(
                    action=ToolAction(call=ToolCall(id="bad", name="write_file", arguments={})),
                    usage=ModelUsage(input_tokens=20, output_tokens=10),
                )
        if task_id == "synthesis" and self.invalid == "coordinator":
            content = "not JSON"
        return ModelResult(
            action=FinalAction(content=content), usage=ModelUsage(input_tokens=20, output_tokens=10)
        )


def context(*, tenant: str = "local") -> RunContext:
    return RunContext(
        application_id="collaboration",
        application_version="demo-v1",
        policy_version="collaboration-readonly-v1",
        run_id="same-public-id",
        session_id="session",
        correlation_id="correlation",
        actor=Actor(id="host"),
        scope=ResourceScope(tenant_id=tenant),
    )


def team(root: Path, model: RecordingModel, **kwargs) -> Collaboration:
    return Collaboration(
        plan=kwargs.pop("plan", demo_plan()),
        model_factory=lambda: model,
        budget=kwargs.pop(
            "budget",
            RunBudget(
                max_steps=3, max_tool_calls=0, max_input_tokens=9_000, max_output_tokens=1_500
            ),
        ),
        state_dir=root / "state",
        authorize_run=kwargs.pop("authorize_run", lambda ctx: True),
        validate_resources=kwargs.pop("validate_resources", lambda ctx, versions: True),
        **kwargs,
    )


async def test_scoped_experts_synthesis_accounting_and_terminal_reuse(tmp_path: Path) -> None:
    model = RecordingModel()
    group = team(tmp_path, model)
    result = await group.harness.run(context=context(), task=group.plan.goal)
    assert result.status is RunStatus.SUCCEEDED
    assert result.usage == Usage(steps=3, input_tokens=60, output_tokens=30)
    assert [assignment(request) for request in model.requests] == ["logic", "tests", "synthesis"]
    assert all(request.tools == () for request in model.requests)
    logic = json.dumps(model.requests[0].model_dump())
    tests = json.dumps(model.requests[1].model_dump())
    assert "Read-only tasks must not dispatch" in logic
    assert "Test that a write tool request" not in logic
    assert "Test that a write tool request" in tests
    assert "Read-only tasks must not dispatch" not in tests
    assert "Untrusted dependency result" in json.dumps(model.requests[2].model_dump())
    assert "Read-only tasks must not dispatch" not in json.dumps(model.requests[2].model_dump())
    snapshot = group.inspect(context())
    assert len({node.delegation_id for node in snapshot.delegations}) == 3
    assert all(
        node.status == "completed" and node.reservation == Usage() and node.report is not None
        for node in snapshot.delegations
    )
    assert await team(tmp_path, model).harness.resume(context=context()) == result
    assert len(model.requests) == 3
    traces = list((tmp_path / "state").rglob("*.jsonl"))
    assert traces and all(verify_trace(path).valid for path in traces)


@pytest.mark.parametrize("invalid", ["citation", "task", "json", "tool", "empty", "duplicate"])
async def test_invalid_specialist_blocks_coordinator_keeps_usage_and_other_result(
    tmp_path: Path, invalid: str
) -> None:
    model = RecordingModel(invalid=invalid)
    group = team(tmp_path, model)
    result = await group.harness.run(context=context(), task=group.plan.goal)
    assert result.status is RunStatus.FAILED
    snapshot = group.inspect(context())
    assert [node.status for node in snapshot.delegations] == ["failed", "completed", "pending"]
    assert snapshot.delegations[0].error == "invalid structured model output"
    assert result.usage == Usage(steps=2, input_tokens=40, output_tokens=20)
    assert len(model.requests) == 2
    await group.harness.resume(context=context())
    assert len(model.requests) == 2


async def test_invalid_coordinator_is_failed_with_known_consumption(tmp_path: Path) -> None:
    model = RecordingModel(invalid="coordinator")
    group = team(tmp_path, model)
    result = await group.harness.run(context=context(), task=group.plan.goal)
    assert result.status is RunStatus.FAILED and result.final_output is None
    assert result.usage == Usage(steps=3, input_tokens=60, output_tokens=30)
    assert group.inspect(context()).delegations[-1].error == "invalid structured model output"


async def test_repeated_expert_has_independent_stable_delegations(tmp_path: Path) -> None:
    raw = demo_plan().model_dump()
    raw["tasks"][1]["agent_id"] = "logic"
    group = team(tmp_path, RecordingModel(), plan=CollaborationPlan.model_validate(raw))
    await group.harness.run(context=context(), task=group.plan.goal)
    first, second, _ = group.inspect(context()).delegations
    assert first.agent_id == second.agent_id == "logic"
    assert first.delegation_id != second.delegation_id
    assert first.report.task_id == "logic" and second.report.task_id == "tests"


@pytest.mark.parametrize(
    "change",
    [
        "agents",
        "evidence",
        "tasks",
        "unknown-agent",
        "unknown-coordinator",
        "unknown-evidence",
        "duplicate-evidence",
        "revision",
        "reserved-task",
    ],
)
def test_definition_validation(change: str) -> None:
    raw = demo_plan().model_dump()
    if change in {"agents", "evidence", "tasks"}:
        raw[change] = (*raw[change], raw[change][0])
    elif change == "unknown-agent":
        raw["tasks"][0]["agent_id"] = "missing"
    elif change == "unknown-coordinator":
        raw["coordinator_id"] = "missing"
    elif change == "unknown-evidence":
        raw["tasks"][0]["evidence_ids"] = ("missing",)
    elif change == "duplicate-evidence":
        raw["tasks"][0]["evidence_ids"] = ("implementation", "implementation")
    elif change == "revision":
        raw["evidence"][1]["resource_id"] = raw["evidence"][0]["resource_id"]
        raw["evidence"][1]["revision"] = "2"
    else:
        raw["tasks"][0]["id"] = "synthesis"
    with pytest.raises(ValidationError):
        CollaborationPlan.model_validate(raw)


@pytest.mark.parametrize(
    "usage",
    [
        Usage(),
        Usage(steps=2, input_tokens=10, output_tokens=10),
        Usage(steps=1, tool_calls=1, input_tokens=10, output_tokens=10),
        Usage(steps=1, input_tokens=0, output_tokens=10),
        Usage(steps=1, input_tokens=10, output_tokens=0),
    ],
)
def test_experts_cannot_expand_execution_scope(usage: Usage) -> None:
    with pytest.raises(ValidationError):
        CollaborationAgent(id="expert", version="1", instructions="Analyze.", max_usage=usage)


def test_aggregate_budget_rejected_before_persistence(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="root budget"):
        team(tmp_path, RecordingModel(), budget=RunBudget(max_steps=2))
    assert not (tmp_path / "state").exists()


@pytest.mark.parametrize("guard", ["authorize_run", "validate_resources"])
def test_missing_host_guard_rejected_before_persistence(tmp_path: Path, guard: str) -> None:
    with pytest.raises(ValueError, match="requires host"):
        team(tmp_path, RecordingModel(), **{guard: None})
    assert not (tmp_path / "state").exists()


async def test_unknown_response_reservation_and_validated_reconciliation(tmp_path: Path) -> None:
    model = RecordingModel(loss=True)

    async def resolver(ctx, node, stable_key):
        assert node.id == "specialist.tests" and stable_key
        return ReconciledNode(
            output=ModelResult(
                action=FinalAction(content=demo_report("tests").model_dump_json()),
                usage=ModelUsage(input_tokens=20, output_tokens=10),
            ).model_dump(),
            usage=Usage(steps=1, input_tokens=20, output_tokens=10),
            evidence_refs=("synthetic:existing-response",),
        )

    group = team(tmp_path, model, reconcile_node=resolver)
    result = await group.harness.run(context=context(), task=group.plan.goal)
    assert result.error == "needs_reconciliation:specialist.tests"
    before = group.inspect(context()).delegations[1]
    assert before.status == "unknown" and before.reservation == demo_plan().agents[1].max_usage
    await team(tmp_path, model, reconcile_node=resolver).harness.resume(context=context())
    assert len(model.requests) == 2
    result = await group.harness.reconcile(context=context(), node_id="specialist.tests")
    assert result.status is RunStatus.SUCCEEDED
    assert result.usage == Usage(steps=3, input_tokens=60, output_tokens=30)
    assert len(model.requests) == 3


async def test_reconciliation_cannot_bypass_result_schema(tmp_path: Path) -> None:
    model = RecordingModel(loss=True)

    async def resolver(ctx, node, stable_key):
        return ReconciledNode(
            output=ModelResult(action=FinalAction(content='{"bad":"report"}')).model_dump(),
            usage=Usage(steps=1, input_tokens=20, output_tokens=10),
            evidence_refs=("synthetic:existing-response",),
        )

    group = team(tmp_path, model, reconcile_node=resolver)
    await group.harness.run(context=context(), task=group.plan.goal)
    result = await group.harness.reconcile(context=context(), node_id="specialist.tests")
    assert result.status is RunStatus.FAILED
    assert result.usage == Usage(steps=2, input_tokens=40, output_tokens=20)
    assert group.inspect(context()).delegations[1].error == "invalid structured model output"
    assert len(model.requests) == 2


async def test_live_authorization_resource_guards_and_tenant_identity(tmp_path: Path) -> None:
    flags = {"authorized": True, "resources": True}
    versions_seen = []

    def resources(ctx, versions):
        versions_seen.append(versions)
        return flags["resources"]

    group = team(
        tmp_path,
        RecordingModel(),
        authorize_run=lambda ctx: flags["authorized"],
        validate_resources=resources,
    )
    await group.harness.run(context=context(), task=group.plan.goal)
    first = group.inspect(context())
    flags["authorized"] = False
    with pytest.raises(ScopeError):
        group.inspect(context())
    flags.update(authorized=True, resources=False)
    with pytest.raises(ScopeError, match="resources"):
        await group.harness.resume(context=context())
    flags["resources"] = True
    await group.harness.run(context=context(tenant="other"), task=group.plan.goal)
    second = group.inspect(context(tenant="other"))
    assert first.delegations[0].delegation_id != second.delegations[0].delegation_id
    assert versions_seen and all(
        value == {"synthetic:implementation": "1", "synthetic:tests": "1"}
        for value in versions_seen
    )


async def test_cancel_stops_admission_preserves_unknown_until_reconciled(tmp_path: Path) -> None:
    model = RecordingModel(loss=True)
    group = team(tmp_path, model)
    await group.harness.run(context=context(), task=group.plan.goal)
    assert group.harness.cancel(context())
    result = await group.harness.resume(context=context())
    assert result.error == "needs_reconciliation:specialist.tests"
    assert group.inspect(context()).cancel_requested
    assert len(model.requests) == 2


async def test_changed_active_schema_or_definition_fails_closed(tmp_path: Path) -> None:
    group = team(tmp_path, RecordingModel(loss=True))
    await group.harness.run(context=context(), task=group.plan.goal)
    graph = group.harness.orchestrator.graph
    graph.nodes[0].output_schema["properties"]["task_id"]["const"] = "other"
    with pytest.raises(ScopeError, match="identity changed"):
        group.inspect(context())


async def test_actual_overrun_is_retained(tmp_path: Path) -> None:
    class Overrun(RecordingModel):
        async def decide(self, request):
            result = await super().decide(request)
            return result.model_copy(
                update={"usage": ModelUsage(input_tokens=3_001, output_tokens=10)}
            )

    group = team(tmp_path, Overrun())
    result = await group.harness.run(context=context(), task=group.plan.goal)
    assert result.status is RunStatus.FAILED
    assert result.usage.input_tokens == 6_002
    assert all(node.status == "failed" for node in group.inspect(context()).delegations[:2])


async def test_verifier_handles_invalid_and_insufficient_reports() -> None:
    verifier = CollaborationVerifier(demo_plan())
    invalid = await verifier.verify(
        VerificationRequest(task_id="x", workspace=None, final=FinalAction(content="not JSON"))
    )
    assert not invalid.passed
    raw = demo_report("synthesis").model_dump()
    raw.update(findings=(), unresolved=("Insufficient evidence for a semantic conclusion.",))
    verdict = await verifier.verify(
        VerificationRequest(task_id="x", workspace=None, final=FinalAction(content=json.dumps(raw)))
    )
    assert verdict.passed and verdict.evidence == ()
    valid = await verifier.verify(
        VerificationRequest(
            task_id="x",
            workspace=None,
            final=FinalAction(content=demo_report("synthesis").model_dump_json()),
        )
    )
    assert valid.passed and len(valid.evidence) == 2
    assert "semantic support not assessed" in valid.reason


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "not-a-type"},
        {"$ref": "https://example.com/s"},
        {"$ref": "#/missing"},
        {"$id": "https://example.com"},
        {"$dynamicRef": "#name"},
        {"description": "x" * 65_537},
    ],
)
def test_unsafe_or_invalid_schemas_rejected_before_invocation(schema: dict) -> None:
    with pytest.raises(ValueError):
        validate_output_schema(schema)


@pytest.mark.parametrize(
    "content",
    [
        '{"x":1,"x":2}',
        '{"x":NaN}',
        '{"x":1e999}',
        "[]",
        "oops",
        '{"x":0}',
        '{"x":true}',
        " " * 65_537,
    ],
)
def test_ambiguous_or_invalid_model_json_rejected(content: str) -> None:
    schema = {"type": "object", "properties": {"x": {"type": "integer", "minimum": 1}}}
    with pytest.raises(ValueError):
        parse_structured_output(content, schema)


def test_schema_local_definitions_and_model_node_only() -> None:
    schema = {
        "type": "object",
        "properties": {"x": {"$ref": "#/$defs/a"}},
        "$defs": {"a": {"type": "integer"}},
    }
    validate_output_schema(schema)
    assert parse_structured_output('{"x":2}', schema) == {"x": 2}
    assert parse_structured_output('{"x":1.5}', {"type": "object"}) == {"x": 1.5}
    with pytest.raises(ValidationError, match="only model"):
        DurableNode(id="join", kind="join", output_schema=schema)
    assert _NoToolsPolicy().evaluate(None).type.value == "deny"
    assert not _NoToolsPolicy().can_approve(requester=context(), owner=context(), call=None)


def test_result_parser_rejects_external_schema_before_resolution() -> None:
    with pytest.raises(ValueError, match="invalid structured") as error:
        parse_structured_output('{"x":1}', {"$ref": "https://127.0.0.1:9/private"})
    assert "only local" in str(error.value.__cause__)
    with pytest.raises(ValueError, match="target a schema"):
        validate_output_schema({"description": "text", "$ref": "#/description"})


def test_three_fresh_processes_reconcile_without_resending(tmp_path: Path) -> None:
    outputs = []
    for stage in ("start", "reconcile", "resume"):
        process = subprocess.run(
            [sys.executable, str(DEMO), "--root", str(tmp_path), "--stage", stage],
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert process.returncode == 0, process.stderr
        outputs.append(json.loads(process.stdout))
    assert [item["status"] for item in outputs] == ["running", "succeeded", "succeeded"]
    assert [
        json.loads(line)["task_id"] for line in (tmp_path / "calls.jsonl").read_text().splitlines()
    ] == ["logic", "tests", "synthesis"]


@pytest.mark.parametrize(
    "point", ["created", "settled:specialist.logic", "model.sending:specialist.tests", "suspended"]
)
def test_process_death_retains_known_and_unknown_facts(tmp_path: Path, point: str) -> None:
    script = """
import asyncio, os, sys
from pathlib import Path
from examples.collaboration_demo import controller
from forgeharness.sdk.durable import DurableRunner
root = Path(sys.argv[1])
original = DurableRunner._save
def save(self, event, **kwargs):
    original(self, event, **kwargs)
    if event == sys.argv[2]:
        os._exit(91)
DurableRunner._save = save
group, ctx = controller(root)
asyncio.run(group.harness.run(context=ctx, task=group.plan.goal))
"""
    first = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path), point],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert first.returncode == 91, first.stderr
    resume = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import asyncio, sys
from pathlib import Path
from examples.collaboration_demo import controller
group, ctx = controller(Path(sys.argv[1]))
asyncio.run(group.harness.resume(context=ctx))
print(group.inspect(ctx).model_dump_json())
""",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert resume.returncode == 0, resume.stderr
    snapshot = json.loads(resume.stdout)
    assert snapshot["delegations"][0]["status"] == "completed"
    assert snapshot["delegations"][1]["status"] == "unknown"
    calls = [
        json.loads(line)["task_id"] for line in (tmp_path / "calls.jsonl").read_text().splitlines()
    ]
    assert calls.count("logic") == 1 and calls.count("tests") <= 1


async def test_credentials_in_definition_rejected_before_model(tmp_path: Path) -> None:
    raw = demo_plan().model_dump()
    raw["evidence"][0]["text"] = "Bearer synthetic-private-credential"
    model = RecordingModel()
    group = team(tmp_path, model, plan=CollaborationPlan.model_validate(raw))
    with pytest.raises(ScopeError, match="credentials"):
        await group.harness.run(context=context(), task=group.plan.goal)
    assert model.requests == []
