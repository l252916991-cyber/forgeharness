"""Durable task records, cancel semantics and the retry matrix (ADR 015, phase S2).

These tests drive :class:`TaskStore` and :class:`TaskRunner` directly: request
identity and binding, the exclusive claim, cancellation that never pretends to be a
rollback, retry decided by effect class, restart recovery through reconciliation,
progress bounds, and schema refusal. Every fixture is synthetic and deterministic:
no model, tenant or business data is involved, and no handler touches the network.
"""

import gc
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from forgeharness.sdk import Actor, ResourceScope, RunContext
from forgeharness.sdk.application import ScopeError
from forgeharness.sdk.tasks import (
    CANCEL_NOTE,
    MAX_ERROR_CHARS,
    MAX_KIND_CHARS,
    MAX_LIST_LIMIT,
    MAX_REQUEST_ID_CHARS,
    MAX_RESULT_CHARS,
    TaskConflict,
    TaskOperation,
    TaskOutcome,
    TaskRecord,
    TaskRunner,
    TaskState,
    TaskStore,
    TaskUnavailable,
    _digest,
)
from forgeharness.tools.base import ToolEffectClass, ToolErrorCode

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)


class Clock:
    """Synthetic monotonic clock: every read is one minute later than the last."""

    def __init__(self) -> None:
        self.reads = 0

    def __call__(self) -> datetime:
        self.reads += 1
        return NOW + timedelta(minutes=self.reads)


class Authorizer:
    """Records every decision so tests can prove re-authorization happened."""

    def __init__(self, allowed: bool = True) -> None:
        self.allowed = allowed
        self.calls: list[tuple[TaskOperation, str | None]] = []

    def __call__(
        self, context: RunContext, operation: TaskOperation, task: TaskRecord | None
    ) -> bool:
        del context
        self.calls.append((operation, None if task is None else task.id))
        return self.allowed

    def operations(self) -> set[TaskOperation]:
        return {operation for operation, _ in self.calls}


class Handler:
    """Deterministic in-test handler: reports, raises, or cancels itself mid-run."""

    def __init__(
        self,
        kind: str = "import",
        *,
        outcome: TaskOutcome | None = None,
        error: BaseException | None = None,
        cancel: tuple[TaskStore, RunContext] | None = None,
    ) -> None:
        self.kind = kind
        self.calls = 0
        self.outcome = TaskOutcome() if outcome is None else outcome
        self.error = error
        self.cancel = cancel

    async def run(self, context: RunContext, task: TaskRecord) -> TaskOutcome:
        del context
        self.calls += 1
        if self.cancel is not None:
            self.cancel[0].request_cancel(self.cancel[1], task.id)
        if self.error is not None:
            raise self.error
        return self.outcome


def context(**changes: object) -> RunContext:
    values: dict[str, object] = {
        "application_id": "catalogue",
        "application_version": "1",
        "policy_version": "1",
        "run_id": "r1",
        "session_id": "s1",
        "actor": Actor(id="owner", roles=("use",)),
        "scope": ResourceScope(tenant_id="shop-a", resources=("product:a",)),
        "correlation_id": "c1",
    }
    values.update(changes)
    return RunContext.model_validate(values)


def new_store(directory: Path, **options: object) -> TaskStore:
    return TaskStore(directory, authorize=Authorizer(), clock=Clock(), **options)


def create(
    service: TaskStore,
    *,
    request_id: str = "r-1",
    kind: str = "import",
    effect_class: ToolEffectClass = ToolEffectClass.NON_IDEMPOTENT,
    business_key: str | None = None,
    progress_total: int = 0,
    identity: RunContext | None = None,
) -> TaskRecord:
    return service.create(
        context() if identity is None else identity,
        kind=kind,
        request_id=request_id,
        business_key=f"key:{request_id}" if business_key is None else business_key,
        effect_class=effect_class,
        progress_total=progress_total,
    )


def claim_new(
    service: TaskStore,
    *,
    request_id: str = "r-1",
    kind: str = "import",
    effect_class: ToolEffectClass = ToolEffectClass.NON_IDEMPOTENT,
    progress_total: int = 0,
) -> TaskRecord:
    record = create(
        service,
        request_id=request_id,
        kind=kind,
        effect_class=effect_class,
        progress_total=progress_total,
    )
    return service.claim(context(), record.id)


# -- identity and stability (invariant 1) ---------------------------------------


def test_create_is_idempotent_for_one_request_identity(tmp_path: Path) -> None:
    service = new_store(tmp_path / "tasks")
    first = create(service, request_id="r-1", business_key="product:a:import")
    second = create(service, request_id="r-1", business_key="product:a:import")
    assert second.id == first.id
    assert second.created_at == first.created_at
    assert second.updated_at == first.updated_at
    assert second.state is TaskState.QUEUED
    assert len(service.list(context())) == 1
    with pytest.raises(TaskConflict, match="request identity reused"):
        create(service, request_id="r-1", business_key="another-key")
    with pytest.raises(TaskConflict, match="request identity reused"):
        create(service, request_id="r-1", kind="parse", business_key="product:a:import")


def test_business_key_cannot_belong_to_a_second_request(tmp_path: Path) -> None:
    service = new_store(tmp_path / "tasks")
    create(service, request_id="r-1", business_key="stable")
    with pytest.raises(TaskConflict, match="business key already belongs"):
        create(service, request_id="r-2", business_key="stable")
    assert len(service.list(context())) == 1


