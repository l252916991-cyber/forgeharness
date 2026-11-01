"""The async backend is the sole hosted execution authority, including recovery."""

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from forgeharness.domain.models import (
    CURRENT_RECOVERY_SEMANTICS_VERSION,
    FinalAction,
    Message,
    MessageRole,
    ModelResult,
    ModelUsage,
    RunResult,
    RunStatus,
    ToolAction,
    ToolCall,
    Usage,
)
from forgeharness.models.prepared import PreparedModelCall
from forgeharness.models.scripted import ScriptedModel
from forgeharness.observability.trace import InMemoryTrace
from forgeharness.runtime.budget import RunBudget
from forgeharness.runtime.execution import ExecutionSuspended
from forgeharness.runtime.loop import AgentRuntime
from forgeharness.runtime.model_call import ModelCallKind, ModelCallOutcome
from forgeharness.runtime.tool_executor import ToolExecutor
from forgeharness.state.approval import ApprovalGrant, InMemoryApprovalLedger
from forgeharness.state.checkpoint import CheckpointConflict, SQLiteCheckpointStore
from forgeharness.state.invocation_journal import SQLiteInvocationJournal
from forgeharness.tools.base import RiskLevel, SchemaSource, ToolContext, ToolOutput, ToolSpec
from forgeharness.tools.builtin import EchoInput, EchoTool
from forgeharness.tools.dispatcher import DispatchResult, ToolDispatcher
from forgeharness.tools.policy import RiskBasedPolicy
from forgeharness.tools.registry import ToolRegistry

CANONICAL_KEY = "pg-admitted:org-a:run-1:invocation.saved"


class Backend:
    """An awaitable port double; durable database semantics are tested separately."""

    def __init__(self) -> None:
        self.latest: RunResult | None = None
        self.saved: list[RunResult] = []
        self.approvals: list[tuple[RunResult, RunResult, ToolCall]] = []
        self.models: list[str] = []
        self.tools: list[ToolCall] = []
        self.recoveries: list[ToolCall] = []
        self.recovered: DispatchResult | None = None
        self.recovered_model: ModelCallOutcome | None = None
        self.suspend: str | None = None
        self.before_tool: Callable[[], None] | None = None
        self.before_save: Callable[[RunResult], None] | None = None
        self.dispatch: Callable[[str], Awaitable[DispatchResult]] | None = None
        self.idempotency_key = CANONICAL_KEY
        self.cached_tool = False
        self.repeat_dispatch = False

    def guard(self, operation: str) -> None:
        if self.suspend == operation:
            raise ExecutionSuspended(f"{operation} needs reconciliation")

    async def save(self, result: RunResult) -> RunResult:
        self.guard("save")
        if self.before_save is not None:
            self.before_save(result)
        current = self.latest.checkpoint_revision if self.latest else 0
        if result.checkpoint_revision != current:
            raise CheckpointConflict("stale backend checkpoint")
        saved = result.model_copy(update={"checkpoint_revision": current + 1}, deep=True)
        self.latest = saved
        self.saved.append(saved)
        return saved

    async def assert_current(self, previous: RunResult) -> None:
        self.guard("current")
        if self.latest != previous:
            raise CheckpointConflict("stale backend checkpoint")

    async def consume_approval(
        self,
        grant: ApprovalGrant,
        *,
        previous: RunResult,
        candidate: RunResult,
        call: ToolCall,
    ) -> RunResult:
        del grant
        self.guard("approval")
        saved = await self.save(candidate)
        self.approvals.append((previous, candidate, call.model_copy(deep=True)))
        return saved

    async def invoke_model(
        self,
        *,
        prepared: PreparedModelCall,
        usage: Usage,
        phase: str,
        dispatch: Callable[[], Awaitable[ModelCallOutcome]],
    ) -> ModelCallOutcome:
        del usage, phase
        self.guard("model")
        self.models.append(prepared.payload_sha256)
        if self.recovered_model is not None:
            recovered = self.recovered_model
            self.recovered_model = None
            return recovered
        return await dispatch()

    async def invoke_tool(
        self,
        *,
        call: ToolCall,
        usage: Usage,
        task_id: str,
        checkpoint_revision: int,
        dispatch: Callable[[str], Awaitable[DispatchResult]],
    ) -> DispatchResult:
        del usage, task_id
        self.dispatch = dispatch
        self.guard("tool")
        assert self.latest is not None
        assert self.latest.checkpoint_revision == checkpoint_revision
        assert self.latest.authorized_call_id == call.id
        self.tools.append(call.model_copy(deep=True))
        if self.before_tool is not None:
            self.before_tool()
        if self.cached_tool:
            return DispatchResult(output=ToolOutput(ok=True, content="cached"), elapsed_ms=0)
        result = await dispatch(self.idempotency_key)
        if self.repeat_dispatch:
            return await dispatch(self.idempotency_key)
        return result

    async def recover_tool(self, *, task_id: str, call: ToolCall) -> DispatchResult | None:
        del task_id
        self.guard("recover")
        self.recoveries.append(call.model_copy(deep=True))
        return self.recovered


