"""Public DAG composition, crash reconciliation, shared caps and lifecycle controls."""

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest
from test_sdk import Echo, Policy, app, context

from forgeharness.sdk import (
    Actor,
    CheckpointConflict,
    FinalAction,
    Harness,
    ModelResult,
    ModelUsage,
    RunBudget,
    RunStatus,
    ScopeError,
    ToolAction,
    ToolCall,
    VerificationResult,
    WorkflowBudget,
    WorkflowRunner,
    WorkflowSpec,
    WorkflowStep,
)


class Model:
    def __init__(self, text):
        self.text = text
        self.requests = []

    async def decide(self, request):
        self.requests.append(request)
        if request.messages[-1].role.value == "tool":
            action = FinalAction(content=self.text)
        else:
            action = ToolAction(
                call=ToolCall(id="call", name="echo", arguments={"text": self.text})
            )
        return ModelResult(action=action, usage=ModelUsage(input_tokens=1, output_tokens=1))


def step(name, *, depends=(), write=False, tool=None, policy=None, text=None):
    model = Model(text or name)
    application = app(
        tool or Echo(write=write),
        [],
        policy,
        model_factory=lambda: model,
        budget=RunBudget(
            max_steps=2, max_tool_calls=1, max_input_tokens=100, max_output_tokens=100
        ),
    )
    return WorkflowStep(name, application, f"Perform {name}", depends)


def spec(*steps, **changes):
    return WorkflowSpec("echo", "1", "1", steps, **changes)


async def test_dag_order_input_handoff_and_aggregate_usage(tmp_path):
    a, b, c = (
        step("retrieve"),
        step("facts", depends=("retrieve",)),
        step("review", depends=("facts",)),
    )
    runner = WorkflowRunner(spec(c, b, a), state_dir=tmp_path)
    result = await runner.run(context=context(), task="synthetic legal review")
    assert result.status == RunStatus.SUCCEEDED
    assert result.completed_nodes == ("retrieve", "facts", "review")
    assert result.usage.steps == 6 and result.usage.tool_calls == 3
    assert result.usage.input_tokens == 6
    assert '"output": "facts"' in c.application.model_factory().requests[0].messages[-1].content
    assert runner.events(context())[-1]["event"] == "completed"
    assert [
        event["node"] for event in runner.events(context()) if event["event"] == "node.completed"
    ] == ["retrieve", "facts", "review"]
    with pytest.raises(CheckpointConflict):
        await runner.run(context=context(), task="duplicate")
    with pytest.raises(CheckpointConflict):
        await runner.resume(context=context())


@pytest.mark.parametrize(
    "kind", ["duplicate", "unknown", "cycle", "id", "empty", "binding", "duplicate-dep"]
)
def test_invalid_graph_rejected_before_any_runtime(kind):
    a = step("a")
    cases = {
        "duplicate": (a, a),
        "unknown": (replace(a, depends_on=("missing",)),),
        "cycle": (replace(a, depends_on=("a",)),),
        "id": (replace(a, id="../outside"),),
        "empty": (),
        "binding": (replace(a, application=replace(a.application, id="other")),),
        "duplicate-dep": (a, step("b", depends=("a", "a"))),
    }
    with pytest.raises(ValueError):
        spec(*cases[kind])


@pytest.mark.parametrize(
    "metric,limit", [("steps", 3), ("tool_calls", 1), ("input_tokens", 199), ("output_tokens", 199)]
)
def test_whole_workflow_reservation_cannot_multiply_budget(metric, limit):
    budget = WorkflowBudget.model_validate({f"max_{metric}": limit})
    with pytest.raises(ValueError, match="reservations"):
        spec(step("a"), step("b"), budget=budget)


async def test_approval_survives_reconstruction_without_rerunning_reads(tmp_path):
    a, b = step("read"), step("write", depends=("read",), write=True)
    definition = spec(a, b)
    runner = WorkflowRunner(definition, state_dir=tmp_path)
    result = await runner.run(context=context(), task="handoff")
    assert result.status == RunStatus.AWAITING_APPROVAL
    assert len(a.application.tools[0].calls) == 1
    assert len(b.application.tools[0].calls) == 0
    runner = WorkflowRunner(definition, state_dir=tmp_path)
    grant = runner.approve(context=context(), approver=context())
    runner = WorkflowRunner(definition, state_dir=tmp_path)
    result = await runner.resume(context=context(), grant=grant, approver=context())
    assert result.status == RunStatus.SUCCEEDED
    assert len(a.application.tools[0].calls) == len(b.application.tools[0].calls) == 1
    assert result.usage.steps == 4