def test_task_id_is_deterministic_across_stores_and_restarts(tmp_path: Path) -> None:
    service = new_store(tmp_path / "tasks")
    record = create(service, request_id="r-1", business_key="key")
    assert record.id == _digest(["catalogue", "shop-a", "r-1"])
    assert new_store(tmp_path / "tasks").get(context(), record.id).id == record.id
    # A different request, tenant or application is a different identity space.
    assert create(service, request_id="r-2").id != record.id
    tenant = ResourceScope(tenant_id="shop-b", resources=("product:a",))
    assert create(service, request_id="r-1", identity=context(scope=tenant)).id != record.id
    assert (
        create(service, request_id="r-1", identity=context(application_id="other")).id != record.id
    )


async def test_duplicate_create_after_completion_returns_the_settled_record(
    tmp_path: Path,
) -> None:
    service = new_store(tmp_path / "tasks")
    handler = Handler(kind="import", outcome=TaskOutcome(result='{"rows": 3}'))
    runner = TaskRunner(service, handlers=(handler,))
    record = create(service, request_id="r-1", business_key="key")
    settled = await runner.run_once(context(), record.id)
    again = create(service, request_id="r-1", business_key="key")
    assert again.id == record.id
    assert again.state is TaskState.SUCCEEDED
    assert again.result == '{"rows": 3}'
    assert settled.attempts == 1
    assert handler.calls == 1


# -- binding and current authorization (invariant 2) ----------------------------


def test_cross_tenant_context_cannot_read_or_mutate_a_task(tmp_path: Path) -> None:
    service = new_store(tmp_path / "tasks")
    record = create(service, request_id="r-1")
    foreign = context(scope=ResourceScope(tenant_id="shop-b", resources=("product:a",)))
    attempts = (
        lambda: service.get(foreign, record.id),
        lambda: service.claim(foreign, record.id),
        lambda: service.progress(foreign, record.id, done=1),
        lambda: service.complete(foreign, record.id),
        lambda: service.fail(foreign, record.id, error="boom", error_code=ToolErrorCode.TIMEOUT),
        lambda: service.request_cancel(foreign, record.id),
        lambda: service.reconcile(foreign, record.id, state=TaskState.SUCCEEDED),
        lambda: service.delete(foreign, record.id),
    )
    for attempt in attempts:
        with pytest.raises(TaskUnavailable, match="binding"):
            attempt()
    assert service.list(foreign) == ()
    assert service.get(context(), record.id).state is TaskState.QUEUED


def test_other_actor_and_application_bindings_are_refused(tmp_path: Path) -> None:
    service = new_store(tmp_path / "tasks")
    record = create(service, request_id="r-1")
    intruder = context(actor=Actor(id="intruder", roles=("use",)))
    with pytest.raises(TaskUnavailable, match="binding"):
        service.get(intruder, record.id)
    # Reusing the same request id must not hand the record to another actor.
    with pytest.raises(TaskUnavailable, match="binding"):
        create(service, request_id="r-1", identity=intruder)
    # Another application is a separate identity space: it can neither read this
    # record nor collide with it, and its own request id stays independent.
    other = context(application_id="another-application")
    with pytest.raises(TaskUnavailable, match="binding"):
        service.get(other, record.id)
    assert create(service, request_id="r-1", identity=other).id != record.id
    assert service.get(context(), record.id).state is TaskState.QUEUED


def test_credential_bearing_bindings_and_identities_are_refused(tmp_path: Path) -> None:
    service = new_store(tmp_path / "tasks")
    with pytest.raises(TaskUnavailable, match="credentials in identity"):
        service.create(
            context(correlation_id="api_key: sk-abcdefghijkl"),
            kind="import",
            request_id="r-1",
            business_key="key",
        )
    with pytest.raises(ValueError, match="must not contain credentials"):
        service.create(
            context(),
            kind="import",
            request_id="token: ghp_abcdefghijkl",
            business_key="key",
        )
    assert service.list(context()) == ()


def test_revoked_authorization_stops_every_operation(tmp_path: Path) -> None:
    authorizer = Authorizer()
    service = TaskStore(tmp_path / "tasks", authorize=authorizer, clock=Clock())
    record = create(service, request_id="r-1")
    authorizer.allowed = False
    for attempt in (
        lambda: service.create(context(), kind="import", request_id="r-2", business_key="key:r-2"),
        lambda: service.get(context(), record.id),
        lambda: service.list(context()),
        lambda: service.claim(context(), record.id),
        lambda: service.progress(context(), record.id, done=1),
        lambda: service.request_cancel(context(), record.id),
        lambda: service.delete(context(), record.id),
    ):
        with pytest.raises(TaskUnavailable, match="permission denied"):
            attempt()
    # Nothing moved: the durable state is decided by the store, not by the attempt.
    assert new_store(tmp_path / "tasks").get(context(), record.id).state is TaskState.QUEUED