class CountingEcho(EchoTool):
    def __init__(self, *, write: bool = False) -> None:
        self.inputs: list[str] = []
        self.idempotency_keys: list[str | None] = []
        self.write = write

    @property
    def spec(self) -> ToolSpec:
        return super().spec.model_copy(
            update={"risk": RiskLevel.WRITE if self.write else RiskLevel.READ}
        )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolOutput:
        self.inputs.append(EchoInput.model_validate(arguments).text)
        self.idempotency_keys.append(context.idempotency_key)
        return await super().execute(arguments, context)


class DefaultEchoInput(EchoInput):
    repetitions: int = 1
    suffix: str = "!"


class DefaultingEcho(CountingEcho):
    def __init__(self, *, write: bool = False, denied: bool = False) -> None:
        super().__init__(write=write)
        self.denied = denied
        self.received: list[dict[str, Any]] = []

    @property
    def input_model(self) -> type[BaseModel]:
        return DefaultEchoInput

    @property
    def spec(self) -> ToolSpec:
        return super().spec.model_copy(
            update={
                "input_schema": DefaultEchoInput.model_json_schema(),
                "risk": RiskLevel.PROCESS if self.denied else super().spec.risk,
            }
        )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolOutput:
        validated = DefaultEchoInput.model_validate(arguments)
        self.received.append(validated.model_dump(mode="json"))
        self.inputs.append(validated.text)
        self.idempotency_keys.append(context.idempotency_key)
        return ToolOutput(
            ok=True, content=(validated.text + validated.suffix) * validated.repetitions
        )


class AuditedBackend(Backend):
    """Mark a committed trace prefix, including backend-owned audit events."""

    def __init__(self) -> None:
        super().__init__()
        self.trace: InMemoryTrace | None = None
        self.committed_events = 0

    async def save(self, result: RunResult) -> RunResult:
        assert self.trace is not None
        self.trace.append(
            "checkpoint.saved",
            {"revision": result.checkpoint_revision + 1, "status": result.status.value},
        )
        saved = await super().save(result)
        self.committed_events = len(self.trace.events)
        return saved

    async def consume_approval(
        self,
        grant: ApprovalGrant,
        *,
        previous: RunResult,
        candidate: RunResult,
        call: ToolCall,
    ) -> RunResult:
        assert self.trace is not None
        self.trace.append("approval.consumed", {"tool": call.name, "granted_by": grant.granted_by})
        return await super().consume_approval(
            grant, previous=previous, candidate=candidate, call=call
        )


def runtime(
    backend: Backend, tool: CountingEcho, actions: list[ModelResult]
) -> tuple[AgentRuntime, InMemoryTrace, ScriptedModel]:
    registry = ToolRegistry()
    registry.register(tool)
    trace = InMemoryTrace("task")
    model = ScriptedModel(actions)
    return (
        AgentRuntime(
            model=model,
            registry=registry,
            dispatcher=ToolDispatcher(registry),
            policy=RiskBasedPolicy(),
            trace=trace,
            execution_backend=backend,
        ),
        trace,
        model,
    )


def call() -> ToolCall:
    return ToolCall(id="call", name="echo", arguments={"text": "original"})


def final() -> ModelResult:
    return ModelResult(action=FinalAction(content="done"))