async def test_bad_approval_keeps_workflow_resumable(tmp_path):
    definition = spec(step("write", write=True))
    runner = WorkflowRunner(definition, state_dir=tmp_path)
    await runner.run(context=context(), task="write")
    grant = runner.approve(context=context(), approver=context())
    bad_actor = context(actor=Actor(id="other", roles=("approve",)))
    with pytest.raises(ScopeError):
        await runner.resume(context=context(), grant=grant, approver=bad_actor)
    assert runner.inspect(context()).status == RunStatus.AWAITING_APPROVAL
    assert (
        await runner.resume(context=context(), grant=grant, approver=context())
    ).status == RunStatus.SUCCEEDED


async def test_completed_child_reconciled_after_parent_checkpoint_crash(tmp_path, monkeypatch):
    a, b = step("a"), step("b", depends=("a",))
    definition = spec(a, b)
    runner = WorkflowRunner(definition, state_dir=tmp_path)
    save = runner._save

    def crash(state, event, **changes):
        if event == "node.completed":
            raise RuntimeError("simulated process loss")
        return save(state, event, **changes)

    monkeypatch.setattr(runner, "_save", crash)
    with pytest.raises(RuntimeError):
        await runner.run(context=context(), task="durable flow")
    assert len(a.application.tools[0].calls) == 1
    rebuilt = WorkflowRunner(definition, state_dir=tmp_path)
    assert (await rebuilt.resume(context=context())).status == RunStatus.SUCCEEDED
    assert len(a.application.tools[0].calls) == len(b.application.tools[0].calls) == 1


async def test_waiting_child_reconciled_after_parent_crash(tmp_path, monkeypatch):
    definition = spec(step("a", write=True))
    runner = WorkflowRunner(definition, state_dir=tmp_path)
    save = runner._save

    def crash(state, event, **changes):
        if event == "node.suspended":
            raise RuntimeError("crash")
        return save(state, event, **changes)

    monkeypatch.setattr(runner, "_save", crash)
    with pytest.raises(RuntimeError):
        await runner.run(context=context(), task="write")
    rebuilt = WorkflowRunner(definition, state_dir=tmp_path)
    assert (await rebuilt.resume(context=context())).status == RunStatus.AWAITING_APPROVAL


async def test_terminal_failure_stops_dependents(tmp_path):
    a, b = step("a"), step("b", depends=("a",))
    a = replace(a, application=replace(a.application, budget=RunBudget(max_steps=1)))
    runner = WorkflowRunner(spec(a, b), state_dir=tmp_path)
    assert (await runner.run(context=context(), task="test")).status == RunStatus.EXHAUSTED
    assert not b.application.tools[0].calls
    with pytest.raises(CheckpointConflict):
        await runner.resume(context=context())


async def test_scope_and_definition_change_rejected(tmp_path):
    a = step("a", write=True)
    runner = WorkflowRunner(spec(a), state_dir=tmp_path)
    await runner.run(context=context(), task="write")
    with pytest.raises(ScopeError):
        runner.inspect(context(actor=Actor(id="intruder")))
    with pytest.raises(ScopeError):
        runner.events(context(session_id="other"))
    changed = WorkflowRunner(spec(replace(a, task="changed instructions")), state_dir=tmp_path)
    with pytest.raises(CheckpointConflict, match="definition"):
        await changed.resume(context=context())


