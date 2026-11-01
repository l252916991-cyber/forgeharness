"""Service-level durable session contracts for the S1 phase (ADR 014).

The HTTP contracts live in ``test_session_control.py``. These tests drive the
embedded :class:`SessionService` directly: host bindings, provider recording,
bounded history, single-writer behaviour, workflow sessions, tombstones, close
semantics and the crash windows that reconciliation exists for. Every fixture is
synthetic and deterministic; no real model, tenant or business data is involved.
"""

import asyncio
import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from test_sdk import Echo, Policy, app, context

from forgeharness.context.provider import ContextMaterial
from forgeharness.sdk import (
    Actor,
    FinalAction,
    ModelRequest,
    ModelResult,
    ResourceScope,
    RunContext,
    RunStatus,
    ToolAction,
    ToolCall,
    WorkflowResult,
    WorkflowSpec,
    WorkflowStep,
)
from forgeharness.sdk.application import Harness, ScopeError
from forgeharness.sdk.sessions import (
    SessionConflict,
    SessionService,
    SessionTurn,
    SessionUnavailable,
    _digest,
)


def always(actor: RunContext, operation: object, session: object) -> bool:
    del actor, operation, session
    return True


def answering_service(tmp_path: Path, **options: object) -> SessionService:
    return SessionService(
        app(Echo(), [ModelResult(action=FinalAction(content="answer"))]),
        state_dir=tmp_path,
        authorize=always,
        **options,
    )


def material(**changes: object) -> ContextMaterial:
    values: dict[str, object] = {
        "id": "source",
        "source": "synthetic-fixture",
        "version": "1",
        "tenant_id": "shop-a",
        "application_id": "echo",
        "resource_id": "product:a",
        "content": "Synthetic reference value 100.",
    }
    values.update(changes)
    return ContextMaterial.model_validate(values)


class StaticProvider:
    """Deterministic stand-in for a host retrieval adapter."""

    def __init__(self, *materials: ContextMaterial) -> None:
        self.materials = materials

    async def provide(self, *, task: str, context: RunContext) -> tuple[ContextMaterial, ...]:
        del task, context
        return self.materials


def sequenced(*results: ModelResult) -> type:
    """Model factory whose decisions are shared across run and resume instances.

    ``Harness`` builds a fresh model from the factory for every invocation, so a
    per-instance deque would replay the first decision after an approval resume.
    """
    pending = list(results)

    class Queue:
        async def decide(self, request: ModelRequest) -> ModelResult:
            del request
            assert pending, "the synthetic queue was over-consumed"
            return pending.pop(0)

    return Queue


class RecordingModel:
    def __init__(self, requests: list[ModelRequest]) -> None:
        self.requests = requests

    async def decide(self, request: ModelRequest) -> ModelResult:
        self.requests.append(request)
        return ModelResult(action=FinalAction(content="answer"))


def with_provider(specification: object, *materials: ContextMaterial) -> object:
    return replace(specification, context_providers=(StaticProvider(*materials),))  # type: ignore[arg-type]


def test_constructor_rejects_unusable_host_contracts(tmp_path: Path) -> None:
    specification = app(Echo(), [ModelResult(action=FinalAction(content="ok"))])
    with pytest.raises(ValueError, match="live host authorizer"):
        SessionService(
            specification,
            state_dir=tmp_path / "unauthorized",
            authorize=None,  # type: ignore[arg-type]
        )
    for options in (
        {"max_message_chars": 0},
        {"max_message_chars": 1_000_001},
        {"history_limit": 0},
        {"history_limit": 101},
    ):
        with pytest.raises(ValueError, match="invalid message or history limit"):
            SessionService(
                specification, state_dir=tmp_path / "limits", authorize=always, **options
            )
    with pytest.raises(ValueError, match="current material validation"):
        SessionService(
            with_provider(specification, material()),  # type: ignore[arg-type]
            state_dir=tmp_path / "unvalidated",
            authorize=always,
        )
    public = tmp_path / "public"
    public.mkdir(mode=0o755)
    with pytest.raises(ScopeError, match="owner-private"):
        SessionService(specification, state_dir=public, authorize=always)