async def test_every_operation_is_re_authorized_through_the_host_callback(
    tmp_path: Path,
) -> None:
    authorizer = Authorizer()
    service = TaskStore(tmp_path / "tasks", authorize=authorizer, clock=Clock())
    runner = TaskRunner(service, handlers=(Handler(kind="import"),))
    first = create(service, request_id="r-1")
    await runner.run_once(context(), first.id)
    service.request_cancel(context(), create(service, request_id="r-2").id)
    service.delete(context(), create(service, request_id="r-3").id)
    assert authorizer.operations() == {
        TaskOperation.CREATE,
        TaskOperation.READ,
        TaskOperation.RUN,
        TaskOperation.CANCEL,
        TaskOperation.DELETE,
    }
    before = len(authorizer.calls)
    service.get(context(), first.id)
    assert len(authorizer.calls) == before + 1
    assert authorizer.calls[-1] == (TaskOperation.READ, first.id)


# -- single writer (invariant 3) ------------------------------------------------


def test_second_claim_of_a_queued_task_is_refused(tmp_path: Path) -> None:
    claimer = new_store(tmp_path / "tasks")
    other = new_store(tmp_path / "tasks")
    record = create(claimer, request_id="r-1")
    claimed = claimer.claim(context(), record.id)
    assert claimed.state is TaskState.RUNNING
    assert claimed.attempts == 0
    with pytest.raises(TaskConflict, match="only a queued task"):
        other.claim(context(), record.id)
    with pytest.raises(TaskConflict, match="only a queued task"):
        claimer.claim(context(), record.id)


def test_a_claim_lost_to_a_concurrent_worker_is_refused_by_the_lock(
    tmp_path: Path,
) -> None:
    """Two workers that both read `queued`: the loser must not run the same task."""
    racer = new_store(tmp_path / "tasks")

    class Racing:
        def __call__(
            self, context: RunContext, operation: TaskOperation, task: TaskRecord | None
        ) -> bool:
            if operation is TaskOperation.RUN and task is not None:
                racer.claim(context, task.id)
            return True

    service = TaskStore(tmp_path / "tasks", authorize=Racing(), clock=Clock())
    record = create(service, request_id="r-1")
    with pytest.raises(TaskConflict, match="another worker"):
        service.claim(context(), record.id)
    assert racer.get(context(), record.id).state is TaskState.RUNNING
    assert service.get(context(), record.id).state is TaskState.RUNNING


async def test_a_cancel_landing_during_claim_authorization_refuses_the_claim(
    tmp_path: Path,
) -> None:
    """A cancel that wins the race must not be revived by an in-flight claim."""
    holder: dict[str, TaskStore] = {}

    class Cancelling:
        def __call__(
            self, context: RunContext, operation: TaskOperation, task: TaskRecord | None
        ) -> bool:
            if (
                operation is TaskOperation.RUN
                and task is not None
                and task.state is TaskState.QUEUED
            ):
                holder["store"].request_cancel(context, task.id)
            return True

    service = TaskStore(tmp_path / "tasks", authorize=Cancelling(), clock=Clock())
    holder["store"] = service
    handler = Handler(kind="import")
    runner = TaskRunner(service, handlers=(handler,))
    record = create(service, request_id="r-1")
    with pytest.raises(TaskConflict, match="left the queue"):
        await runner.run_once(context(), record.id)
    assert handler.calls == 0
    assert service.get(context(), record.id).state is TaskState.CANCELLED


def test_only_the_claimer_can_report_progress_or_settle(tmp_path: Path) -> None:
    claimer = new_store(tmp_path / "tasks")
    other = new_store(tmp_path / "tasks")
    record = claim_new(claimer, request_id="r-1")
    for attempt in (
        lambda: other.progress(context(), record.id, done=1),
        lambda: other.complete(context(), record.id),
        lambda: other.fail(context(), record.id, error="boom", error_code=ToolErrorCode.TIMEOUT),
    ):
        with pytest.raises(TaskConflict, match="another worker"):
            attempt()
    assert claimer.progress(context(), record.id, done=1).progress_done == 1
    assert claimer.complete(context(), record.id).state is TaskState.SUCCEEDED


def test_a_settled_attempt_releases_the_claim_for_the_retry(tmp_path: Path) -> None:
    claimer = new_store(tmp_path / "tasks")
    other = new_store(tmp_path / "tasks")
    record = claim_new(claimer, request_id="r-1", effect_class=ToolEffectClass.IDEMPOTENT)
    requeued = claimer.fail(
        context(), record.id, error="timed out", error_code=ToolErrorCode.TIMEOUT
    )
    assert requeued.state is TaskState.QUEUED
    assert requeued.attempts == 1
    assert other.claim(context(), record.id).state is TaskState.RUNNING
    with pytest.raises(TaskConflict, match="another worker"):
        claimer.progress(context(), record.id, done=1)


# -- cancellation (invariant 4) -------------------------------------------------


async def test_cancel_before_start_settles_queued_without_running_the_handler(
    tmp_path: Path,
) -> None:
    service = new_store(tmp_path / "tasks")
    handler = Handler(kind="import")
    runner = TaskRunner(service, handlers=(handler,))
    record = create(service, request_id="r-1")
    cancelled = service.request_cancel(context(), record.id)
    assert cancelled.state is TaskState.CANCELLED
    assert cancelled.cancel_requested is True
    assert cancelled.progress_done == 0
    assert cancelled.progress_total == 0
    assert CANCEL_NOTE in str(cancelled.error)
    with pytest.raises(TaskConflict, match="only a queued task"):
        await runner.run_once(context(), record.id)
    assert handler.calls == 0
    assert service.recoverable() == ()