async def test_active_worker_rejects_concurrent_resume_and_delete(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()

    class BlockingEcho(Echo):
        async def execute(self, arguments, context):
            entered.set()
            await release.wait()
            return await super().execute(arguments, context)

    definition = spec(step("a", tool=BlockingEcho()))
    runner = WorkflowRunner(definition, state_dir=tmp_path)
    running = asyncio.create_task(runner.run(context=context(), task="slow"))
    await entered.wait()
    rebuilt = WorkflowRunner(definition, state_dir=tmp_path)
    with pytest.raises(ScopeError, match="active"):
        await rebuilt.resume(context=context())
    with pytest.raises(ScopeError, match="active"):
        rebuilt.delete(context())
    release.set()
    assert (await running).status == RunStatus.SUCCEEDED


async def test_delete_retry_and_find_distinguish_tombstones(tmp_path, monkeypatch):
    runner = WorkflowRunner(spec(step("a")), state_dir=tmp_path)
    await runner.run(context=context(), task="private case data")
    original = Harness.delete
    calls = 0

    def crash(self, owner, **kwargs):
        nonlocal calls
        original(self, owner, **kwargs)
        calls += 1
        if calls == 1:
            raise OSError("cleanup interrupted")

    monkeypatch.setattr(Harness, "delete", crash)
    with pytest.raises(OSError):
        runner.delete(context())
    with pytest.raises(ScopeError, match="deleted"):
        await runner.resume(context=context())
    runner.delete(context())
    with pytest.raises(ScopeError):
        runner.inspect(context())
    with pytest.raises(CheckpointConflict):
        await runner.run(context=context(), task="reuse")
    assert not list(tmp_path.rglob("*.jsonl"))


async def test_bounded_dependency_data_and_credential_transformation(tmp_path):
    a, b = step("a", text="x" * 300), step("b", depends=("a",))
    runner = WorkflowRunner(
        spec(a, b, budget=WorkflowBudget(max_input_chars=250)), state_dir=tmp_path
    )
    with pytest.raises(ValueError, match="input"):
        await runner.run(context=context(), task="api_key=private-example")
    assert not b.application.tools[0].calls
    state = runner._load(context())
    assert "private-example" not in state.task
    with pytest.raises(ValueError):
        await runner.run(context=context(run_id="another"), task="x" * 251)


async def test_public_find_missing_and_deleted_are_distinct(tmp_path: Path):
    application = step("a").application
    harness = Harness(application, state_dir=tmp_path)
    assert harness.find(context()) is None
    await harness.run(context=context(), task="hello")
    harness.delete(context())
    with pytest.raises(ScopeError, match="deleted"):
        harness.find(context())


async def test_revoked_execution_policy_prevents_pending_write(tmp_path):
    policy = Policy()
    a = step("a", write=True, policy=policy)
    runner = WorkflowRunner(spec(a), state_dir=tmp_path)
    await runner.run(context=context(), task="write")
    grant = runner.approve(context=context(), approver=context())
    policy.revoked = True
    with pytest.raises(ScopeError):
        await runner.resume(context=context(), grant=grant, approver=context())
    assert not a.application.tools[0].calls


@pytest.mark.parametrize("kind", ["rejection", "exception"])
async def test_final_verifier_failure_never_becomes_success(tmp_path, kind):
    class Verifier:
        async def verify(self, request):
            if kind == "exception":
                raise RuntimeError("api_key=must-not-be-recorded")
            return VerificationResult(passed=False, reason="business evidence incomplete")

    runner = WorkflowRunner(spec(step("a"), verifier=Verifier()), state_dir=tmp_path)
    result = await runner.run(context=context(), task="test")
    assert result.status == RunStatus.FAILED
    assert result.nodes["a"].status == RunStatus.SUCCEEDED
    assert "must-not-be-recorded" not in result.model_dump_json()
    with pytest.raises(ScopeError):
        runner.approve(context=context(), approver=context())


def test_invalid_workflow_identity_private_directory_and_binding(tmp_path):
    definition = spec(step("a"))
    with pytest.raises(ValueError):
        replace(definition, id="../bad")
    with pytest.raises(ValueError):
        replace(definition, version="")
    tmp_path.chmod(0o755)
    with pytest.raises(ScopeError):
        WorkflowRunner(definition, state_dir=tmp_path)


async def test_missing_binding_and_fresh_approver_required(tmp_path):
    runner = WorkflowRunner(spec(step("a", write=True)), state_dir=tmp_path)
    with pytest.raises(ScopeError):
        runner.inspect(context())
    with pytest.raises(ScopeError):
        await runner.run(context=context(application_version="2"), task="wrong version")
    await runner.run(context=context(), task="write")
    grant = runner.approve(context=context(), approver=context())
    with pytest.raises(ScopeError):
        await runner.resume(context=context(), grant=grant)