def test_unsupported_session_schema_is_refused(tmp_path: Path) -> None:
    specification = app(Echo(), [ModelResult(action=FinalAction(content="ok"))])
    state_dir = tmp_path / "future"
    state_dir.mkdir(mode=0o700)
    database = state_dir / "sessions.sqlite"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE session_schema (version INTEGER NOT NULL)")
        db.execute("INSERT INTO session_schema VALUES(2)")

    with pytest.raises(SessionConflict, match="unsupported session schema"):
        SessionService(specification, state_dir=state_dir, authorize=always)


async def test_provider_materials_are_transformed_recorded_and_validated(
    tmp_path: Path,
) -> None:
    seen: list[tuple[RunContext, tuple[ContextMaterial, ...]]] = []
    requests: list[ModelRequest] = []

    def validate(identity: RunContext, materials: tuple[ContextMaterial, ...]) -> bool:
        seen.append((identity, materials))
        return True

    specification = replace(
        app(Echo(), [], model_factory=lambda: RecordingModel(requests)),
        context_providers=(
            StaticProvider(material(content="Synthetic value 100. password: hunter2")),
        ),
    )
    service = SessionService(
        specification, state_dir=tmp_path, authorize=always, validate_materials=validate
    )
    service.create(context())
    turn = await service.submit(context(), request_id="one", text="read the source")

    assert turn.status is RunStatus.SUCCEEDED
    # Resuming a finished turn without a grant is an idempotent read of the stored
    # result, never a second execution.
    assert (await service.resume(context(), turn.id)).status is RunStatus.SUCCEEDED
    assert len(requests) == 1
    # The host validator sees the authenticated binding that will execute, not a
    # caller-supplied body.
    assert seen and seen[0][0].session_id == "s1"
    assert seen[0][0].actor == context().actor
    assert seen[0][0].scope == context().scope
    recorded = service.get_turn(context(), turn.id).materials
    assert [item.id for item in recorded] == ["source"]
    assert recorded[0].version == "1"
    # Same transformation as model input and stored evidence: no raw credential.
    assert "hunter2" not in recorded[0].content
    assert "hunter2" not in json.dumps(
        [item.model_dump(mode="json") for item in requests[0].messages]
    )


@pytest.mark.parametrize(
    ("invalid", "message"),
    [
        ({"tenant_id": "shop-b"}, "outside current scope"),
        ({"resource_id": "product:b"}, "outside current scope"),
        ({"expires_at": datetime.now(UTC) - timedelta(minutes=1)}, "outside current scope"),
    ],
)
async def test_materials_outside_current_scope_are_refused(
    tmp_path: Path, invalid: dict[str, object], message: str
) -> None:
    service = SessionService(
        with_provider(
            app(Echo(), [ModelResult(action=FinalAction(content="ok"))]), material(**invalid)
        ),  # type: ignore[arg-type]
        state_dir=tmp_path,
        authorize=always,
        validate_materials=lambda identity, materials: True,
    )
    service.create(context())

    with pytest.raises(SessionUnavailable, match=message):
        await service.submit(context(), request_id="one", text="read")

    # A preparation failure is recorded as failed, never as an unfinished success.
    assert [turn.status for turn in service.history(context())] == [RunStatus.FAILED]


async def test_host_material_validation_denial_is_recorded_as_failure(tmp_path: Path) -> None:
    service = SessionService(
        with_provider(app(Echo(), [ModelResult(action=FinalAction(content="ok"))]), material()),  # type: ignore[arg-type]
        state_dir=tmp_path,
        authorize=always,
        validate_materials=lambda identity, materials: False,
    )
    service.create(context())

    with pytest.raises(SessionUnavailable, match="source version unavailable"):
        await service.submit(context(), request_id="one", text="read")

    turn = service.history(context())[0]
    assert turn.status is RunStatus.FAILED
    assert turn.error == "execution_preparation_failed"