def previous(backend: Backend) -> RunResult:
    pending = call()
    state = RunResult(
        task_id="task",
        status=RunStatus.RUNNING,
        messages=(
            Message(role=MessageRole.USER, content="task"),
            Message(role=MessageRole.ASSISTANT, tool_calls=(pending,)),
        ),
        usage=Usage(steps=1),
        authorized_call_id=pending.id,
        checkpoint_revision=1,
        recovery_semantics_version=CURRENT_RECOVERY_SEMANTICS_VERSION,
    )
    backend.latest = state
    return state


@pytest.mark.parametrize("store", ["checkpoint_store", "approval_ledger", "invocation_journal"])
def test_backend_cannot_share_authority_with_a_local_store(tmp_path: Path, store: str) -> None:
    database = tmp_path / "local.sqlite"
    stores: dict[str, Any] = {
        "checkpoint_store": SQLiteCheckpointStore(database),
        "approval_ledger": InMemoryApprovalLedger(),
        "invocation_journal": SQLiteInvocationJournal(database),
    }
    registry = ToolRegistry()
    with pytest.raises(ValueError, match="cannot share authority"):
        AgentRuntime(
            model=ScriptedModel([]),
            registry=registry,
            dispatcher=ToolDispatcher(registry),
            policy=RiskBasedPolicy(),
            trace=InMemoryTrace("task"),
            execution_backend=Backend(),
            **{store: stores[store]},
        )


async def test_backend_owns_checkpoints_models_and_tool_dispatch() -> None:
    backend, tool = Backend(), CountingEcho()
    runner, _, model = runtime(
        backend, tool, [ModelResult(action=ToolAction(call=call())), final()]
    )
    result = await runner.run(task_id="task", task="task", workspace=None)
    assert result == backend.latest
    assert result.status is RunStatus.SUCCEEDED
    assert result.recovery_semantics_version == CURRENT_RECOVERY_SEMANTICS_VERSION
    assert len(backend.models) == 2 and len(backend.tools) == 1
    assert tool.inputs == ["original"]
    assert tool.idempotency_keys == [CANONICAL_KEY]
    assert model.requests[1].messages[-1].content == "original"


@pytest.mark.parametrize("boundary", ["save", "tool"])
async def test_dispatch_arguments_are_detached_before_backend_await(boundary: str) -> None:
    backend, tool, requested = Backend(), CountingEcho(), call()

    def mutate() -> None:
        requested.arguments.update(text="changed during admission")

    if boundary == "tool":
        backend.before_tool = mutate
    else:
        backend.before_save = lambda saved: mutate() if saved.authorized_call_id else None
    runner, _, _ = runtime(backend, tool, [ModelResult(action=ToolAction(call=requested)), final()])
    await runner.run(task_id="task", task="task", workspace=None)
    assert backend.tools[0].arguments == {"text": "original"}
    assert tool.inputs == ["original"]


async def test_backend_consumes_exact_approval_and_checkpoint_in_one_operation() -> None:
    backend, tool = Backend(), CountingEcho(write=True)
    runner, _, _ = runtime(backend, tool, [ModelResult(action=ToolAction(call=call())), final()])
    suspended = await runner.run(task_id="task", task="task", workspace=None)
    assert suspended.status is RunStatus.AWAITING_APPROVAL
    assert not tool.inputs
    grant = InMemoryApprovalLedger().issue(task_id="task", call=call(), granted_by="approver")
    result = await runner.resume_approved(previous=suspended, grant=grant, workspace=None)
    assert result.status is RunStatus.SUCCEEDED
    old, candidate, approved = backend.approvals[0]
    assert old == suspended and approved == call()
    assert candidate.pending_approval is None and candidate.authorized_call_id == approved.id
    assert candidate.status is RunStatus.RUNNING
    assert tool.inputs == ["original"]