async def test_cancel_during_run_of_an_idempotent_task_settles_cancelled(
    tmp_path: Path,
) -> None:
    service = new_store(tmp_path / "tasks")
    handler = Handler(
        kind="import", error=TimeoutError("write timed out"), cancel=(service, context())
    )
    runner = TaskRunner(service, handlers=(handler,))
    record = create(service, request_id="r-1", effect_class=ToolEffectClass.IDEMPOTENT)
    settled = await runner.run_once(context(), record.id)
    assert settled.state is TaskState.CANCELLED
    assert settled.cancel_requested is True
    assert settled.error_code is None
    assert CANCEL_NOTE in str(settled.error)
    assert "TimeoutError" in str(settled.error)
    assert settled.attempts == 1


async def test_cancel_during_run_of_a_non_idempotent_task_needs_reconciliation(
    tmp_path: Path,
) -> None:
    service = new_store(tmp_path / "tasks")
    handler = Handler(
        kind="import", error=TimeoutError("write timed out"), cancel=(service, context())
    )
    runner = TaskRunner(service, handlers=(handler,))
    record = create(service, request_id="r-1")
    settled = await runner.run_once(context(), record.id)
    assert settled.state is TaskState.NEEDS_RECONCILIATION
    assert settled.cancel_requested is True
    assert settled.error_code is ToolErrorCode.TIMEOUT
    assert CANCEL_NOTE in str(settled.error)
    assert not settled.terminal
    assert [item.id for item in service.recoverable()] == [record.id]


async def test_a_handler_that_finishes_after_a_cancel_request_still_succeeds(
    tmp_path: Path,
) -> None:
    service = new_store(tmp_path / "tasks")
    handler = Handler(
        kind="import",
        outcome=TaskOutcome(result='{"rows": 2}'),
        cancel=(service, context()),
    )
    runner = TaskRunner(service, handlers=(handler,))
    record = create(service, request_id="r-1")
    settled = await runner.run_once(context(), record.id)
    assert settled.state is TaskState.SUCCEEDED
    assert settled.cancel_requested is True
    assert settled.result == '{"rows": 2}'
    assert settled.error is None


def test_cancellation_is_recorded_as_a_stop_and_not_a_rollback(tmp_path: Path) -> None:
    service = new_store(tmp_path / "tasks")
    record = create(service, request_id="r-1")
    cancelled = service.request_cancel(context(), record.id)
    resumed = new_store(tmp_path / "tasks").get(context(), record.id)
    assert resumed.state is TaskState.CANCELLED
    assert resumed.cancel_requested is True
    assert resumed.error == CANCEL_NOTE
    assert "not a rollback" in CANCEL_NOTE
    assert "rollback" in str(resumed.error)
    assert resumed.attempts == cancelled.attempts == 0


async def test_cancel_request_survives_a_requeue_and_stops_the_next_claim(
    tmp_path: Path,
) -> None:
    service = new_store(tmp_path / "tasks")
    handler = Handler(kind="import")
    runner = TaskRunner(service, handlers=(handler,))
    record = claim_new(service, request_id="r-1")
    requested = service.request_cancel(context(), record.id)
    assert requested.state is TaskState.RUNNING
    assert requested.cancel_requested is True
    requeued = service.reconcile(context(), record.id, state=TaskState.QUEUED)
    assert requeued.state is TaskState.QUEUED
    assert requeued.cancel_requested is True
    settled = await runner.run_once(context(), record.id)
    assert settled.state is TaskState.CANCELLED
    assert handler.calls == 0


def test_cancel_of_a_settled_or_unreconciled_task_is_refused(tmp_path: Path) -> None:
    service = new_store(tmp_path / "tasks")
    done = claim_new(service, request_id="r-1")
    service.complete(context(), done.id)
    with pytest.raises(TaskConflict, match="cannot be cancelled"):
        service.request_cancel(context(), done.id)
    unknown = claim_new(service, request_id="r-2")
    service.fail(context(), unknown.id, error="timed out", error_code=ToolErrorCode.TIMEOUT)
    with pytest.raises(TaskConflict, match="cannot be cancelled"):
        service.request_cancel(context(), unknown.id)
    with pytest.raises(TaskUnavailable, match="task unavailable"):
        service.request_cancel(context(), "missing")


# -- retry decided by effect class (invariant 5) --------------------------------


def test_transient_failure_of_an_idempotent_task_returns_to_the_queue(
    tmp_path: Path,
) -> None:
    service = new_store(tmp_path / "tasks")
    record = claim_new(service, request_id="r-1", effect_class=ToolEffectClass.IDEMPOTENT)
    requeued = service.fail(
        context(), record.id, error="timed out", error_code=ToolErrorCode.TIMEOUT
    )
    assert requeued.state is TaskState.QUEUED
    assert requeued.attempts == 1
    assert requeued.id == record.id
    assert requeued.business_key == record.business_key
    assert requeued.error == "timed out"
    assert requeued.error_code is ToolErrorCode.TIMEOUT
    assert requeued.cancel_requested is False