async def test_history_is_bounded_to_the_configured_window(tmp_path: Path) -> None:
    requests: list[ModelRequest] = []
    service = SessionService(
        app(Echo(), [], model_factory=lambda: RecordingModel(requests)),
        state_dir=tmp_path,
        authorize=always,
        history_limit=1,
    )
    service.create(context())
    first = await service.submit(context(), request_id="one", text="first-marker")
    second = await service.submit(context(), request_id="two", text="second-marker")

    assert first.status is second.status is RunStatus.SUCCEEDED
    assert service.get_turn(context(), second.id).history_turns == (first.id,)
    assembled = " ".join(message.content or "" for message in requests[-1].messages)
    assert "first-marker" in assembled
    assert "untrusted_conversation_history" in assembled
    # Only the most recent turn fits the window; older text is not smuggled in.
    assert assembled.count("first-marker") == 1

    with pytest.raises(ValueError, match="history limit"):
        service.history(context(), limit=0)
    with pytest.raises(ValueError, match="before_sequence"):
        service.history(context(), before_sequence=0)
    assert [turn.id for turn in service.history(context(), before_sequence=second.sequence)] == [
        first.id
    ]


async def test_failed_turn_is_reported_and_kept_out_of_later_context(tmp_path: Path) -> None:
    requests: list[ModelRequest] = []
    remaining = {"failures": 1}

    class FailingOnce:
        async def decide(self, request: ModelRequest) -> ModelResult:
            requests.append(request)
            if remaining["failures"]:
                remaining["failures"] -= 1
                raise RuntimeError("synthetic provider outage")
            return ModelResult(action=FinalAction(content="recovered"))

    specification = app(Echo(), [], model_factory=FailingOnce)
    service = SessionService(specification, state_dir=tmp_path, authorize=always)
    service.create(context())
    failed = await service.submit(context(), request_id="one", text="failing-marker")
    assert failed.status is RunStatus.FAILED

    succeeded = await service.submit(context(), request_id="two", text="recovered-marker")

    assert succeeded.status is RunStatus.SUCCEEDED
    assert service.get_turn(context(), succeeded.id).history_turns == ()
    assert "failing-marker" not in " ".join(
        message.content or "" for message in requests[-1].messages
    )


async def test_second_worker_is_refused_while_the_session_is_busy(tmp_path: Path) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    class SlowModel:
        async def decide(self, request: ModelRequest) -> ModelResult:
            del request
            started.set()
            await release.wait()
            return ModelResult(action=FinalAction(content="answer"))

    service = SessionService(
        app(Echo(), [], model_factory=SlowModel), state_dir=tmp_path, authorize=always
    )
    service.create(context())
    first = asyncio.create_task(service.submit(context(), request_id="one", text="first"))
    await started.wait()
    try:
        with pytest.raises(SessionConflict, match="active worker"):
            await service.submit(context(), request_id="two", text="second")
    finally:
        release.set()
    assert (await first).status is RunStatus.SUCCEEDED


async def test_suspended_turn_blocks_the_next_submission(tmp_path: Path) -> None:
    tool = Echo(write=True)
    service = SessionService(
        app(
            tool,
            [],
            policy=Policy(),
            model_factory=sequenced(
                ModelResult(
                    action=ToolAction(call=ToolCall(id="write", name="echo", arguments={}))
                ),
                ModelResult(action=FinalAction(content="saved")),
            ),
        ),
        state_dir=tmp_path,
        authorize=always,
    )
    service.create(context())
    waiting = await service.submit(context(), request_id="one", text="write")
    assert waiting.status is RunStatus.AWAITING_APPROVAL

    with pytest.raises(SessionConflict, match="active turn"):
        await service.submit(context(), request_id="two", text="another question")

    assert len(tool.calls) == 0


