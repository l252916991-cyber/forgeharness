"""Event-barrier proofs and failure interleavings for the single-writer path."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import datetime, timedelta

import pytest
from pydantic import BaseModel, ValidationError

from forgeharness.sdk import (
    Actor,
    ApplicationSpec,
    CapacityError,
    DurableGraph,
    DurableNode,
    DurableOrchestrator,
    FinalAction,
    Harness,
    Message,
    MessageRole,
    ModelDispatcher,
    ModelRequest,
    ModelResult,
    ModelUsage,
    NoopVerifier,
    ParallelExecution,
    PolicyDecision,
    PolicyDecisionType,
    ReconciledNode,
    ResourceScope,
    RiskLevel,
    RunBudget,
    RunContext,
    RunStatus,
    ScopeError,
    ToolCall,
    ToolEffectClass,
    ToolOutput,
    ToolSpec,
    Usage,
    verify_trace,
)
from forgeharness.sdk.durable import DurableRunner
from forgeharness.sdk.parallel import CapacityTicket
from forgeharness.state.orchestration import OrchestrationStore
from forgeharness.state.sqlite import connect_wal


class Allow:
    def evaluate(self, request):
        return PolicyDecision(type=PolicyDecisionType.ALLOW, reason="test")

    def can_approve(self, **kwargs):
        return True


class GateModel:
    def __init__(self, *, fail: str = "", invalid: str = "", overrun: bool = False):
        self.entered: asyncio.Queue[tuple[str, ModelRequest]] = asyncio.Queue()
        self.releases = {f"n{i}": asyncio.Event() for i in range(5)}
        self.requests: list[ModelRequest] = []
        self.active = self.peak = 0
        self.fail, self.invalid, self.overrun = fail, invalid, overrun

    async def decide(self, request: ModelRequest) -> ModelResult:
        node = request.messages[-1].content
        assert node is not None
        self.requests.append(request)
        self.active += 1
        self.peak = max(self.active, self.peak)
        await self.entered.put((node, request))
        try:
            await self.releases[node].wait()
            if node == self.fail:
                raise TimeoutError("synthetic lost response")
            return ModelResult(
                action=FinalAction(
                    content="invalid" if node == self.invalid else json.dumps({"node": node})
                ),
                usage=ModelUsage(input_tokens=1001 if self.overrun else 7, output_tokens=3),
            )
        finally:
            self.active -= 1


def ctx(run="run") -> RunContext:
    return RunContext(
        application_id="parallel",
        application_version="1",
        policy_version="1",
        run_id=run,
        session_id="session",
        correlation_id="trace",
        actor=Actor(id="host"),
        scope=ResourceScope(tenant_id="test"),
    )


def graph(count=2, *, schema=None):
    nodes = tuple(
        DurableNode(
            id=f"n{i}",
            kind="model",
            messages=(Message(role=MessageRole.USER, content=f"n{i}"),),
            max_usage=Usage(steps=1, input_tokens=1000, output_tokens=100),
            output_schema=schema,
        )
        for i in range(count)
    )
    return DurableGraph(
        version="1",
        model_identity="gated-v1",
        resource_versions={},
        nodes=(
            *nodes,
            DurableNode(id="join", kind="join", dependencies=tuple(n.id for n in nodes)),
        ),
        final_node="join",
    )


def harness(
    root,
    model,
    dispatcher,
    *,
    limit=2,
    definition=None,
    budget=None,
    authorize=None,
    resources=None,
    reconcile=None,
    **options,
):
    return Harness(
        ApplicationSpec(
            id="parallel",
            version="1",
            policy_version="1",
            model_factory=lambda: model,
            tools=(),
            policy=Allow(),
            verifier=NoopVerifier(),
            budget=budget or RunBudget(max_steps=5, max_input_tokens=5000, max_output_tokens=500),
            authorize_run=authorize or (lambda context: True),
        ),
        state_dir=root / "state",
        orchestrator=DurableOrchestrator(
            graph=definition or graph(),
            validate_resources=resources or (lambda context, revisions: True),
            reconcile_node=reconcile,
            execution=ParallelExecution(
                max_in_flight=limit,
                model_concurrency_safe=True,
                poll_interval_seconds=0.005,
                drain_timeout_seconds=0.1,
                **options,
            ),
            dispatcher=dispatcher,
        ),
    )


async def entered(model):
    return await asyncio.wait_for(model.entered.get(), 3)


async def eventually(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            tick = asyncio.Event()
            asyncio.get_running_loop().call_soon(tick.set)
            await tick.wait()


@pytest.mark.parametrize("limit", [1, 2])
@pytest.mark.parametrize("reverse", [False, True])
async def test_barrier_real_overlap_bound_and_join(tmp_path, limit, reverse):
    model = GateModel()
    with ModelDispatcher(state_dir=tmp_path / "capacity", provider_id="test") as dispatcher:
        controller = harness(tmp_path, model, dispatcher, limit=limit)
        run = asyncio.create_task(controller.run(context=ctx(), task="test"))
        first, _ = await entered(model)
        if limit == 2:
            second, _ = await entered(model)
            assert model.active == model.peak == 2
            assert not any(model.releases[n].is_set() for n in (first, second))
            assert not run.done()
            order = (second, first) if reverse else (first, second)
            model.releases[order[0]].set()
            await eventually(
                lambda: (
                    controller.inspect_orchestration(ctx()).nodes[int(order[0][1:])].status
                    == "completed"
                )
            )
            assert controller.inspect_orchestration(ctx()).nodes[-1].status == "pending"
            model.releases[order[1]].set()
        else:
            assert model.active == 1 and model.entered.empty()
            model.releases[first].set()
            second, _ = await entered(model)
            assert model.active == model.peak == 1
            model.releases[second].set()
        result = await asyncio.wait_for(run, 3)
        assert result.status is RunStatus.SUCCEEDED
        assert result.usage == Usage(steps=2, input_tokens=14, output_tokens=6)
        assert model.peak == limit
        assert await controller.resume(context=ctx()) == result
        assert len(model.requests) == 2
        assert verify_trace(controller.trace_path(ctx())).valid
        events = [
            json.loads(line)["event"]
            for line in controller.trace_path(ctx()).read_text().splitlines()
        ]
        for node in ("n0", "n1"):
            sending = next(
                i
                for i, e in enumerate(events)
                if e["type"] == "orchestration.transition"
                and e["payload"]["event"] == "model.sending:" + node
            )
            request = next(
                i
                for i, e in enumerate(events)
                if e["type"] == "model.request" and e["payload"]["phase"] == "r2:" + node
            )
            assert request < sending


async def test_three_ready_nodes_never_exceed_two_and_refill(tmp_path):
    model = GateModel()
    with ModelDispatcher(state_dir=tmp_path / "capacity", provider_id="test") as dispatcher:
        controller = harness(tmp_path, model, dispatcher, definition=graph(3))
        run = asyncio.create_task(controller.run(context=ctx(), task="test"))
        assert (await entered(model))[0] == "n0"
        assert (await entered(model))[0] == "n1"
        assert model.entered.empty()
        model.releases["n1"].set()
        assert (await entered(model))[0] == "n2"
        assert model.active == model.peak == 2
        model.releases["n0"].set()
        model.releases["n2"].set()
        assert (await run).status is RunStatus.SUCCEEDED


async def test_two_runs_share_capacity(tmp_path):
    model = GateModel()
    with ModelDispatcher(
        state_dir=tmp_path / "capacity", provider_id="test", capacity=2
    ) as dispatcher:
        first = harness(tmp_path / "a", model, dispatcher, definition=graph(1))
        second = harness(tmp_path / "b", model, dispatcher, definition=graph(1))
        runs = [
            asyncio.create_task(first.run(context=ctx("a"), task="a")),
            asyncio.create_task(second.run(context=ctx("b"), task="b")),
        ]
        await entered(model)
        await entered(model)
        assert model.active == model.peak == 2
        model.releases["n0"].set()
        assert all(r.status is RunStatus.SUCCEEDED for r in await asyncio.gather(*runs))


async def test_last_budget_reservation_cannot_be_spent_twice(tmp_path):
    model = GateModel()
    with ModelDispatcher(state_dir=tmp_path / "capacity", provider_id="test") as dispatcher:
        controller = harness(
            tmp_path,
            model,
            dispatcher,
            budget=RunBudget(max_steps=1, max_input_tokens=1000, max_output_tokens=100),
        )
        run = asyncio.create_task(controller.run(context=ctx(), task="test"))
        assert (await entered(model))[0] == "n0"
        model.releases["n0"].set()
        result = await run
        assert result.status is RunStatus.EXHAUSTED
        assert len(model.requests) == 1
        assert result.usage == Usage(steps=1, input_tokens=7, output_tokens=3)


async def test_known_invalid_result_drains_sibling_then_blocks_join(tmp_path):
    model = GateModel(invalid="n0")
    schema = {"type": "object", "required": ["node"]}
    with ModelDispatcher(state_dir=tmp_path / "capacity", provider_id="test") as dispatcher:
        controller = harness(tmp_path, model, dispatcher, definition=graph(schema=schema))
        run = asyncio.create_task(controller.run(context=ctx(), task="test"))
        await entered(model)
        await entered(model)
        model.releases["n0"].set()
        await eventually(
            lambda: controller.inspect_orchestration(ctx()).nodes[0].status == "failed"
        )
        model.releases["n1"].set()
        result = await run
        assert result.status is RunStatus.FAILED
        assert result.usage == Usage(steps=2, input_tokens=14, output_tokens=6)
        assert [n.status for n in controller.inspect_orchestration(ctx()).nodes] == [
            "failed",
            "completed",
            "pending",
        ]


@pytest.mark.parametrize("revoked", ["actor", "resource"])
async def test_revocation_hides_output_but_settles_incurred_cost(tmp_path, revoked):
    allowed = {"actor": True, "resource": True}
    model = GateModel()
    with ModelDispatcher(state_dir=tmp_path / "capacity", provider_id="test") as dispatcher:
        controller = harness(
            tmp_path,
            model,
            dispatcher,
            authorize=lambda c: allowed["actor"],
            resources=lambda c, v: allowed["resource"],
        )
        run = asyncio.create_task(controller.run(context=ctx(), task="test"))
        await entered(model)
        await entered(model)
        allowed[revoked] = False
        model.releases["n0"].set()
        model.releases["n1"].set()
        with pytest.raises(ScopeError):
            await run
        with pytest.raises(ScopeError):
            controller.inspect(ctx())
        allowed[revoked] = True
        snapshot = controller.inspect_orchestration(ctx())
        assert snapshot.result.usage == Usage(steps=2, input_tokens=14, output_tokens=6)
        assert snapshot.nodes[-1].status == "pending"


async def test_cancel_stops_new_nodes_and_retains_known_results(tmp_path):
    model = GateModel()
    with ModelDispatcher(state_dir=tmp_path / "capacity", provider_id="test") as dispatcher:
        controller = harness(tmp_path, model, dispatcher, definition=graph(3))
        run = asyncio.create_task(controller.run(context=ctx(), task="test"))
        await entered(model)
        await entered(model)
        assert controller.cancel(ctx())
        model.releases["n0"].set()
        model.releases["n1"].set()
        result = await run
        assert result.status is RunStatus.CANCELLED
        assert result.usage == Usage(steps=2, input_tokens=14, output_tokens=6)
        assert len(model.requests) == 2
        assert model.active == 0


async def test_unknown_usage_retains_budget_no_resend_and_reconciles(tmp_path):
    model = GateModel(fail="n1")
    model.releases["n0"].set()
    model.releases["n1"].set()

    receipt_keys = []

    async def reconcile(context, node, key):
        receipt_keys.append(key)
        return ReconciledNode(
            output=ModelResult(
                action=FinalAction(content="receipt"),
                usage=ModelUsage(input_tokens=9, output_tokens=4),
            ).model_dump(mode="json"),
            usage=Usage(steps=1, input_tokens=9, output_tokens=4),
            evidence_refs=("receipt:1",),
        )

    with ModelDispatcher(state_dir=tmp_path / "capacity", provider_id="test") as dispatcher:
        controller = harness(tmp_path, model, dispatcher, reconcile=reconcile)
        result = await controller.run(context=ctx(), task="test")
        assert result.status is RunStatus.RUNNING
        state = controller.inspect_orchestration(ctx())
        assert state.nodes[1].reservation == Usage(steps=1, input_tokens=1000, output_tokens=100)
        assert dispatcher.unresolved == (state.nodes[1].invocation_id,)
        await controller.resume(context=ctx())
        assert len(model.requests) == 2
        result = await controller.reconcile(context=ctx(), node_id="n1")
        assert result.status is RunStatus.SUCCEEDED
        assert result.usage == Usage(steps=2, input_tokens=16, output_tokens=7)
        assert receipt_keys == [state.nodes[1].invocation_id]
        assert dispatcher.unresolved == ()


async def test_duplicate_identical_response_is_idempotent_conflict_is_rejected(
    tmp_path, monkeypatch
):
    import forgeharness.sdk.parallel_runner as scheduler
    from forgeharness.sdk import CheckpointConflict

    original = scheduler._settle

    def check(runner, flight, result):
        status = original(runner, flight, result)
        assert original(runner, flight, result) == status
        outcome, events = result
        conflicting = replace(outcome, usage=Usage(steps=1, input_tokens=99, output_tokens=3))
        with pytest.raises(CheckpointConflict, match="conflicting duplicate"):
            original(runner, flight, (conflicting, events))
        return status

    monkeypatch.setattr(scheduler, "_settle", check)
    model = GateModel()
    model.releases["n0"].set()
    model.releases["n1"].set()
    with ModelDispatcher(state_dir=tmp_path / "capacity", provider_id="test") as dispatcher:
        controller = harness(tmp_path, model, dispatcher)
        result = await controller.run(context=ctx(), task="test")
        assert result.usage == Usage(steps=2, input_tokens=14, output_tokens=6)


async def test_transport_error_partial_usage_does_not_settle_as_final(tmp_path):
    class PartialTransport(GateModel):
        async def decide(self, request):
            error = TimeoutError("partial transport receipt is not a final response")
            error.usage = ModelUsage(input_tokens=2, output_tokens=1)
            raise error

    with ModelDispatcher(state_dir=tmp_path / "capacity", provider_id="test") as dispatcher:
        controller = harness(tmp_path, PartialTransport(), dispatcher, definition=graph(1))
        result = await controller.run(context=ctx(), task="test")
        assert result.status is RunStatus.RUNNING
        node = controller.inspect_orchestration(ctx()).nodes[0]
        assert node.status == "unknown" and node.reservation.input_tokens == 1000
        assert len(dispatcher.unresolved) == 1


async def test_unconfirmed_cancel_keeps_physical_capacity_across_restart(tmp_path):
    model = GateModel()
    directory = tmp_path / "capacity"
    with ModelDispatcher(state_dir=directory, provider_id="test") as dispatcher:
        controller = harness(tmp_path, model, dispatcher)
        run = asyncio.create_task(controller.run(context=ctx(), task="test"))
        await entered(model)
        await entered(model)
        run.cancel()
        with pytest.raises(asyncio.CancelledError):
            await run
        assert model.active == 0
        assert len(dispatcher.unresolved) == 2
        invocations = dispatcher.unresolved
    with ModelDispatcher(state_dir=directory, provider_id="test") as fresh:
        assert fresh.unresolved == invocations
        with pytest.raises(CapacityError, match="reconciliation"):
            fresh.request(CapacityTicket("new", "new-run", "node"))
        for key in invocations:
            fresh.confirm_ended(key, evidence_refs=("backend:idle",))
        # Clearing physical capacity must not erase the run's unknown budget.
        revived = harness(tmp_path, model, fresh)
        result = await revived.resume(context=ctx())
        assert result.status is RunStatus.RUNNING
        assert len(model.requests) == 2


def test_policy_and_owner_validation(tmp_path):
    with pytest.raises(ValidationError):
        ParallelExecution(max_in_flight=2)
    with pytest.raises(ValidationError):
        ParallelExecution(request_timeout_seconds=float("nan"))
    with pytest.raises(ValueError, match="dispatcher"):
        DurableOrchestrator(
            graph(),
            lambda c, v: True,
            execution=ParallelExecution(max_in_flight=2, model_concurrency_safe=True),
        )
    with ModelDispatcher(state_dir=tmp_path / "capacity", provider_id="test") as dispatcher:
        with pytest.raises(ValueError, match="shared concurrent provider"):
            DurableOrchestrator(graph(), lambda c, v: True, dispatcher=dispatcher)
        with pytest.raises(CapacityError, match="live dispatcher"):
            ModelDispatcher(state_dir=tmp_path / "capacity", provider_id="test")


async def test_queue_bound_round_robin_and_unsent_release(tmp_path):
    with ModelDispatcher(
        state_dir=tmp_path / "capacity", provider_id="test", capacity=1, max_queue=3
    ) as dispatcher:
        first = CapacityTicket("first", "a", "n0")
        assert (await dispatcher.request(first)) == first
        a = CapacityTicket("a", "a", "n1")
        b = CapacityTicket("b", "b", "n0")
        a2 = CapacityTicket("a2", "a", "n2")
        fa, fb, fa2 = [dispatcher.request(t) for t in (a, b, a2)]
        with pytest.raises(CapacityError, match="full"):
            dispatcher.request(CapacityTicket("extra", "c", "n0"))
        dispatcher.withdraw(first)
        assert await fa == a
        assert not fb.done() and not fa2.done()
        dispatcher.withdraw(a)
        assert await fb == b
        assert not fa2.done()
        dispatcher.withdraw(b)
        assert await fa2 == a2
        dispatcher.withdraw(a2)


async def test_prepared_payload_and_policy_identity_cannot_change_on_resume(tmp_path):
    model = GateModel()
    model.releases["n0"].set()
    model.releases["n1"].set()
    with ModelDispatcher(state_dir=tmp_path / "capacity", provider_id="test") as dispatcher:
        controller = harness(tmp_path, model, dispatcher)
        await controller.run(context=ctx(), task="test")
        assert all(
            n.prepared_payload_sha256 for n in controller.inspect_orchestration(ctx()).nodes[:2]
        )
        controller.orchestrator = replace(
            controller.orchestrator,
            execution=ParallelExecution(
                max_in_flight=1,
                model_concurrency_safe=True,
                poll_interval_seconds=0.005,
                drain_timeout_seconds=0.1,
            ),
        )
        with pytest.raises(ScopeError, match="identity changed"):
            await controller.resume(context=ctx())


async def test_overrun_is_retained_and_stops_more_admission(tmp_path):
    model = GateModel(overrun=True)
    model.releases["n0"].set()
    model.releases["n1"].set()
    with ModelDispatcher(state_dir=tmp_path / "capacity", provider_id="test") as dispatcher:
        controller = harness(tmp_path, model, dispatcher, definition=graph(3))
        result = await controller.run(context=ctx(), task="test")
        assert result.status is RunStatus.FAILED
        assert result.usage.input_tokens >= 1001
        assert len(model.requests) <= 2
        assert all(
            n.reservation == Usage()
            for n in controller.inspect_orchestration(ctx()).nodes
            if n.status == "failed"
        )


async def test_capacity_wait_cancel_does_not_stop_another_run(tmp_path):
    model = GateModel()
    with ModelDispatcher(
        state_dir=tmp_path / "capacity", provider_id="test", capacity=1
    ) as dispatcher:
        first = harness(tmp_path / "a", model, dispatcher, definition=graph(1))
        second = harness(tmp_path / "b", model, dispatcher, definition=graph(1))
        a = asyncio.create_task(first.run(context=ctx("a"), task="a"))
        await entered(model)
        b = asyncio.create_task(second.run(context=ctx("b"), task="b"))
        await eventually(
            lambda: (
                second.find(ctx("b")) is not None
                and second.inspect_orchestration(ctx("b")).nodes[0].dispatch_phase == "queued"
            )
        )
        second.cancel(ctx("b"))
        assert (await b).status is RunStatus.CANCELLED
        assert len(model.requests) == 1 and model.active == 1
        model.releases["n0"].set()
        assert (await a).status is RunStatus.SUCCEEDED


class Empty(BaseModel):
    pass


class ExclusiveTool:
    input_model = Empty
    spec = ToolSpec(
        name="observe",
        description="test barrier",
        input_schema=Empty.model_json_schema(),
        risk=RiskLevel.READ,
        effect_class=ToolEffectClass.READ_ONLY,
    )

    def __init__(self, model):
        self.model, self.calls = model, 0

    async def execute(self, arguments, context):
        assert self.model.active == 0
        self.calls += 1
        return ToolOutput(ok=True, content="exclusive")


@pytest.mark.parametrize("lost", [False, True])
async def test_tool_is_exclusive_and_unknown_never_crosses_barrier(tmp_path, lost):
    model = GateModel(fail="n0" if lost else "")
    tool = ExclusiveTool(model)
    base = graph()
    definition = base.model_copy(
        update={
            "nodes": (
                base.nodes[0],
                DurableNode(
                    id="tool",
                    kind="tool",
                    call=ToolCall(id="x", name="observe", arguments={}),
                    max_usage=Usage(tool_calls=1),
                ),
                base.nodes[1],
                base.nodes[2],
            )
        }
    )
    with ModelDispatcher(state_dir=tmp_path / "capacity", provider_id="test") as dispatcher:
        controller = harness(tmp_path, model, dispatcher, definition=definition)
        controller.application = replace(controller.application, tools=(tool,))
        run = asyncio.create_task(controller.run(context=ctx(), task="test"))
        await entered(model)
        assert tool.calls == 0 and model.entered.empty()
        model.releases["n0"].set()
        if lost:
            assert (await run).status is RunStatus.RUNNING
            assert tool.calls == 0 and model.active == 0
            await controller.resume(context=ctx())
            assert tool.calls == 0 and len(model.requests) == 1
        else:
            assert (await entered(model))[0] == "n1"
            assert tool.calls == 1
            model.releases["n1"].set()
            assert (await run).status is RunStatus.SUCCEEDED


async def test_expired_deadline_stops_admission_but_commits_active_cost(tmp_path, monkeypatch):
    import forgeharness.sdk.durable as durable

    class Clock(datetime):
        offset = timedelta()

        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) + cls.offset

    monkeypatch.setattr(durable, "datetime", Clock)
    model = GateModel()
    with ModelDispatcher(state_dir=tmp_path / "capacity", provider_id="test") as dispatcher:
        controller = harness(
            tmp_path, model, dispatcher, definition=graph(3), budget=RunBudget(max_wall_seconds=10)
        )
        run = asyncio.create_task(controller.run(context=ctx(), task="test"))
        await entered(model)
        await entered(model)
        Clock.offset = timedelta(seconds=20)
        model.releases["n0"].set()
        model.releases["n1"].set()
        result = await run
        assert result.status is RunStatus.EXHAUSTED
        assert result.usage == Usage(steps=2, input_tokens=14, output_tokens=6)
        assert len(model.requests) == 2


async def test_provider_timeout_is_unknown_and_holds_capacity(tmp_path):
    model = GateModel()
    with ModelDispatcher(state_dir=tmp_path / "capacity", provider_id="test") as dispatcher:
        controller = harness(tmp_path, model, dispatcher, request_timeout_seconds=0.01)
        result = await controller.run(context=ctx(), task="test")
        assert result.status is RunStatus.RUNNING
        assert len(dispatcher.unresolved) == 2
        assert model.active == 0
        assert all(
            n.used == Usage(steps=1) and n.reservation.steps == 1
            for n in controller.inspect_orchestration(ctx()).nodes[:2]
        )


async def test_preparation_failure_does_not_send_or_consume_capacity(tmp_path):
    class PreparationFailure(GateModel):
        def prepare_request(self, request):
            raise ValueError("synthetic preparation refusal")

        async def decide_prepared(self, prepared):
            raise AssertionError("must not send")

    model = PreparationFailure()
    with ModelDispatcher(state_dir=tmp_path / "capacity", provider_id="test") as dispatcher:
        controller = harness(tmp_path, model, dispatcher)
        result = await controller.run(context=ctx(), task="test")
        assert result.status is RunStatus.FAILED and result.usage == Usage()
        assert not model.requests and not dispatcher.unresolved


async def test_commit_after_publish_error_does_not_double_charge(tmp_path, monkeypatch):
    original = DurableRunner.publish
    fired = False

    def broken(self):
        nonlocal fired
        original(self)
        if not fired and self._progress("n0").status == "completed":
            fired = True
            raise OSError("synthetic post-commit trace failure")

    monkeypatch.setattr(DurableRunner, "publish", broken)
    model = GateModel()
    model.releases["n0"].set()
    model.releases["n1"].set()
    with ModelDispatcher(state_dir=tmp_path / "capacity", provider_id="test") as dispatcher:
        controller = harness(tmp_path, model, dispatcher)
        with pytest.raises(OSError):
            await controller.run(context=ctx(), task="test")
        monkeypatch.setattr(DurableRunner, "publish", original)
        # Each known committed result remains reusable; local cleanup is idempotent.
        state = controller.inspect_orchestration(ctx())
        assert state.nodes[0].status == "completed"
        assert state.nodes[0].used == Usage(steps=1, input_tokens=7, output_tokens=3)
        await controller.resume(context=ctx())
        assert len(model.requests) == 2


async def test_schema_two_and_recovery_four_are_rejected(tmp_path):
    model = GateModel()
    model.releases["n0"].set()
    model.releases["n1"].set()
    with ModelDispatcher(state_dir=tmp_path / "capacity", provider_id="test") as dispatcher:
        controller = harness(tmp_path, model, dispatcher)
        await controller.run(context=ctx(), task="test")
        state = controller.inspect_orchestration(ctx())
        payload = state.model_dump(mode="json")
        payload["schema_version"] = 2
        payload["result"]["recovery_semantics_version"] = 4
        with connect_wal(controller.database) as connection:
            connection.execute(
                "UPDATE orchestration_runs SET payload=? WHERE task_id=?",
                (json.dumps(payload), state.result.task_id),
            )
        with pytest.raises(ValidationError):
            await controller.resume(context=ctx())


async def test_resource_revoked_after_sending_permit_before_provider_entry(tmp_path, monkeypatch):
    allowed = True
    original = DurableRunner._save

    def save(self, event, **kwargs):
        nonlocal allowed
        original(self, event, **kwargs)
        if event == "model.sending:n0":
            allowed = False

    monkeypatch.setattr(DurableRunner, "_save", save)
    model = GateModel()
    with ModelDispatcher(state_dir=tmp_path / "capacity", provider_id="test") as dispatcher:
        controller = harness(tmp_path, model, dispatcher, resources=lambda c, v: allowed)
        with pytest.raises(ScopeError):
            await controller.run(context=ctx(), task="test")
        assert model.requests == [] and dispatcher.unresolved == ()
        allowed = True
        state = controller.inspect_orchestration(ctx())
        assert state.nodes[0].status == "failed"
        assert state.result.usage == Usage()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"provider_id": "http://unsafe?token=raw"},
        {"provider_id": "sk-abcdefghijk"},
        {"capacity": True},
        {"capacity": 3},
        {"max_queue": 0},
        {"max_queue": True},
    ],
)
def test_provider_configuration_cannot_bypass_limits_or_persist_credentials(tmp_path, kwargs):
    with pytest.raises(ValueError):
        ModelDispatcher(state_dir=tmp_path / "capacity", **({"provider_id": "test"} | kwargs))


async def test_receipt_cannot_clear_live_or_unreferenced_capacity(tmp_path):
    dispatcher = ModelDispatcher(state_dir=tmp_path / "capacity", provider_id="test")
    ticket = CapacityTicket("id", "run", "node")
    await dispatcher.request(ticket)
    with pytest.raises(CapacityError, match="active calls"):
        dispatcher.close()
    with pytest.raises(CapacityError, match="unresolved"):
        dispatcher.confirm_ended("id", evidence_refs=("receipt:1",))
    dispatcher.sent(ticket)
    dispatcher.finish(ticket, confirmed=False)
    for refs in ((), ("bad receipt",), ("sk-abcdefghijk",)):
        with pytest.raises(ValueError):
            dispatcher.confirm_ended("id", evidence_refs=refs)
    dispatcher.confirm_ended("id", evidence_refs=("receipt:1",))
    dispatcher.close()
    dispatcher.close()
    with pytest.raises(CapacityError, match="closed"):
        dispatcher.__enter__()


async def test_dispatcher_rejects_duplicate_waiters_and_conflicting_identity(tmp_path):
    with ModelDispatcher(
        state_dir=tmp_path / "capacity", provider_id="test", capacity=1
    ) as dispatcher:
        ticket = CapacityTicket("id", "run", "node")
        await dispatcher.request(ticket)
        with pytest.raises(CapacityError, match="already owns"):
            dispatcher.request(ticket)
        other = CapacityTicket("other", "run", "node2")
        future = dispatcher.request(other)
        with pytest.raises(CapacityError, match="already owns"):
            dispatcher.request(other)
        dispatcher.withdraw(other)
        assert future.cancelled()
        dispatcher.withdraw(ticket)
        conflict = dispatcher.request(CapacityTicket("id", "foreign-run", "node"))
        with pytest.raises(CapacityError, match="cannot change"):
            await conflict
        with pytest.raises(CapacityError, match="owned capacity"):
            dispatcher.sent(CapacityTicket("missing", "run", "node"))
        with pytest.raises(CapacityError, match="active ticket"):
            dispatcher.finish(CapacityTicket("missing", "run", "node"), confirmed=True)


def test_provider_directory_is_private_and_ledger_does_not_follow_symlinks(tmp_path):
    directory = tmp_path / "public"
    directory.mkdir(mode=0o755)
    with pytest.raises(ScopeError, match="private"):
        ModelDispatcher(state_dir=directory, provider_id="test")
    import hashlib

    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    (private / (hashlib.sha256(b"test").hexdigest() + ".sqlite")).symlink_to(tmp_path / "foreign")
    with pytest.raises(ScopeError, match="symbolic"):
        ModelDispatcher(state_dir=private, provider_id="test")


def test_dispatcher_cannot_span_event_loops(tmp_path):
    with ModelDispatcher(state_dir=tmp_path / "capacity", provider_id="test") as dispatcher:

        async def use():
            ticket = CapacityTicket("id", "run", "node")
            await dispatcher.request(ticket)
            dispatcher.withdraw(ticket)

        asyncio.run(use())
        with pytest.raises(CapacityError, match="event loops"):
            asyncio.run(use())


async def test_stale_owner_cannot_settle_but_releases_confirmed_physical_capacity(tmp_path):
    from forgeharness.sdk import CheckpointConflict

    model = GateModel()
    with ModelDispatcher(state_dir=tmp_path / "capacity", provider_id="test") as dispatcher:
        controller = harness(tmp_path, model, dispatcher)
        run = asyncio.create_task(controller.run(context=ctx(), task="test"))
        await entered(model)
        await entered(model)
        with connect_wal(controller.database) as connection:
            connection.execute("UPDATE orchestration_runs SET owner='new-owner'")
        model.releases["n0"].set()
        model.releases["n1"].set()
        with pytest.raises(CheckpointConflict):
            await run
        state = OrchestrationStore(controller.database).load(controller._key(ctx()))
        assert state is not None and all(n.status == "unknown" for n in state.nodes[:2])
        assert dispatcher.unresolved == ()


async def test_same_public_id_in_two_stores_cannot_release_another_run_capacity(tmp_path):
    with ModelDispatcher(state_dir=tmp_path / "capacity", provider_id="test") as dispatcher:
        first_model = GateModel(fail="n1")
        first_model.releases["n0"].set()
        first_model.releases["n1"].set()
        first = harness(tmp_path / "first", first_model, dispatcher)
        assert (await first.run(context=ctx(), task="same")).status is RunStatus.RUNNING
        first_unknown = first.inspect_orchestration(ctx()).nodes[1].invocation_id
        dispatcher.confirm_ended(first_unknown, evidence_refs=("backend:idle-after-first",))
        second_model = GateModel(fail="n0")
        second_model.releases["n0"].set()
        second_model.releases["n1"].set()
        second = harness(tmp_path / "second", second_model, dispatcher)
        assert (await second.run(context=ctx(), task="same")).status is RunStatus.RUNNING
        second_unknown = second.inspect_orchestration(ctx()).nodes[0].invocation_id
        assert second_unknown in dispatcher.unresolved
        await first.resume(context=ctx())
        assert second_unknown in dispatcher.unresolved


async def test_previous_framework_candidate_identity_cannot_resume_under_fixed_version(
    tmp_path, monkeypatch
):
    import forgeharness.sdk.durable as durable
    from forgeharness import __version__

    model = GateModel()
    model.releases["n0"].set()
    model.releases["n1"].set()
    with ModelDispatcher(state_dir=tmp_path / "capacity", provider_id="test") as dispatcher:
        controller = harness(tmp_path, model, dispatcher)
        monkeypatch.setattr(durable, "__version__", "0.9.0")
        await controller.run(context=ctx(), task="test")
        monkeypatch.setattr(durable, "__version__", __version__)
        with pytest.raises(ScopeError, match="identity changed"):
            await controller.resume(context=ctx())