def test_read_only_task_retries_a_timeout(tmp_path: Path) -> None:
    service = new_store(tmp_path / "tasks")
    record = claim_new(service, request_id="r-1", effect_class=ToolEffectClass.READ_ONLY)
    requeued = service.fail(
        context(), record.id, error="timed out", error_code=ToolErrorCode.TIMEOUT
    )
    assert requeued.state is TaskState.QUEUED
    assert requeued.attempts == 1


@pytest.mark.parametrize(
    "error_code",
    [ToolErrorCode.TIMEOUT, ToolErrorCode.TOOL_EXECUTION_ERROR],
)
def test_non_idempotent_failure_needs_reconciliation(
    tmp_path: Path, error_code: ToolErrorCode
) -> None:
    service = new_store(tmp_path / "tasks")
    record = claim_new(service, request_id="r-1")
    unknown = service.fail(context(), record.id, error="failed mid write", error_code=error_code)
    assert unknown.state is TaskState.NEEDS_RECONCILIATION
    assert unknown.attempts == 1
    assert unknown.error_code is error_code
    assert unknown.terminal is False


@pytest.mark.parametrize(
    ("effect_class", "error_code"),
    [
        (ToolEffectClass.IDEMPOTENT, ToolErrorCode.INVALID_ARGUMENTS),
        (ToolEffectClass.IDEMPOTENT, ToolErrorCode.PATH_NOT_FOUND),
        (ToolEffectClass.READ_ONLY, ToolErrorCode.TOOL_EXECUTION_ERROR),
        (ToolEffectClass.NON_IDEMPOTENT, ToolErrorCode.INVALID_ARGUMENTS),
    ],
)
def test_deterministic_failures_fail_immediately(
    tmp_path: Path, effect_class: ToolEffectClass, error_code: ToolErrorCode
) -> None:
    service = new_store(tmp_path / "tasks")
    record = claim_new(service, request_id="r-1", effect_class=effect_class)
    failed = service.fail(context(), record.id, error="bad request", error_code=error_code)
    assert failed.state is TaskState.FAILED
    assert failed.attempts == 1
    assert failed.terminal is True
    assert service.recoverable() == ()


def test_retry_budget_exhaustion_fails_the_task(tmp_path: Path) -> None:
    service = new_store(tmp_path / "tasks", max_attempts=2)
    record = create(service, request_id="r-1", effect_class=ToolEffectClass.IDEMPOTENT)
    for expected_attempt in (1, 2):
        service.claim(context(), record.id)
        failed = service.fail(
            context(), record.id, error="timed out", error_code=ToolErrorCode.TIMEOUT
        )
        assert failed.attempts == expected_attempt
    assert failed.state is TaskState.FAILED
    assert failed.id == record.id
    with pytest.raises(TaskConflict, match="only a queued task"):
        service.claim(context(), record.id)


async def test_runner_maps_a_handler_timeout_to_a_retry(tmp_path: Path) -> None:
    service = new_store(tmp_path / "tasks")
    handler = Handler(kind="import", error=TimeoutError("slow source"))
    runner = TaskRunner(service, handlers=(handler,))
    record = create(service, request_id="r-1", effect_class=ToolEffectClass.IDEMPOTENT)
    settled = await runner.run_once(context(), record.id)
    assert settled.state is TaskState.QUEUED
    assert settled.attempts == 1
    assert settled.error_code is ToolErrorCode.TIMEOUT
    assert "TimeoutError" in str(settled.error)
    assert handler.calls == 1


# -- restart recovery (invariant 6) ---------------------------------------------


def test_recoverable_returns_only_non_terminal_records_after_restart(
    tmp_path: Path,
) -> None:
    service = new_store(tmp_path / "tasks")
    create(service, request_id="r-queued")
    running = claim_new(service, request_id="r-running")
    unknown = claim_new(service, request_id="r-unknown")
    service.fail(context(), unknown.id, error="timed out", error_code=ToolErrorCode.TIMEOUT)
    done = claim_new(service, request_id="r-done")
    service.complete(context(), done.id)
    cancelled = create(service, request_id="r-cancelled")
    service.request_cancel(context(), cancelled.id)
    restarted = new_store(tmp_path / "tasks")
    assert [item.id for item in restarted.recoverable()] == [running.id, unknown.id]
    assert [item.state for item in restarted.recoverable()] == [
        TaskState.RUNNING,
        TaskState.NEEDS_RECONCILIATION,
    ]


async def test_reconcile_settles_a_finished_run_without_re_running_the_handler(
    tmp_path: Path,
) -> None:
    def crashing_worker(directory: Path) -> str:
        """Claim a task in a worker scope that then disappears, like a crashed process.

        Dropping the store closes its lock file descriptor, which is exactly what the
        operating system does to a process that died before it settled the record.
        """
        worker = new_store(directory)
        record = create(worker, request_id="r-1")
        worker.claim(context(), record.id)
        return record.id

    task_id = crashing_worker(tmp_path / "tasks")
    gc.collect()
    restarted = new_store(tmp_path / "tasks")
    assert [item.id for item in restarted.recoverable()] == [task_id]
    handler = Handler(kind="import")
    runner = TaskRunner(restarted, handlers=(handler,))
    with pytest.raises(TaskConflict, match="only a queued task"):
        await runner.run_once(context(), task_id)
    assert handler.calls == 0
    settled = restarted.reconcile(
        context(), task_id, state=TaskState.SUCCEEDED, result='{"rows": 3}'
    )
    assert settled.state is TaskState.SUCCEEDED
    assert settled.result == '{"rows": 3}'
    assert settled.attempts == 1
    assert settled.error is None
    assert restarted.recoverable() == ()
    assert handler.calls == 0