async def test_workflow_session_exposes_keyed_results_events_and_tombstones(
    tmp_path: Path,
) -> None:
    steps = (
        WorkflowStep(
            id="collect",
            application=app(Echo(), [ModelResult(action=FinalAction(content="first node"))]),
            task="collect",
        ),
        WorkflowStep(
            id="answer",
            application=app(Echo(), [ModelResult(action=FinalAction(content="second node"))]),
            task="answer",
            depends_on=("collect",),
        ),
    )
    service = SessionService(
        WorkflowSpec(id="echo", version="1", policy_version="1", steps=steps),
        state_dir=tmp_path,
        authorize=always,
    )
    service.create(context())
    turn = await service.submit(context(), request_id="one", text="run the synthetic flow")

    assert turn.status is RunStatus.SUCCEEDED
    assert turn.workflow_id == turn.run_id
    # Several sinks are reported by node name instead of pretending one is the answer.
    assert json.loads(turn.output) == {"answer": "second node", "collect": "first node"}
    result = service.inspect(context(), turn.id)
    assert isinstance(result, WorkflowResult)
    assert set(result.nodes) == {"collect", "answer"}

    events = service.events(context(), turn.id)
    assert events and [event["sequence"] for event in events] == list(range(1, len(events) + 1))
    window = service.events(context(), turn.id, after=1, limit=1)
    assert [event["sequence"] for event in window] == [2]

    service.delete_turn(context(), turn.id)

    with pytest.raises(SessionUnavailable, match="turn evidence deleted"):
        service.get_turn(context(), turn.id)
    with pytest.raises(SessionUnavailable, match="turn evidence deleted"):
        await service.submit(context(), request_id="one", text="run the synthetic flow")


async def test_deleted_session_retires_its_identity(tmp_path: Path) -> None:
    service = answering_service(tmp_path)
    identity = context()
    service.create(identity)
    turn = await service.submit(identity, request_id="one", text="hello")
    service.delete_turn(identity, turn.id)
    assert [item.status for item in service.history(identity)] == ["deleted"]

    service.delete(identity)
    service.delete(identity)  # Deleting again is idempotent, never a resurrection.

    with pytest.raises(SessionUnavailable, match="session deleted"):
        service.get(identity)
    with pytest.raises(SessionUnavailable, match="cannot be reused"):
        service.create(identity)


async def test_other_binding_and_missing_identifiers_stay_unavailable(tmp_path: Path) -> None:
    service = answering_service(tmp_path)
    service.create(context())
    await service.submit(context(), request_id="one", text="hello")

    for changed in (
        {"scope": ResourceScope(tenant_id="shop-b", resources=("product:a",))},
        {"session_id": "another-session"},
        {"application_id": "other"},
    ):
        with pytest.raises(SessionUnavailable):
            service.history(context(**changed))

    with pytest.raises(SessionUnavailable, match="turn unavailable"):
        service.get_turn(context(), "missing-turn")
    # A credential in the host binding is refused before any session is touched.
    with pytest.raises(SessionUnavailable, match="credentials in identity"):
        service.create(context(correlation_id="api_key: sk-abcdefghijkl"))


async def test_close_requires_a_settled_session_and_blocks_later_work(tmp_path: Path) -> None:
    tool = Echo(write=True)
    service = SessionService(
        app(
            tool,
            [],
            policy=Policy(),
            model_factory=sequenced(
                ModelResult(
                    action=ToolAction(call=ToolCall(id="write", name="echo", arguments={}))
                ),
                ModelResult(action=FinalAction(content="saved")),
            ),
        ),
        state_dir=tmp_path,
        authorize=always,
    )
    owner = context()
    approver = context(actor=Actor(id="owner", roles=("use", "approve")))
    service.create(owner)
    waiting = await service.submit(owner, request_id="one", text="write")

    with pytest.raises(SessionConflict, match="active turn prevents closing"):
        service.close(owner)

    grant = service.approve(owner, waiting.id, approver=approver)
    with pytest.raises(ValueError, match="approver requires an approval grant"):
        await service.resume(owner, waiting.id, approver=approver)
    with pytest.raises(SessionUnavailable, match="fresh approver identity"):
        await service.resume(owner, waiting.id, grant=grant)
    assert (await service.resume(owner, waiting.id, grant=grant, approver=approver)).status is (
        RunStatus.SUCCEEDED
    )
    assert len(tool.calls) == 1
    service.close(owner)

    with pytest.raises(SessionConflict, match="session is closed"):
        await service.submit(owner, request_id="two", text="more")
    with pytest.raises(SessionConflict, match="closed session cannot approve"):
        service.approve(owner, waiting.id, approver=approver)
    with pytest.raises(SessionConflict, match="closed session cannot resume"):
        await service.resume(owner, waiting.id)