@pytest.mark.parametrize("interrupted", [False, True])
async def test_hosted_defaults_and_types_are_approved_checkpointed_and_dispatched_exactly(
    interrupted: bool,
) -> None:
    backend, tool = Backend(), DefaultingEcho(write=True)
    requested = ToolCall(id="call", name="echo", arguments={"text": "original", "repetitions": "2"})
    expected = {"text": "original", "repetitions": 2, "suffix": "!"}
    runner, trace, _ = runtime(
        backend, tool, [ModelResult(action=ToolAction(call=requested)), final()]
    )

    pending = await runner.run(task_id="task", task="task", workspace=None)

    assert pending.status is RunStatus.AWAITING_APPROVAL
    assert pending.pending_approval is not None
    approved_call = pending.pending_approval.call
    assert approved_call.arguments == expected
    assert pending.messages[-1].tool_calls[0] == approved_call
    assert requested.arguments == {"text": "original", "repetitions": "2"}
    original_action = next(event for event in trace.events if event.type == "model.action")
    assert original_action.payload["action"]["call"]["arguments"] == requested.arguments
    normalized = [event for event in trace.events if event.type == "tool.arguments.normalized"]
    assert len(normalized) == 1 and normalized[0].payload["arguments"] == expected
    grant = InMemoryApprovalLedger().issue(
        task_id="task", call=approved_call, granted_by="approver"
    )
    if interrupted:
        backend.suspend = "tool"
        with pytest.raises(ExecutionSuspended):
            await runner.resume_approved(previous=pending, grant=grant, workspace=None)
        assert backend.latest is not None
        assert backend.latest.messages[-1].tool_calls[0] == approved_call
        backend.suspend = None
        result = await runner.resume_running(previous=backend.latest, workspace=None)
        assert backend.recoveries == [approved_call]
    else:
        result = await runner.resume_approved(previous=pending, grant=grant, workspace=None)

    assert result.status is RunStatus.SUCCEEDED
    assert len(backend.approvals) == 1
    assert backend.approvals[0][2] == approved_call
    assert backend.tools == [approved_call]
    assert tool.received == [expected]
    assert tool.idempotency_keys == [CANONICAL_KEY]


async def test_invalid_hosted_arguments_still_reach_policy_denial_without_execution() -> None:
    backend, tool = Backend(), DefaultingEcho(denied=True)
    invalid = ToolCall(id="call", name="echo", arguments={"repetitions": "invalid"})
    runner, trace, _ = runtime(
        backend, tool, [ModelResult(action=ToolAction(call=invalid)), final()]
    )

    result = await runner.run(task_id="task", task="task", workspace=None)

    assert result.status is RunStatus.SUCCEEDED
    decision = next(event for event in trace.events if event.type == "policy.decided")
    assert decision.payload["decision"] == "deny"
    assert not any(event.type == "tool.arguments.normalized" for event in trace.events)
    assert result.messages[1].tool_calls == (invalid,)
    assert "policy denied" in (result.messages[2].content or "")
    assert not backend.tools and not tool.received


@pytest.mark.parametrize("write", [False, True])
async def test_invalid_hosted_arguments_are_logical_refusals_without_physical_admission(
    write: bool,
) -> None:
    backend, tool = Backend(), DefaultingEcho(write=write)
    invalid = ToolCall(id="call", name="echo", arguments={"repetitions": "invalid"})
    runner, trace, _ = runtime(
        backend, tool, [ModelResult(action=ToolAction(call=invalid)), final()]
    )

    result = await runner.run(task_id="task", task="task", workspace=None)

    assert result.status is RunStatus.SUCCEEDED
    assert result.usage.tool_calls == 1
    assert result.pending_approval is None and result.authorized_call_id is None
    assert not backend.tools and not backend.approvals and not tool.received
    refused = [event for event in trace.events if event.type == "tool.invalid_arguments"]
    assert len(refused) == 1
    assert refused[0].payload == {
        "call_id": "call",
        "tool": "echo",
        "error_code": "invalid_arguments",
        "recoverable": True,
    }
    assert "required schema" in (result.messages[2].content or "")


class StrictExternalEcho(DefaultingEcho):
    @property
    def spec(self) -> ToolSpec:
        return super().spec.model_copy(
            update={
                "schema_source": SchemaSource.EXTERNAL_JSON_SCHEMA,
                "input_schema": {
                    "type": "object",
                    "properties": {"text": {"type": "string", "pattern": "^allowed$"}},
                    "required": ["text"],
                },
            }
        )