def test_reconcile_refuses_a_task_a_live_worker_still_holds(tmp_path: Path) -> None:
    claimer = new_store(tmp_path / "tasks")
    other = new_store(tmp_path / "tasks")
    record = claim_new(claimer, request_id="r-1")
    with pytest.raises(TaskConflict, match="another worker"):
        other.reconcile(context(), record.id, state=TaskState.SUCCEEDED)
    assert claimer.get(context(), record.id).state is TaskState.RUNNING


def test_reconcile_requeues_an_interrupted_attempt_and_charges_the_budget(
    tmp_path: Path,
) -> None:
    service = new_store(tmp_path / "tasks", max_attempts=2)
    record = claim_new(service, request_id="r-1")
    requeued = service.reconcile(context(), record.id, state=TaskState.QUEUED)
    assert requeued.state is TaskState.QUEUED
    assert requeued.attempts == 1
    service.claim(context(), record.id)
    with pytest.raises(TaskConflict, match="budget is exhausted"):
        service.reconcile(context(), record.id, state=TaskState.QUEUED)
    with pytest.raises(ValueError, match="cannot put a task into running"):
        service.reconcile(context(), record.id, state=TaskState.RUNNING)


async def test_reconciled_task_never_runs_its_handler_again(tmp_path: Path) -> None:
    service = new_store(tmp_path / "tasks")
    record = claim_new(service, request_id="r-1")
    unknown = service.fail(
        context(), record.id, error="timed out", error_code=ToolErrorCode.TIMEOUT
    )
    assert unknown.state is TaskState.NEEDS_RECONCILIATION
    confirmed = new_store(tmp_path / "tasks").reconcile(
        context(), record.id, state=TaskState.SUCCEEDED, result="done elsewhere"
    )
    assert confirmed.state is TaskState.SUCCEEDED
    assert confirmed.attempts == 1
    handler = Handler(kind="import")
    runner = TaskRunner(service, handlers=(handler,))
    with pytest.raises(TaskConflict, match="only a queued task"):
        await runner.run_once(context(), record.id)
    assert handler.calls == 0
    assert await runner.drain(context()) == ()


# -- progress (invariant 7) -----------------------------------------------------


def test_progress_is_monotonic(tmp_path: Path) -> None:
    service = new_store(tmp_path / "tasks")
    record = claim_new(service, request_id="r-1")
    assert service.progress(context(), record.id, done=2).progress_done == 2
    assert service.progress(context(), record.id, done=2).progress_done == 2
    with pytest.raises(TaskConflict, match="monotonic"):
        service.progress(context(), record.id, done=1)
    with pytest.raises(ValueError, match="non-negative"):
        service.progress(context(), record.id, done=-1)


def test_progress_never_exceeds_a_declared_total(tmp_path: Path) -> None:
    service = new_store(tmp_path / "tasks")
    record = claim_new(service, request_id="r-1", progress_total=2)
    with pytest.raises(TaskConflict, match="declared total"):
        service.progress(context(), record.id, done=3)
    with pytest.raises(ValueError, match="total"):
        service.progress(context(), record.id, done=0, total=-1)
    # A declared total may shrink, but never below the progress already reported.
    assert service.progress(context(), record.id, done=1, total=1).progress_total == 1
    with pytest.raises(TaskConflict, match="declared total"):
        service.progress(context(), record.id, done=2, total=1)
    assert service.progress(context(), record.id, done=1).progress_done == 1
    assert service.progress(context(), record.id, done=1).progress_total == 1


@pytest.mark.parametrize("settle", ["complete", "cancel", "fail"])
def test_progress_is_rejected_after_settlement(tmp_path: Path, settle: str) -> None:
    service = new_store(tmp_path / "tasks")
    record = claim_new(service, request_id="r-1")
    if settle == "complete":
        service.complete(context(), record.id)
    elif settle == "fail":
        service.fail(context(), record.id, error="boom", error_code=ToolErrorCode.PATH_NOT_FOUND)
    else:
        service.reconcile(context(), record.id, state=TaskState.CANCELLED)
    expected = "only a running task can report progress"
    with pytest.raises(TaskConflict, match=expected):
        service.progress(context(), record.id, done=1)


async def test_runner_records_progress_from_the_handler(tmp_path: Path) -> None:
    service = new_store(tmp_path / "tasks")
    handler = Handler(
        kind="import",
        outcome=TaskOutcome(result='{"rows": 5}', progress_done=5, progress_total=5),
    )
    runner = TaskRunner(service, handlers=(handler,))
    record = create(service, request_id="r-1", progress_total=5)
    settled = await runner.run_once(context(), record.id)
    assert settled.state is TaskState.SUCCEEDED
    assert settled.progress_done == 5
    assert settled.progress_total == 5
    assert settled.result == '{"rows": 5}'