async def test_changed_owner_roles_cannot_resume_a_suspended_binding(tmp_path: Path) -> None:
    service = SessionService(
        app(
            Echo(write=True),
            [ModelResult(action=ToolAction(call=ToolCall(id="write", name="echo", arguments={})))],
            policy=Policy(),
        ),
        state_dir=tmp_path,
        authorize=always,
    )
    owner = context(actor=Actor(id="owner", roles=("use", "approve")))
    service.create(owner)
    waiting = await service.submit(owner, request_id="one", text="write")

    # Same actor and scope, but the current host roles are no longer the ones that
    # produced the suspended binding.
    changed = context(actor=Actor(id="owner", roles=("use",)))
    with pytest.raises(SessionUnavailable, match="owner roles changed"):
        await service.resume(changed, waiting.id)


async def test_bounded_inputs_are_validation_errors(tmp_path: Path) -> None:
    service = answering_service(tmp_path, max_message_chars=10)
    identity = context()
    service.create(identity)
    for request_id in ("", "   ", "x" * 201, "api_key: sk-abcdefghijkl"):
        with pytest.raises(ValueError, match="request_id"):
            await service.submit(identity, request_id=request_id, text="ok")
    for text in ("", "   ", "x" * 11):
        with pytest.raises(ValueError, match="message is empty or exceeds limit"):
            await service.submit(identity, request_id="one", text=text)

    turn = await service.submit(identity, request_id="one", text="ok")
    for options in ({"after": -1}, {"limit": 0}, {"limit": 1001}):
        with pytest.raises(ValueError, match="invalid event pagination"):
            service.events(identity, turn.id, **options)


async def test_replay_of_a_stored_request_is_reconciled_without_rerunning(
    tmp_path: Path,
) -> None:
    requests: list[ModelRequest] = []
    service = SessionService(
        app(Echo(), [], model_factory=lambda: RecordingModel(requests)),
        state_dir=tmp_path,
        authorize=always,
    )
    identity = context()
    service.create(identity)
    turn = await service.submit(identity, request_id="one", text="hello")

    # Manufacture the ADR 014 crash window: the durable run finished, but the turn
    # row still looks active because the process died before the last write.
    service._update_turn(turn.model_copy(update={"status": "starting", "output": None}))
    reconciled = await service.submit(identity, request_id="one", text="hello")

    assert reconciled.status is RunStatus.SUCCEEDED
    assert reconciled.id == turn.id
    assert len(requests) == 1
    with pytest.raises(SessionConflict, match="different text"):
        await service.submit(identity, request_id="one", text="changed")


def manufactured_turn(service: SessionService, identity: RunContext) -> SessionTurn:
    """Insert the durable row without executing, reproducing the pre-run crash window."""
    binding = identity.model_copy(update={"run_id": "turn-never-started"})
    turn = SessionTurn(
        id="turn-never-started",
        session_id=identity.session_id,
        sequence=1,
        request_id="never-started",
        run_id=binding.run_id,
        context=binding,
        text="never started",
    )
    with sqlite3.connect(service.database) as db:
        db.execute(
            "INSERT INTO sdk_turns VALUES(?,?,?,?,?,?)",
            (
                service._key(identity),
                turn.id,
                turn.sequence,
                turn.request_id,
                # The same request digest `submit` stores for this exact text.
                _digest([turn.text, identity.actor.id]),
                turn.model_dump_json(),
            ),
        )
    return turn