@pytest.mark.parametrize("write", [False, True])
async def test_hosted_external_schema_is_checked_before_normalization_or_admission(
    write: bool,
) -> None:
    backend, tool = Backend(), StrictExternalEcho(write=write)
    invalid = ToolCall(id="call", name="echo", arguments={"text": "rejected"})
    runner, trace, _ = runtime(
        backend, tool, [ModelResult(action=ToolAction(call=invalid)), final()]
    )

    result = await runner.run(task_id="task", task="task", workspace=None)

    assert result.status is RunStatus.SUCCEEDED and result.usage.tool_calls == 1
    assert result.messages[1].tool_calls == (invalid,)
    assert result.pending_approval is None
    assert not backend.tools and not tool.received
    assert any(event.type == "tool.invalid_arguments" for event in trace.events)
    assert not any(event.type == "tool.arguments.normalized" for event in trace.events)


async def test_terminal_checkpoint_has_no_uncommitted_core_audit_tail() -> None:
    backend = AuditedBackend()
    runner, trace, _ = runtime(backend, CountingEcho(), [final()])
    backend.trace = trace
    result = await runner.run(task_id="task", task="task", workspace=None)
    assert result.status is RunStatus.SUCCEEDED
    assert len(trace.events) == backend.committed_events
    assert sum(event.type == "checkpoint.saved" for event in trace.events) == len(backend.saved)
    assert trace.events[-1].type == "checkpoint.saved"
    assert trace.events[-1].payload["status"] == "succeeded"


async def test_approval_audits_are_committed_once_before_dispatch_without_core_duplicates() -> None:
    backend, tool = AuditedBackend(), CountingEcho(write=True)
    runner, trace, _ = runtime(
        backend, tool, [ModelResult(action=ToolAction(call=call())), final()]
    )
    backend.trace = trace
    pending = await runner.run(task_id="task", task="task", workspace=None)
    assert pending.status is RunStatus.AWAITING_APPROVAL
    assert len(trace.events) == backend.committed_events

    def assert_committed_before_dispatch() -> None:
        assert len(trace.events) == backend.committed_events
        assert sum(event.type == "approval.consumed" for event in trace.events) == 1

    backend.before_tool = assert_committed_before_dispatch
    grant = InMemoryApprovalLedger().issue(task_id="task", call=call(), granted_by="approver")
    result = await runner.resume_approved(previous=pending, grant=grant, workspace=None)
    assert result.status is RunStatus.SUCCEEDED and tool.inputs == ["original"]
    assert len(trace.events) == backend.committed_events
    assert sum(event.type == "approval.consumed" for event in trace.events) == 1
    assert sum(event.type == "checkpoint.saved" for event in trace.events) == len(backend.saved)


@pytest.mark.parametrize("operation", ["save", "model", "tool"])
async def test_unknown_execution_propagates_without_a_terminal_checkpoint(operation: str) -> None:
    backend, tool = Backend(), CountingEcho()
    backend.suspend = operation
    runner, trace, _ = runtime(backend, tool, [ModelResult(action=ToolAction(call=call()))])
    with pytest.raises(ExecutionSuspended):
        await runner.run(task_id="task", task="task", workspace=None)
    assert all(saved.status is RunStatus.RUNNING for saved in backend.saved)
    assert not any(event.type == "run.finished" for event in trace.events)
    assert not tool.inputs


async def test_suspended_approval_is_not_converted_to_failure() -> None:
    backend, tool = Backend(), CountingEcho(write=True)
    runner, trace, _ = runtime(backend, tool, [ModelResult(action=ToolAction(call=call()))])
    suspended = await runner.run(task_id="task", task="task", workspace=None)
    backend.suspend = "approval"
    grant = InMemoryApprovalLedger().issue(task_id="task", call=call(), granted_by="approver")
    with pytest.raises(ExecutionSuspended):
        await runner.resume_approved(previous=suspended, grant=grant, workspace=None)
    assert backend.latest == suspended and not tool.inputs
    assert not any(event.type == "run.finished" for event in trace.events)