# -- bounded input (invariant 8) ------------------------------------------------


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"kind": "x" * (MAX_KIND_CHARS + 1)}, "kind"),
        ({"kind": "   "}, "kind"),
        ({"request_id": "x" * (MAX_REQUEST_ID_CHARS + 1)}, "request_id"),
        ({"business_key": ""}, "business_key"),
    ],
)
def test_unbounded_or_empty_identities_are_refused(
    tmp_path: Path, changes: dict[str, str], message: str
) -> None:
    service = new_store(tmp_path / "tasks")
    values: dict[str, str] = {
        "kind": "import",
        "request_id": "r-1",
        "business_key": "key",
    }
    values.update(changes)
    with pytest.raises(ValueError, match=message):
        service.create(context(), **values)


def test_unbounded_payloads_limits_and_options_are_refused(tmp_path: Path) -> None:
    service = new_store(tmp_path / "tasks")
    record = claim_new(service, request_id="r-1")
    with pytest.raises(ValueError, match="result"):
        service.complete(context(), record.id, result="x" * (MAX_RESULT_CHARS + 1))
    with pytest.raises(ValueError, match="error"):
        service.fail(
            context(),
            record.id,
            error="x" * (MAX_ERROR_CHARS + 1),
            error_code=ToolErrorCode.TIMEOUT,
        )
    with pytest.raises(ValueError, match="error_code"):
        service.fail(context(), record.id, error="boom", error_code="nonsense")
    with pytest.raises(ValueError, match="limit"):
        service.list(context(), limit=MAX_LIST_LIMIT + 1)
    with pytest.raises(ValueError, match="limit"):
        service.list(context(), limit=0)
    with pytest.raises(ValueError, match="state"):
        service.list(context(), state="nonsense")
    with pytest.raises(ValueError, match="effect_class"):
        service.create(
            context(),
            kind="import",
            request_id="r-2",
            business_key="key:r-2",
            effect_class="nonsense",
        )
    with pytest.raises(ValueError, match="max_attempts"):
        new_store(tmp_path / "other", max_attempts=0)
    with pytest.raises(ValueError, match="authorizer"):
        TaskStore(tmp_path / "unbound", authorize=None)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="clock"):
        TaskStore(tmp_path / "untimed", authorize=Authorizer(), clock="now")  # type: ignore[arg-type]
    assert service.get(context(), record.id).state is TaskState.RUNNING


async def test_handler_contract_violations_settle_through_the_failure_path(
    tmp_path: Path,
) -> None:
    service = new_store(tmp_path / "tasks")
    bad_progress = Handler(kind="import", outcome=TaskOutcome(progress_done=-1))
    record = create(service, request_id="r-1", effect_class=ToolEffectClass.IDEMPOTENT)
    failed = await TaskRunner(service, handlers=(bad_progress,)).run_once(context(), record.id)
    assert failed.state is TaskState.FAILED
    assert "progress" in str(failed.error)
    huge = Handler(kind="import", outcome=TaskOutcome(result="x" * (MAX_RESULT_CHARS + 1)))
    other = create(service, request_id="r-2")
    unknown = await TaskRunner(service, handlers=(huge,)).run_once(context(), other.id)
    assert unknown.state is TaskState.NEEDS_RECONCILIATION
    assert "result" in str(unknown.error)


def test_result_and_error_are_masked_before_storage(tmp_path: Path) -> None:
    service = new_store(tmp_path / "tasks")
    done = claim_new(service, request_id="r-1")
    settled = service.complete(context(), done.id, result='{"token": "sk-abcdefghijklmnop"}')
    assert "sk-abcdefghijklmnop" not in str(settled.result)
    assert "REDACTED" in str(settled.result)
    broken = claim_new(service, request_id="r-2")
    failed = service.fail(
        context(),
        broken.id,
        error="password: hunter2 rejected",
        error_code=ToolErrorCode.INVALID_ARGUMENTS,
    )
    assert "hunter2" not in str(failed.error)
    assert new_store(tmp_path / "tasks").get(context(), done.id).result == settled.result


# -- schema and storage boundary (invariant 9) ----------------------------------


def test_unsupported_task_schema_is_refused(tmp_path: Path) -> None:
    directory = tmp_path / "future"
    directory.mkdir(mode=0o700)
    with sqlite3.connect(directory / "tasks.sqlite") as db:
        db.execute("CREATE TABLE task_schema (version INTEGER NOT NULL)")
        db.execute("INSERT INTO task_schema VALUES(2)")
    with pytest.raises(TaskConflict, match="unsupported task schema"):
        TaskStore(directory, authorize=Authorizer())


def test_task_directory_must_be_owner_private(tmp_path: Path) -> None:
    public = tmp_path / "public"
    public.mkdir(mode=0o755)
    with pytest.raises(TaskUnavailable, match="owner-private"):
        TaskStore(public, authorize=Authorizer())
    with pytest.raises(ScopeError, match="owner-private"):
        TaskStore(public, authorize=Authorizer())


def test_state_path_must_be_a_directory(tmp_path: Path) -> None:
    target = tmp_path / "tasks.sqlite"
    target.write_text("")
    with pytest.raises(ValueError, match="must be a directory"):
        TaskStore(target, authorize=Authorizer())
    assert target.is_file()


# -- runner wiring and listing --------------------------------------------------