async def test_never_started_turn_cannot_report_success_or_hide_a_deletion(
    tmp_path: Path,
) -> None:
    service = answering_service(tmp_path)
    identity = context()
    service.create(identity)
    turn = manufactured_turn(service, identity)

    with pytest.raises(SessionUnavailable, match="has not started"):
        service.inspect(identity, turn.id)

    # A turn that was registered but never executed may still be started, once,
    # under its original binding instead of being recreated.
    started = await service.resume(identity, turn.id)
    assert started.status is RunStatus.SUCCEEDED
    assert started.id == turn.id

    # Deleting the turn must still tombstone the identity even though no run and no
    # checkpoint exist, otherwise the request key could be replayed afterwards.
    service.delete_turn(identity, turn.id)

    assert [item.status for item in service.history(identity)] == ["deleted"]
    with pytest.raises(SessionUnavailable, match="turn evidence deleted"):
        await service.submit(identity, request_id="never-started", text="never started")


async def test_workflow_adapter_failure_does_not_lose_durable_state(tmp_path: Path) -> None:
    failing = {"remaining": 1}

    class FlakyProvider:
        async def provide(self, *, task: str, context: RunContext) -> tuple[ContextMaterial, ...]:
            del task, context
            if failing["remaining"]:
                failing["remaining"] -= 1
                raise RuntimeError("synthetic retrieval adapter outage")
            return ()

    step = replace(
        app(Echo(), [ModelResult(action=FinalAction(content="node answer"))]),
        context_providers=(FlakyProvider(),),
    )
    service = SessionService(
        WorkflowSpec(
            id="echo",
            version="1",
            policy_version="1",
            steps=(WorkflowStep(id="only", application=step, task="go"),),
        ),
        state_dir=tmp_path,
        authorize=always,
        validate_materials=lambda identity, materials: True,
    )
    identity = context()
    service.create(identity)

    with pytest.raises(RuntimeError, match="synthetic retrieval adapter outage"):
        await service.submit(identity, request_id="one", text="go")

    # A node-adapter failure must surface the durable workflow state, including its
    # error, instead of leaving the turn looking like it never started.
    turn = service.history(identity)[0]
    assert turn.status is RunStatus.RUNNING
    assert turn.error is not None and "RuntimeError" in turn.error

    # The suspended turn is resumed once the host adapter recovers; it is never
    # silently recreated under a new identity.
    recovered = await service.resume(identity, turn.id)
    assert recovered.status is RunStatus.SUCCEEDED
    assert recovered.id == turn.id
    assert service.close(identity).status == "closed"


async def test_deleted_execution_evidence_is_not_reusable(tmp_path: Path) -> None:
    service = answering_service(tmp_path)
    identity = context()
    service.create(identity)
    turn = await service.submit(identity, request_id="one", text="hello")

    Harness(service.application, state_dir=service._controller(turn).state_dir).delete(turn.context)  # type: ignore[arg-type]

    with pytest.raises(ScopeError, match="deleted"):
        service.history(identity)
    with pytest.raises(ScopeError, match="deleted"):
        await service.resume(identity, turn.id)


async def test_failed_preparation_can_be_deleted_repeatedly(tmp_path):
    service = SessionService(
        with_provider(app(Echo(), [ModelResult(action=FinalAction(content="ok"))]), material()),
        state_dir=tmp_path,
        authorize=always,
        validate_materials=lambda *_: False,
    )
    service.create(context())
    with pytest.raises(SessionUnavailable):
        await service.submit(context(), request_id="one", text="read")
    turn = service.history(context())[0]
    service.delete_turn(context(), turn.id)
    service.delete_turn(context(), turn.id)
    service.delete(context())
    service.delete(context())


async def test_source_permission_changed_during_execution_is_rechecked(tmp_path):
    allowed = True

    class RevokingModel:
        async def decide(self, request):
            nonlocal allowed
            allowed = False
            return ModelResult(action=FinalAction(content="ok"))

    service = SessionService(
        with_provider(app(Echo(), [], model_factory=RevokingModel), material()),
        state_dir=tmp_path,
        authorize=always,
        validate_materials=lambda *_: allowed,
    )
    service.create(context())
    with pytest.raises(SessionUnavailable, match="source version"):
        await service.submit(context(), request_id="one", text="read")