async def test_recovery_reuses_backend_output_without_dispatching_again() -> None:
    backend, tool = Backend(), CountingEcho()
    backend.recovered = DispatchResult(
        output=ToolOutput(ok=True, content="confirmed prior output"), elapsed_ms=0
    )
    runner, _, model = runtime(backend, tool, [final()])
    result = await runner.resume_running(previous=previous(backend), workspace=None)
    assert result.status is RunStatus.SUCCEEDED and result.usage.tool_calls == 1
    assert not backend.tools and not tool.inputs
    assert model.requests[0].messages[-1].content == "confirmed prior output"
    assert backend.recoveries == [call()]


async def test_first_hosted_save_consumes_settled_tool_usage_and_missing_message() -> None:
    backend, tool = Backend(), CountingEcho()
    backend.recovered = DispatchResult(
        output=ToolOutput(
            ok=True,
            content="confirmed late tool output",
            usage=Usage(steps=2, tool_calls=3, input_tokens=17, output_tokens=8),
        ),
        elapsed_ms=0,
    )
    state = previous(backend)
    expected = Usage(steps=3, tool_calls=4, input_tokens=17, output_tokens=8)
    observed: list[RunResult] = []

    def enforce_settled_usage(saved: RunResult) -> None:
        if observed:
            return
        assert saved.usage == expected
        assert saved.messages[-1].role is MessageRole.TOOL
        assert saved.messages[-1].content == "confirmed late tool output"
        assert saved.messages[-1].tool_call_id == call().id
        assert saved.authorized_call_id is None
        observed.append(saved)

    backend.before_save = enforce_settled_usage
    runner, _, _ = runtime(backend, tool, [final()])

    result = await runner.resume_running(previous=state, workspace=None)

    assert result.status is RunStatus.SUCCEEDED
    assert len(observed) == 1
    assert observed[0].checkpoint_revision == state.checkpoint_revision
    assert backend.saved[0].usage == expected
    assert backend.saved[0].messages == observed[0].messages
    assert backend.saved[0].checkpoint_revision == state.checkpoint_revision + 1
    assert not backend.tools and not tool.inputs


async def test_first_hosted_save_consumes_a_confirmed_model_response_and_its_usage() -> None:
    backend, tool = Backend(), CountingEcho()
    state = previous(backend).model_copy(
        update={
            "messages": (Message(role=MessageRole.USER, content="original request"),),
            "authorized_call_id": None,
        }
    )
    backend.latest = state
    expected = Usage(steps=2, input_tokens=17, output_tokens=8)
    backend.recovered_model = ModelCallOutcome(
        kind=ModelCallKind.SUCCESS,
        usage=expected,
        result=ModelResult(
            action=FinalAction(content="confirmed delayed model output"),
            usage=ModelUsage(input_tokens=17, output_tokens=8),
        ),
        response_received=True,
    )

    def enforce_settled_usage(saved: RunResult) -> None:
        assert saved.usage == expected
        assert saved.messages[-1].role is MessageRole.ASSISTANT
        assert saved.messages[-1].content == "confirmed delayed model output"

    backend.before_save = enforce_settled_usage
    runner, _, model = runtime(backend, tool, [])

    result = await runner.resume_running(previous=state, workspace=None)

    assert result.status is RunStatus.SUCCEEDED
    assert len(backend.saved) == 1
    assert backend.saved[0] == result
    assert result.checkpoint_revision == state.checkpoint_revision + 1
    assert not model.requests and not backend.tools and not tool.inputs


async def test_proven_unadmitted_backend_recovery_uses_a_fresh_permit() -> None:
    backend, tool = Backend(), CountingEcho()
    runner, _, _ = runtime(backend, tool, [final()])
    result = await runner.resume_running(previous=previous(backend), workspace=None)
    assert result.status is RunStatus.SUCCEEDED
    assert backend.recoveries == [call()] and len(backend.tools) == 1
    assert tool.inputs == ["original"]


@pytest.mark.parametrize("operation", ["current", "recover"])
async def test_unknown_recovery_propagates_without_dispatch_or_failure(operation: str) -> None:
    backend, tool = Backend(), CountingEcho()
    backend.suspend = operation
    runner, trace, _ = runtime(backend, tool, [])
    with pytest.raises(ExecutionSuspended):
        await runner.resume_running(previous=previous(backend), workspace=None)
    assert not tool.inputs and not backend.tools
    assert not backend.saved
    assert backend.latest is not None and backend.latest.status is RunStatus.RUNNING
    assert not any(event.type == "run.finished" for event in trace.events)