def test_runner_rejects_unusable_handler_registrations(tmp_path: Path) -> None:
    service = new_store(tmp_path / "tasks")

    class Nameless:
        async def run(self, context: RunContext, task: TaskRecord) -> TaskOutcome:
            raise AssertionError

    class Sync:
        kind = "import"

        def run(self, context: RunContext, task: TaskRecord) -> TaskOutcome:
            raise AssertionError

    class Long:
        kind = "x" * (MAX_KIND_CHARS + 1)

        async def run(self, context: RunContext, task: TaskRecord) -> TaskOutcome:
            raise AssertionError

    class NoRun:
        kind = "import"

    with pytest.raises(ValueError, match="at least one task handler"):
        TaskRunner(service, handlers=())
    with pytest.raises(ValueError, match="duplicate"):
        TaskRunner(service, handlers=(Handler(), Handler()))
    with pytest.raises(ValueError, match="non-empty kind"):
        TaskRunner(service, handlers=(Nameless(),))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="non-empty kind"):
        TaskRunner(service, handlers=(Long(),))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="async run"):
        TaskRunner(service, handlers=(Sync(),))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="async run"):
        TaskRunner(service, handlers=(NoRun(),))  # type: ignore[arg-type]


async def test_drain_runs_queued_tasks_and_skips_unregistered_kinds(
    tmp_path: Path,
) -> None:
    service = new_store(tmp_path / "tasks")
    handler = Handler(kind="import", outcome=TaskOutcome(result="ok"))
    runner = TaskRunner(service, handlers=(handler,))
    first = create(service, request_id="r-1")
    unserved = create(service, request_id="r-2", kind="parse")
    third = create(service, request_id="r-3")
    settled = await runner.drain(context(), limit=10)
    assert [item.id for item in settled] == [first.id, third.id]
    assert [item.state for item in settled] == [TaskState.SUCCEEDED] * 2
    assert service.get(context(), unserved.id).state is TaskState.QUEUED
    assert handler.calls == 2
    with pytest.raises(ValueError, match="limit"):
        await runner.drain(context(), limit=0)
    with pytest.raises(TaskConflict, match="no handler registered"):
        await runner.run_once(context(), unserved.id)
    assert service.get(context(), unserved.id).state is TaskState.QUEUED


def test_list_filters_by_state_and_kind_with_a_bounded_limit(tmp_path: Path) -> None:
    service = new_store(tmp_path / "tasks")
    imported = create(service, request_id="r-1")
    parsed = create(service, request_id="r-2", kind="parse")
    second = create(service, request_id="r-3")
    service.claim(context(), second.id)
    assert [item.id for item in service.list(context(), kind="import")] == [
        imported.id,
        second.id,
    ]
    assert [item.id for item in service.list(context(), state=TaskState.QUEUED)] == [
        imported.id,
        parsed.id,
    ]
    assert [item.id for item in service.list(context(), limit=1)] == [imported.id]
    assert service.list(context(actor=Actor(id="other", roles=()))) == ()
    assert service.list(context(scope=ResourceScope(tenant_id="shop-b"))) == ()


def test_delete_removes_a_queued_task_and_refuses_open_work(tmp_path: Path) -> None:
    service = new_store(tmp_path / "tasks")
    queued = create(service, request_id="r-1")
    service.delete(context(), queued.id)
    with pytest.raises(TaskUnavailable, match="task unavailable"):
        service.get(context(), queued.id)
    with pytest.raises(TaskUnavailable, match="cannot be reused"):
        create(service, request_id="r-1")
    running = claim_new(service, request_id="r-2")
    with pytest.raises(TaskConflict, match="reconcile first"):
        service.delete(context(), running.id)
    unknown = claim_new(service, request_id="r-3")
    service.fail(context(), unknown.id, error="timed out", error_code=ToolErrorCode.TIMEOUT)
    with pytest.raises(TaskConflict, match="reconcile first"):
        service.delete(context(), unknown.id)
    settled = claim_new(service, request_id="r-4")
    service.complete(context(), settled.id)
    service.delete(context(), settled.id)
    assert [item.id for item in service.list(context())] == [
        running.id,
        unknown.id,
    ]


def test_tasks_recheck_resource_scope_and_per_record_authorization(tmp_path):
    service = new_store(tmp_path / "tasks")
    task = create(service, request_id="one")
    narrowed = context(scope=ResourceScope(tenant_id="shop-a", resources=()))
    assert service.list(narrowed) == ()
    with pytest.raises(TaskUnavailable):
        service.get(narrowed, task.id)
    service.authorize = lambda caller, op, item: item is None
    assert service.list(context()) == ()
    with pytest.raises(TaskUnavailable):
        service.get(context(), task.id)


def test_deleted_task_business_key_is_retired_across_request_ids(tmp_path):
    service = new_store(tmp_path / "tasks")
    task = create(service, request_id="one")
    service.delete(context(), task.id)
    rebuilt = new_store(tmp_path / "tasks")
    with pytest.raises(TaskUnavailable, match="cannot be reused"):
        rebuilt.create(
            context(), kind=task.kind, request_id="another", business_key=task.business_key
        )


def test_one_attempt_budget_cannot_requeue_after_crash(tmp_path):
    service = new_store(tmp_path / "tasks", max_attempts=1)
    task = claim_new(service, request_id="one")
    with pytest.raises(TaskConflict, match="budget is exhausted"):
        service.reconcile(context(), task.id, state=TaskState.QUEUED)
    assert service.get(context(), task.id).state is TaskState.RUNNING
    assert service.reconcile(context(), task.id, state=TaskState.FAILED).attempts == 1