async def test_history_revoked_during_provider_await_never_reaches_model(tmp_path: Path) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    allowed = True
    requests: list[ModelRequest] = []

    class PausingProvider:
        calls = 0

        async def provide(self, *, task: str, context: RunContext) -> tuple[ContextMaterial, ...]:
            del task, context
            self.calls += 1
            if self.calls == 1:
                return (material(),)
            entered.set()
            await release.wait()
            return ()

    service = SessionService(
        replace(
            app(Echo(), [], model_factory=lambda: RecordingModel(requests)),
            context_providers=(PausingProvider(),),
        ),
        state_dir=tmp_path,
        authorize=always,
        validate_materials=lambda *_: allowed,
    )
    identity = context()
    service.create(identity)
    await service.submit(identity, request_id="first", text="private-history-marker")
    pending = asyncio.create_task(service.submit(identity, request_id="second", text="continue"))
    await asyncio.wait_for(entered.wait(), timeout=5)
    allowed = False
    release.set()

    with pytest.raises(SessionUnavailable, match="source version"):
        await pending
    assert len(requests) == 1  # No second model request, even one that later fails.


@pytest.mark.parametrize("revoked", ["source", "session"])
async def test_earlier_provider_revoked_before_first_model_is_refused(
    tmp_path: Path, revoked: str
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    permissions = {"source": True, "session": True}
    requests: list[ModelRequest] = []

    class PausingProvider:
        async def provide(self, *, task: str, context: RunContext) -> tuple[ContextMaterial, ...]:
            del task, context
            entered.set()
            await release.wait()
            return ()

    service = SessionService(
        replace(
            app(Echo(), [], model_factory=lambda: RecordingModel(requests)),
            context_providers=(StaticProvider(material()), PausingProvider()),
        ),
        state_dir=tmp_path,
        authorize=lambda *_: permissions["session"],
        validate_materials=lambda *_: permissions["source"],
    )
    identity = context()
    service.create(identity)
    pending = asyncio.create_task(service.submit(identity, request_id="first", text="read"))
    await asyncio.wait_for(entered.wait(), timeout=5)
    permissions[revoked] = False
    release.set()

    with pytest.raises(SessionUnavailable):
        await pending
    assert requests == []


def test_history_validation_batches_long_chains_and_refuses_over_bound(tmp_path, monkeypatch):
    from forgeharness.sdk.sessions import SessionTurn, ValidationCounters
    from forgeharness.sdk.sources import SourceRef

    service = answering_service(tmp_path)
    service.source_validator = lambda *_: True
    service.create(context())
    monkeypatch.setattr(service, "_find", lambda turn: None)
    previous = ()
    with service._connect() as db:
        for sequence in range(1, 514):
            turn = SessionTurn(
                id=f"t{sequence}",
                session_id=context().session_id,
                sequence=sequence,
                request_id=f"q{sequence}",
                run_id=f"r{sequence}",
                context=context(),
                status=RunStatus.FAILED,
                text="synthetic",
                history_turns=previous,
                sources=(SourceRef(resource_id="product:a", version="1"),) if sequence == 1 else (),
            )
            db.execute(
                "INSERT INTO sdk_turns VALUES(?,?,?,?,?,?)",
                (
                    service._key(context()),
                    turn.id,
                    sequence,
                    turn.request_id,
                    "fixture",
                    turn.model_dump_json(),
                ),
            )
            previous = (turn.id,)
    service.counters = ValidationCounters()
    assert len(service.history(context(), limit=100, before_sequence=513)) == 100
    assert service.counters.turns_validated == 512
    assert service.counters.turn_queries <= 1
    assert service.counters.run_lookups == 512
    service.source_validator = lambda *_: False
    with pytest.raises(SessionUnavailable, match="source version"):
        service.history(context(), limit=1, before_sequence=513)
    service.source_validator = lambda *_: True
    with pytest.raises(SessionUnavailable, match="validation bound"):
        service.history(context(), limit=1)