async def test_unadmitted_recovery_still_requires_checkpointed_authorization() -> None:
    backend, tool = Backend(), CountingEcho()
    state = previous(backend).model_copy(update={"authorized_call_id": None})
    backend.latest = state
    runner, _, _ = runtime(backend, tool, [])
    result = await runner.resume_running(previous=state, workspace=None)
    assert result.status is RunStatus.FAILED
    assert result.error == "recovery_unsupported: missing execution authorization"
    assert not tool.inputs and not backend.tools


async def test_one_backend_permit_cannot_dispatch_twice() -> None:
    backend, tool = Backend(), CountingEcho()
    backend.repeat_dispatch = True
    runner, trace, _ = runtime(backend, tool, [ModelResult(action=ToolAction(call=call()))])
    with pytest.raises(ExecutionSuspended, match="permit"):
        await runner.run(task_id="task", task="task", workspace=None)
    assert tool.inputs == ["original"]
    assert not any(event.type == "run.finished" for event in trace.events)


@pytest.mark.parametrize("suspend", [False, True])
async def test_cached_dispatch_permit_expires_when_backend_returns_or_suspends(
    suspend: bool,
) -> None:
    backend, tool = Backend(), CountingEcho()
    backend.cached_tool = True
    if suspend:
        backend.suspend = "tool"
    runner, _, _ = runtime(backend, tool, [ModelResult(action=ToolAction(call=call())), final()])
    if suspend:
        with pytest.raises(ExecutionSuspended):
            await runner.run(task_id="task", task="task", workspace=None)
    else:
        await runner.run(task_id="task", task="task", workspace=None)
    assert backend.dispatch is not None
    with pytest.raises(ExecutionSuspended, match="permit"):
        await backend.dispatch(CANONICAL_KEY)
    assert not tool.inputs


@pytest.mark.parametrize("key", ["a", CANONICAL_KEY, "x" * 256])
async def test_actual_tool_receives_exact_backend_idempotency_key(key: str) -> None:
    backend, tool = Backend(), CountingEcho()
    backend.idempotency_key = key
    runner, _, _ = runtime(backend, tool, [ModelResult(action=ToolAction(call=call())), final()])

    result = await runner.run(task_id="task", task="task", workspace=None)

    assert result.status is RunStatus.SUCCEEDED
    assert tool.idempotency_keys == [key]


@pytest.mark.parametrize(
    "key", [None, True, 7, b"bytes", "", "x" * 257, "bad\x00key", "bad\nkey", "bad key", "路径"]
)
async def test_invalid_backend_key_never_enters_a_tool_or_renews_the_permit(key: Any) -> None:
    backend, tool = Backend(), CountingEcho()
    backend.idempotency_key = key
    runner, trace, _ = runtime(
        backend, tool, [ModelResult(action=ToolAction(call=call())), final()]
    )

    with pytest.raises(ValueError, match="idempotency key"):
        await runner.run(task_id="task", task="task", workspace=None)

    assert not tool.inputs and not tool.idempotency_keys
    assert not any(event.type == "run.finished" for event in trace.events)
    assert backend.dispatch is not None
    with pytest.raises(ExecutionSuspended, match="permit"):
        await backend.dispatch(CANONICAL_KEY)
    assert not tool.inputs


async def test_journal_rejects_an_external_key_before_any_claim(tmp_path: Path) -> None:
    tool = CountingEcho()
    registry = ToolRegistry()
    registry.register(tool)
    journal = SQLiteInvocationJournal(tmp_path / "journal.sqlite")
    executor = ToolExecutor(
        registry=registry,
        dispatcher=ToolDispatcher(registry),
        policy=RiskBasedPolicy(),
        trace=InMemoryTrace("task"),
        budget=RunBudget(),
        context=lambda: None,
        journal=journal,
    )

    with pytest.raises(ValueError, match="cannot share authority"):
        await executor.dispatch(
            call(), None, Usage(), task_id="task", idempotency_key=CANONICAL_KEY
        )

    assert not tool.inputs
    assert journal.load(run_id="task", logical_call_id="call") is None
