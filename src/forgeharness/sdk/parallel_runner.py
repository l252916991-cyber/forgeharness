"""Single-writer R2 dispatcher; provider tasks return facts, never commit state."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from forgeharness.context.assembly import ContextAssembler
from forgeharness.domain.models import FinalAction, ModelUsage, RunResult, TraceEvent, Usage
from forgeharness.models.base import Model, ModelRequest
from forgeharness.models.prepared import PreparedModelCall
from forgeharness.observability.trace import InMemoryTrace
from forgeharness.runtime.budget import RunBudget
from forgeharness.runtime.model_call import (
    ModelCallKind,
    ModelCallOutcome,
    dispatch_model_once,
    prepare_model_once,
)
from forgeharness.sdk.durable import DurableNode, _StopAdmission, _Unknown
from forgeharness.sdk.parallel import CapacityError, CapacityTicket
from forgeharness.sdk.spec import ScopeError
from forgeharness.state.checkpoint import CheckpointConflict

if TYPE_CHECKING:
    from forgeharness.sdk.durable import DurableRunner


@dataclass(frozen=True)
class _Flight:
    node: DurableNode
    ticket: CapacityTicket
    owner: str
    payload_sha256: str


async def _worker(
    *,
    model: Model,
    node_id: str,
    task_id: str,
    prepared: PreparedModelCall,
    budget: RunBudget,
    request_seconds: float,
    validate_send: Callable[[], None],
) -> tuple[ModelCallOutcome, tuple[TraceEvent, ...]]:
    # Only the provider adapter and bounded, private memory trace are shared with
    # this task. No runner state mutation or file/DB writes are permitted here.
    trace = InMemoryTrace(task_id)
    try:
        validate_send()
    except Exception as exc:
        trace.append(
            "model.dispatch_refused", {"phase": "r2:" + node_id, "error_type": type(exc).__name__}
        )
        return ModelCallOutcome(kind=ModelCallKind.PREPARATION_FAILED, not_sent=True), trace.events
    try:
        trace.append("model.provider_entered", {"phase": "r2:" + node_id})
        outcome = await asyncio.wait_for(
            dispatch_model_once(
                model=model,
                prepared=prepared,
                budget=budget,
                trace=trace,
                usage=Usage(),
                phase="r2:" + node_id,
            ),
            timeout=request_seconds,
        )
    except TimeoutError:
        trace.append("model.failed", {"phase": "r2:" + node_id, "error_type": "TimeoutError"})
        outcome = ModelCallOutcome(kind=ModelCallKind.MODEL_FAILED, error_type="TimeoutError")
    return outcome, trace.events


def _send_guard(runner: DurableRunner, ticket: CapacityTicket) -> Callable[[], None]:
    """Read-only boundary check immediately before the provider is entered."""
    harness, context, orchestrator, store = (
        runner.harness,
        runner.context,
        runner.orchestrator,
        runner.store,
    )
    owner, identity, messages = (
        runner.state.owner,
        runner.state.identity,
        runner.state.result.messages,
    )

    def check() -> None:
        harness._validate(context)
        if orchestrator.identity(harness, context, messages) != identity:
            raise ScopeError("model sending identity changed")
        current = store.load(ticket.run_id)
        if current is None or (current.owner, current.identity) != (owner, identity):
            raise CheckpointConflict("model sending owner changed")
        progress = next(n for n in current.nodes if n.id == ticket.node_id)
        if progress.invocation_id != ticket.invocation_id or progress.dispatch_phase != "sending":
            raise CheckpointConflict("model sending permit changed")
        now = datetime.now(UTC)
        if current.cancel_requested or (current.deadline is not None and now >= current.deadline):
            raise _StopAdmission("model sending was cancelled or expired")
        if now < current.observed_at:
            raise ScopeError("model sending clock moved backwards")

    return check


def _settle(
    runner: DurableRunner,
    flight: _Flight,
    result: tuple[ModelCallOutcome, tuple[TraceEvent, ...]],
) -> bool:
    runner._guard_accounting()
    node = flight.node
    progress = runner._progress(node.id)
    if (runner.state.owner, progress.invocation_id, progress.prepared_payload_sha256) != (
        flight.owner,
        flight.ticket.invocation_id,
        flight.payload_sha256,
    ):
        raise CheckpointConflict("stale or conflicting model completion ticket")
    outcome, events = result
    known = _known_result(outcome)
    dispatcher = runner.orchestrator.dispatcher
    assert dispatcher is not None
    if progress.status in {"completed", "failed"}:
        # A commit may succeed before outbox publication raises. Retrying local
        # cleanup must not append/charge the completed response again.
        if known is None or (progress.output, progress.used) != (known[0], known[1]):
            raise CheckpointConflict("conflicting duplicate model response or usage")
        dispatcher.finish(flight.ticket, confirmed=outcome.response_received or outcome.not_sent)
        return progress.status != "completed"
    if progress.status != "unknown":
        raise CheckpointConflict("model completion requires a sent invocation")
    for event in events:
        runner.trace.append(
            event.type,
            {
                **event.payload,
                "node_id": node.id,
                "invocation_id": flight.ticket.invocation_id,
                "owner": flight.owner,
                "provider_observed_at": event.timestamp.isoformat(),
            },
        )
    if known is not None:
        runner._complete(node, known[0], known[1], failed=known[2], accounting=True)
    else:
        runner._mark_unknown(
            node, "model response or usage requires reconciliation", accounting=True
        )
    dispatcher.finish(flight.ticket, confirmed=outcome.response_received or outcome.not_sent)
    return runner._progress(node.id).status != "completed"


def _known_result(outcome: ModelCallOutcome) -> tuple[dict[str, Any], Usage, bool] | None:
    if outcome.not_sent:
        return {"error": "dispatch_refused"}, Usage(), True
    if outcome.kind is ModelCallKind.SUCCESS and outcome.result is not None:
        return (
            outcome.result.model_dump(mode="json"),
            outcome.usage,
            not isinstance(outcome.result.action, FinalAction),
        )
    reported = getattr(outcome.violation, "usage", None)
    if outcome.response_received and isinstance(reported, ModelUsage):
        usage = ModelUsage.model_validate(reported.model_dump())
        return (
            {"error": "protocol_violation"},
            Usage(steps=1, input_tokens=usage.input_tokens, output_tokens=usage.output_tokens),
            True,
        )
    return None


async def advance_parallel(runner: DurableRunner) -> RunResult:
    """Own all durable mutations; poll control while provider/capacity is awaited."""
    dispatcher = runner.orchestrator.dispatcher
    assert dispatcher is not None
    policy = runner.orchestrator.execution
    loop = asyncio.get_running_loop()
    active: dict[asyncio.Task[tuple[ModelCallOutcome, tuple[TraceEvent, ...]]], _Flight] = {}
    waiting: tuple[DurableNode, CapacityTicket, asyncio.Future[CapacityTicket]] | None = None
    stop: str | None = None
    raised: BaseException | None = None
    halting = any(n.status == "failed" for n in runner.state.nodes)
    drain_until: float | None = None

    # Any sending marker left by an earlier owner is uncertain, not retryable.
    for node in runner.orchestrator.graph.nodes:
        progress = runner._progress(node.id)
        if (
            progress.status in {"completed", "failed"}
            and progress.invocation_id in dispatcher.unresolved
        ):
            assert progress.invocation_id is not None
            dispatcher.confirm_ended(
                progress.invocation_id,
                evidence_refs=(f"r2-checkpoint:{node.id}:{runner.state.revision}",),
            )
        if progress.status == "unknown":
            runner._mark_unknown(
                node, "previous owner sent this call; reconcile only", accounting=True
            )
            halting = True

    try:
        while True:
            try:
                runner._guard()
            except _StopAdmission as exc:
                stop, halting = str(exc), True
            except BaseException as exc:
                raised, halting = exc, True

            # Commit returned facts first, including costs after revoke/cancel.
            for task in tuple(active):
                if task.done():
                    flight = active[task]
                    if task.cancelled():
                        runner._mark_unknown(flight.node, "local waiter cancelled", accounting=True)
                        dispatcher.finish(flight.ticket, confirmed=False)
                        halting = True
                    else:
                        halting = _settle(runner, flight, task.result()) or halting
                    del active[task]

            if halting and waiting is not None:
                node, ticket, future = waiting
                if future.done() and not future.cancelled():
                    future.exception()  # consume a queued admission failure, if any
                dispatcher.withdraw(ticket)
                waiting = None

            if halting:
                if not active:
                    break
                if drain_until is None:
                    drain_until = loop.time() + policy.drain_timeout_seconds
                if loop.time() >= drain_until:
                    break
            else:
                if waiting is not None and waiting[2].done():
                    node, ticket, future = waiting
                    waiting = None
                    try:
                        future.result()
                        runner._guard()
                        if runner._progress(node.id).status != "admitted":
                            runner._admit(node)
                        budget = runner._node_budget(node)
                        prepared = prepare_model_once(
                            model=runner.model,
                            request=ModelRequest(
                                task_id=runner.internal.run_id,
                                messages=runner._messages(node),
                                tools=(),
                            ),
                            budget=budget,
                            assembler=ContextAssembler(max_tokens=budget.max_context_tokens),
                            trace=runner.trace,
                            usage=Usage(),
                            phase="r2:" + node.id,
                        )
                        if isinstance(prepared, ModelCallOutcome):
                            runner._complete(
                                node,
                                {"error": prepared.kind.value},
                                Usage(),
                                failed=True,
                                accounting=True,
                            )
                            dispatcher.withdraw(ticket)
                            halting = True
                            continue
                        runner._guard()
                        dispatcher.sent(ticket)
                        runner._replace(
                            runner._progress(node.id).model_copy(
                                update={
                                    "status": "unknown",
                                    "dispatch_phase": "sending",
                                    "used": Usage(steps=1),
                                    "invocation_id": ticket.invocation_id,
                                    "prepared_payload_sha256": prepared.payload_sha256,
                                    "sent_at": datetime.now(UTC),
                                }
                            )
                        )
                        runner._save("model.sending:" + node.id)
                        runner.trace.append(
                            "orchestration.dispatch",
                            {
                                "node_id": node.id,
                                "invocation_id": ticket.invocation_id,
                                "owner": runner.state.owner,
                                "prepared_payload_sha256": prepared.payload_sha256,
                            },
                        )
                        task = asyncio.create_task(
                            _worker(
                                model=runner.model,
                                node_id=node.id,
                                task_id=runner.internal.run_id,
                                prepared=prepared,
                                budget=budget,
                                request_seconds=policy.request_timeout_seconds,
                                validate_send=_send_guard(runner, ticket),
                            )
                        )
                        dispatcher.watch_worker(task)
                        active[task] = _Flight(
                            node, ticket, runner.state.owner, prepared.payload_sha256
                        )
                    except (CapacityError, _StopAdmission) as exc:
                        dispatcher.withdraw(ticket)
                        stop, halting = str(exc), True
                    except BaseException:
                        dispatcher.withdraw(ticket)
                        if runner._progress(node.id).dispatch_phase == "sending":
                            dispatcher.finish(ticket, confirmed=False)
                        raise
                    continue

                if waiting is None and len(active) < policy.max_in_flight:
                    ready = [
                        node
                        for node in runner.orchestrator.graph.nodes
                        if runner._progress(node.id).status in {"pending", "admitted"}
                        and all(
                            runner._progress(dep).status == "completed" for dep in node.dependencies
                        )
                    ]
                    if ready:
                        node = DurableNode.model_validate_json(ready[0].model_dump_json())
                        if node.kind == "join":
                            runner._complete(
                                node,
                                {dep: runner._progress(dep).output for dep in node.dependencies},
                                Usage(),
                            )
                            continue
                        if node.kind == "tool":
                            if not active:
                                call = runner._call(node)
                                if runner._progress(node.id).status != "admitted":
                                    from forgeharness.tools.policy import PolicyDecisionType

                                    if (
                                        runner._authorize(node, call)
                                        is PolicyDecisionType.REQUIRE_APPROVAL
                                    ):
                                        from forgeharness.sdk.durable import digest

                                        runner._replace(
                                            runner._progress(node.id).model_copy(
                                                update={
                                                    "status": "awaiting_approval",
                                                    "output": call.model_dump(mode="json"),
                                                    "input_digest": digest(
                                                        call.model_dump(mode="json")
                                                    ),
                                                }
                                            )
                                        )
                                        runner._save("approval.pending:" + node.id)
                                        continue
                                    try:
                                        runner._admit(node, call=call)
                                    except _StopAdmission as exc:
                                        stop, halting = str(exc), True
                                        continue
                                try:
                                    await runner._execute(node)
                                except _Unknown as exc:
                                    runner._mark_unknown(node, str(exc))
                                    halting = True
                                except _StopAdmission as exc:
                                    stop, halting = str(exc), True
                                halting = halting or runner._progress(node.id).status == "failed"
                                continue
                            # Tool at the front is an exclusive admission barrier.
                        else:
                            invocation = runner._model_invocation_id(node.id)
                            ticket = CapacityTicket(invocation, runner.internal.run_id, node.id)
                            runner._replace(
                                runner._progress(node.id).model_copy(
                                    update={
                                        "dispatch_phase": "queued",
                                        "invocation_id": invocation,
                                        "queued_at": datetime.now(UTC),
                                    }
                                )
                            )
                            runner._save("capacity.queued:" + node.id)
                            try:
                                waiting = (node, ticket, dispatcher.request(ticket))
                            except CapacityError as exc:
                                stop, halting = str(exc), True
                            continue
                    elif not active:
                        break

            if not active and waiting is None:
                break
            handles: set[asyncio.Future[Any]] = set(active)
            if waiting is not None:
                handles.add(waiting[2])
            try:
                await asyncio.wait(
                    handles,
                    timeout=policy.poll_interval_seconds,
                    return_when=asyncio.FIRST_COMPLETED,
                )
            except asyncio.CancelledError as exc:
                runner.store.request_cancel(runner.internal.run_id)
                raised, halting = exc, True
    finally:
        if waiting is not None:
            dispatcher.withdraw(waiting[1])
        # No worker writes run state or a disk trace after owner exit. Keep sent
        # capacity if the adapter cannot prove physical completion.
        if active:
            for task in active:
                task.cancel()
            await asyncio.wait(active, timeout=policy.poll_interval_seconds)
            cleanup_error: BaseException | None = None
            for task, flight in active.items():
                returned = task.done() and not task.cancelled() and task.exception() is None
                confirmed = returned and (
                    task.result()[0].response_received or task.result()[0].not_sent
                )
                try:
                    if returned:
                        _settle(runner, flight, task.result())
                    else:
                        if task.done() and not task.cancelled():
                            task.exception()
                        runner._mark_unknown(
                            flight.node, "owner stopped waiting; reconcile only", accounting=True
                        )
                except BaseException as exc:
                    cleanup_error = cleanup_error or exc
                finally:
                    # A stale owner cannot write the run ledger, but it can still
                    # account for a physically returned provider capacity ticket.
                    dispatcher.finish(flight.ticket, confirmed=confirmed)
            if cleanup_error is not None:
                raise cleanup_error
    if raised is not None:
        if isinstance(raised, asyncio.CancelledError):
            try:
                await runner._finish("cancelled")
            except ScopeError:
                pass  # revoked callers receive no new projection
        raise raised
    return await runner._finish(stop)
