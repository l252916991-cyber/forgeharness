"""Minimal model/tool loop with Harness-owned policy, budget, and trace."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from pathlib import Path
from time import monotonic
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as JSONSchemaValidationError

from forgeharness.context.assembly import ContextAssembler
from forgeharness.context.compression import ObservationCompressor, observation_spill_dir
from forgeharness.context.spill import MessageSpill
from forgeharness.domain.models import (
    CURRENT_RECOVERY_SEMANTICS_VERSION,
    FinalAction,
    Message,
    MessageRole,
    PendingApproval,
    RunContext,
    RunResult,
    RunStatus,
    ToolAction,
    ToolCall,
    Usage,
)
from forgeharness.models.base import Model, ModelRequest
from forgeharness.models.prepared import canonical_json
from forgeharness.observability.trace import TraceRecorder
from forgeharness.runtime.budget import RunBudget
from forgeharness.runtime.execution import AsyncExecutionBackend, ExecutionSuspended
from forgeharness.runtime.model_call import ModelCallKind, invoke_model_once
from forgeharness.runtime.todos import (
    TodoBoard,
    TodoSnapshot,
    WriteTodosTool,
    replay_board,
    todo_pinned_message,
)
from forgeharness.runtime.tool_executor import RecoveryOutcome, ToolExecutor
from forgeharness.runtime.verification import (
    ToolEvidence,
    VerificationRequest,
    VerificationResult,
    Verifier,
)
from forgeharness.state.approval import ApprovalGrant, ApprovalLedger
from forgeharness.state.checkpoint import CheckpointConflict, CheckpointStore
from forgeharness.state.invocation_journal import (
    InvocationJournal,
    InvocationRecord,
    JournalError,
    RecoveryDecision,
    SQLiteInvocationJournal,
    args_digest,
)
from forgeharness.tools.base import (
    SchemaSource,
    ToolEffectClass,
    ToolErrorCode,
    ToolFailure,
)
from forgeharness.tools.dispatcher import DispatchResult, ToolDispatcher
from forgeharness.tools.policy import PolicyDecisionType, ToolPolicy
from forgeharness.tools.registry import ToolRegistry


class AgentRuntime:
    """Execute validated model decisions under deterministic Harness controls."""

    def __init__(
        self,
        *,
        model: Model,
        registry: ToolRegistry,
        dispatcher: ToolDispatcher,
        policy: ToolPolicy,
        trace: TraceRecorder,
        budget: RunBudget | None = None,
        approval_ledger: ApprovalLedger | None = None,
        checkpoint_store: CheckpointStore | None = None,
        observation_compressor: ObservationCompressor | None = None,
        observation_spill: bool = False,
        context_assembler: ContextAssembler | None = None,
        verifier: Verifier | None = None,
        invocation_journal: InvocationJournal | None = None,
        sleep_fn: Callable[[float], Awaitable[None]] | None = None,
        clock_fn: Callable[[], float] | None = None,
        execution_backend: AsyncExecutionBackend | None = None,
        todos: TodoBoard | None = None,
        max_todo_rejections: int = 3,
        message_spill: bool = False,
    ) -> None:
        if execution_backend is not None and any(
            store is not None for store in (checkpoint_store, approval_ledger, invocation_journal)
        ):
            raise ValueError("execution_backend cannot share authority with local state stores")
        if max_todo_rejections < 0:
            raise ValueError("max_todo_rejections must not be negative")
        self._execution_backend = execution_backend
        self._model = model
        self._registry = registry
        self._dispatcher = dispatcher
        self._policy = policy
        self._trace = trace
        self._budget = budget or RunBudget()
        self._approval_ledger = approval_ledger
        self._checkpoint_store = checkpoint_store
        self._observation_compressor = observation_compressor or ObservationCompressor()
        # Compressed originals are kept only when a host asks: spilling writes into
        # the workspace, and a runtime must not create files nobody requested.
        self._observation_spill = observation_spill
        # Compacted *messages* are only recoverable when a host asks for the same
        # treatment observations already get: spilling writes into the workspace.
        self._message_spill = message_spill
        # A todo board is the difference between a plan and a plan the model can
        # amend. When one is supplied the runtime also pins the objective, because
        # a board that outlives the request that created it is, by definition, a
        # long-horizon run -- exactly the case where the objective must not be
        # eligible for compaction.
        self._todos = todos
        self._max_todo_rejections = max_todo_rejections
        self._todo_rejections = 0
        self._context_assembler = context_assembler or ContextAssembler(
            max_tokens=self._budget.max_context_tokens, pin_task=todos is not None
        )
        if todos is not None and registry.get("write_todos") is None:
            # The board and the tool that writes it are one capability; a host that
            # asks for todo state should not also have to remember a registration.
            registry.register(WriteTodosTool(todos))
        self._verifier = verifier
        # Optional for the same reason the checkpoint store is: a runtime without
        # durable state still runs, it just cannot promise side-effect recovery.
        self._invocation_journal = invocation_journal
        # Injected so tests exercise backoff and deadline paths without real waiting.
        self._sleep_fn = sleep_fn
        self._clock = clock_fn or monotonic
        self._context: ContextVar[RunContext | None] = ContextVar("run_context", default=None)
        self._evidence: ContextVar[list[ToolEvidence]] = ContextVar("tool_evidence")
        self._authorized: ContextVar[str | None] = ContextVar("authorized_call", default=None)
        self._tools = ToolExecutor(
            registry=registry,
            dispatcher=dispatcher,
            policy=policy,
            trace=trace,
            budget=self._budget,
            context=self._context.get,
            journal=invocation_journal,
            sleep_fn=sleep_fn,
        )

    async def run(
        self,
        *,
        task_id: str,
        task: str,
        workspace: Path | None,
        instructions: tuple[Message, ...] = (),
        initial_usage: Usage | None = None,
        run_context: RunContext | None = None,
    ) -> RunResult:
        """Run until a final answer, approval suspension, failure, or budget exhaustion."""
        self._context.set(run_context)
        self._evidence.set([])
        self._authorized.set(None)
        self._bind_observation_spill(workspace)
        if run_context is not None and workspace != run_context.scope.workspace:
            raise ValueError("workspace does not match trusted scope")
        if any(message.role != MessageRole.SYSTEM for message in instructions):
            raise ValueError("runtime instructions must be system messages")
        # A new run is a new task: an inherited board would gate this run on work
        # that belonged to a previous one.
        self._reset_todos()
        messages = [*instructions, Message(role=MessageRole.USER, content=task)]
        usage = initial_usage or Usage()
        self._trace.append("run.started", {"workspace": str(workspace)})
        initial = await self._checkpoint(
            RunResult(
                task_id=task_id,
                status=RunStatus.RUNNING,
                messages=tuple(messages),
                usage=usage,
            )
        )
        return await self._drive(
            task_id=task_id,
            messages=messages,
            usage=usage,
            workspace=workspace,
            checkpoint_revision=initial.checkpoint_revision,
        )

    async def resume_approved(
        self, *, previous: RunResult, grant: ApprovalGrant, workspace: Path | None
    ) -> RunResult:
        """Consume an exact approval, execute the suspended call, and continue the run."""
        if self._execution_backend is not None:
            previous = previous.model_copy(deep=True)
        if previous.status != RunStatus.AWAITING_APPROVAL or previous.pending_approval is None:
            raise ValueError("only an awaiting-approval run can be resumed")
        if self._approval_ledger is None and self._execution_backend is None:
            raise RuntimeError("runtime has no approval ledger")
        await self._assert_current(previous)
        self._context.set(previous.run_context)
        self._evidence.set(list(previous.tool_evidence))
        self._authorized.set(previous.authorized_call_id)
        self._bind_observation_spill(workspace)
        if previous.run_context is not None and workspace != previous.run_context.scope.workspace:
            raise ValueError("workspace does not match trusted scope")
        self._restore_todos(previous.messages, previous.tool_evidence)
        call = previous.pending_approval.call.model_copy(deep=True)
        tool = self._registry.get(call.name)
        if tool is None:
            raise ValueError(f"approved tool is no longer registered: {call.name}")
        current_decision = self._tools.authorize(call, task_id=previous.task_id, emit=False)
        if current_decision.type == PolicyDecisionType.DENY:
            raise ValueError(f"approved tool is now denied: {current_decision.reason}")
        self._authorized.set(call.id)
        candidate = self._snapshot(
            previous.model_copy(update={"status": RunStatus.RUNNING, "pending_approval": None})
        )
        from forgeharness.state.approval import AtomicApprovalLedger

        if self._execution_backend is not None:
            claimed = await self._execution_backend.consume_approval(
                grant, previous=previous, candidate=candidate, call=call.model_copy(deep=True)
            )
        elif isinstance(self._approval_ledger, AtomicApprovalLedger):
            claimed = self._approval_ledger.consume_and_checkpoint(
                grant,
                task_id=previous.task_id,
                call=call,
                result=candidate,
                checkpoint_store=self._checkpoint_store,
            )
            self._trace.append("checkpoint.saved", {"revision": claimed.checkpoint_revision})
        else:
            assert self._approval_ledger is not None
            self._approval_ledger.consume(grant, task_id=previous.task_id, call=call)
            claimed = await self._checkpoint(candidate)
        if self._execution_backend is None:
            self._trace.append(
                "approval.consumed", {"tool": call.name, "granted_by": grant.granted_by}
            )
        messages = list(previous.messages)
        dispatch = await self._dispatch_journalled(
            call,
            workspace,
            previous.usage,
            task_id=previous.task_id,
            checkpoint_revision=claimed.checkpoint_revision,
        )
        usage = self._account_tool_usage(previous.usage, dispatch.output.usage)
        evidence = _evidence(
            call.name, dispatch.output.ok, dispatch.output.content, dispatch.output.metadata
        ).model_copy(update={"logical_call_id": call.id, "receipt": dispatch.output.receipt})
        self._evidence.get().append(evidence)
        self._authorized.set(None)
        observation = self._observation(dispatch.output.content)
        messages.append(
            Message(
                role=MessageRole.TOOL,
                content=observation,
                tool_call_id=call.id,
                tool_name=call.name,
            )
        )
        if call.name == "write_todos":
            self._restore_todos(tuple(messages), tuple(self._evidence.get()))
        self._trace.append(
            "tool.completed",
            self._tool_event(call, dispatch, observation, usage),
        )
        running = await self._checkpoint(
            RunResult(
                task_id=previous.task_id,
                status=RunStatus.RUNNING,
                messages=tuple(messages),
                usage=usage,
                checkpoint_revision=claimed.checkpoint_revision,
            )
        )
        return await self._drive(
            task_id=previous.task_id,
            messages=messages,
            usage=usage,
            workspace=workspace,
            checkpoint_revision=running.checkpoint_revision,
            tool_evidence=tuple(self._evidence.get()),
        )

    async def resume_running(self, *, previous: RunResult, workspace: Path | None) -> RunResult:
        """Resume a RUNNING checkpoint whose last step may have had a side effect.

        This is the path the crash windows exercise. The checkpoint records control
        flow only, so the journal is consulted for the outstanding call and decides
        whether its effect already happened:

        * ``completed`` — reuse the stored observation and append it as the tool
          message the checkpoint is missing, then continue. The tool is **not**
          called again.
        * ``started`` on a non-idempotent tool — the effect's occurrence cannot be
          established, so the run fails with an explicit indeterminate error rather
          than guessing.
        * ``claimed`` or replay-safe ``started`` — restart with current policy,
          the same logical key and the remaining physical attempt budget.
        * no row — execute only when generation 3 checkpoint records admission.

        A run without an invocation journal cannot make these guarantees; an
        unresolved call therefore fails closed.
        """
        if self._execution_backend is not None:
            previous = previous.model_copy(deep=True)
        if previous.status is not RunStatus.RUNNING:
            raise ValueError("only a running run can be resumed this way")
        await self._assert_current(previous)
        self._context.set(previous.run_context)
        self._evidence.set(list(previous.tool_evidence))
        self._authorized.set(previous.authorized_call_id)
        self._bind_observation_spill(workspace)
        if previous.run_context is not None and workspace != previous.run_context.scope.workspace:
            raise ValueError("workspace does not match trusted scope")
        self._restore_todos(previous.messages, previous.tool_evidence)
        messages = list(previous.messages)
        usage = previous.usage
        if self._execution_backend is not None:
            # The hosted backend already fenced this worker with its current
            # ownership claim. A late receipt may have advanced actual usage
            # before this transcript consumed it; recover first, then save the
            # matching usage and messages together.
            claimed = previous
        else:
            claimed = await self._checkpoint(previous)
        checkpoint_revision = claimed.checkpoint_revision
        evidence = self._evidence.get()
        outstanding = _outstanding_call(messages)
        if (
            outstanding is not None
            and self._execution_backend is None
            and previous.recovery_semantics_version < CURRENT_RECOVERY_SEMANTICS_VERSION
        ):
            # A pre-journal run has no durable record of its tool side effects, and
            # the trace cannot stand in for one: a missing `tool.completed` event
            # never proves the tool did not run. ADR 009 requires failing closed
            # rather than reconstructing a journal by inference.
            self._trace.append(
                "tool.recovery.decided",
                {
                    "logical_call_id": outstanding.id,
                    "tool": outstanding.name,
                    "decision": RecoveryDecision.REFUSE_REPLAY.value,
                    "reason": "pre_journal_run",
                    "recovery_semantics_version": previous.recovery_semantics_version,
                },
            )
            return await self._finish(
                task_id=previous.task_id,
                status=RunStatus.FAILED,
                messages=messages,
                usage=usage,
                checkpoint_revision=checkpoint_revision,
                error=(
                    "indeterminate_side_effect: this run predates the invocation "
                    f"journal, so whether {outstanding.name} already ran cannot be "
                    "established; it is not replayed"
                ),
            )
        if outstanding is not None:
            try:
                if self._execution_backend is not None:
                    tool = self._registry.get(outstanding.name)
                    if tool is None:
                        raise JournalError("tool no longer registered")
                    decision = self._tools.authorize(
                        outstanding, task_id=previous.task_id, emit=False
                    )
                    if decision.type is PolicyDecisionType.DENY:
                        raise JournalError("recovery denied by current policy")
                    if (
                        decision.type is PolicyDecisionType.REQUIRE_APPROVAL
                        and previous.authorized_call_id != outstanding.id
                    ):
                        raise JournalError("recovery requires a fresh approval")
                    dispatch = await self._execution_backend.recover_tool(
                        task_id=previous.task_id, call=outstanding.model_copy(deep=True)
                    )
                    if dispatch is None:
                        if previous.authorized_call_id != outstanding.id:
                            raise JournalError(
                                "recovery_unsupported: missing execution authorization"
                            )
                        dispatch = await self._dispatch_journalled(
                            outstanding,
                            workspace,
                            usage,
                            task_id=previous.task_id,
                            checkpoint_revision=checkpoint_revision,
                        )
                else:
                    outcome = self.recover_call(
                        task_id=previous.task_id, logical_call_id=outstanding.id
                    )
                    if outcome is not None and outcome.decision is RecoveryDecision.REFUSE_REPLAY:
                        raise JournalError(
                            "indeterminate_side_effect: unsafe invocation is not replayed"
                        )
                    tool = self._registry.get(outstanding.name)
                    if tool is None:
                        raise JournalError("tool no longer registered")
                    decision = self._tools.authorize(
                        outstanding, task_id=previous.task_id, emit=False
                    )
                    if decision.type is PolicyDecisionType.DENY:
                        raise JournalError("recovery denied by current policy")
                    if (
                        decision.type is PolicyDecisionType.REQUIRE_APPROVAL
                        and previous.authorized_call_id != outstanding.id
                    ):
                        raise JournalError("recovery requires a fresh approval")
                    if outcome is not None:
                        record = outcome.record
                        if (
                            record.tool_name != outstanding.name
                            or record.args_digest != args_digest(outstanding.arguments)
                            or record.effect_class != tool.spec.effect_class
                        ):
                            raise JournalError(
                                "journal call identity or effect declaration changed"
                            )
                    if outcome is not None and outcome.decision is RecoveryDecision.REUSE_RESULT:
                        stored = outcome.record.result
                        if stored is None:
                            raise JournalError("completed invocation has no result")
                        dispatch = DispatchResult(output=stored, elapsed_ms=0)
                    elif outcome is not None and isinstance(
                        self._invocation_journal, SQLiteInvocationJournal
                    ):
                        started = self._invocation_journal.restart(
                            outcome.record, checkpoint_revision=checkpoint_revision
                        )
                        dispatch = await self._run_attempts(
                            call=outstanding,
                            record=outcome.record,
                            started=started,
                            workspace=workspace,
                            usage=usage,
                            task_id=previous.task_id,
                            effect_class=tool.spec.effect_class,
                        )
                    elif (
                        outcome is None
                        and self._invocation_journal is not None
                        and previous.authorized_call_id == outstanding.id
                    ):
                        dispatch = await self._dispatch_journalled(
                            outstanding,
                            workspace,
                            usage,
                            task_id=previous.task_id,
                            checkpoint_revision=checkpoint_revision,
                        )
                    else:
                        raise JournalError("recovery_unsupported: missing execution authorization")
            except JournalError as exc:
                return await self._finish(
                    task_id=previous.task_id,
                    status=RunStatus.FAILED,
                    messages=messages,
                    usage=usage,
                    checkpoint_revision=checkpoint_revision,
                    error=str(exc),
                    error_type="recovery_refused",
                )
            stored = dispatch.output
            observation = self._observation(stored.content)
            messages.append(
                Message(
                    role=MessageRole.TOOL,
                    content=observation,
                    tool_call_id=outstanding.id,
                    tool_name=outstanding.name,
                )
            )
            usage = self._account_tool_usage(usage, stored.usage)
            evidence.append(
                _evidence(outstanding.name, stored.ok, stored.content, stored.metadata).model_copy(
                    update={"logical_call_id": outstanding.id, "receipt": stored.receipt}
                )
            )
            if outstanding.name == "write_todos":
                self._restore_todos(tuple(messages), tuple(evidence))
            self._authorized.set(None)
            self._trace.append(
                "tool.completed", self._tool_event(outstanding, dispatch, observation, usage)
            )
            checkpoint_revision = await self._save_running(
                previous.task_id, messages, usage, checkpoint_revision
            )
        return await self._drive(
            task_id=previous.task_id,
            messages=messages,
            usage=usage,
            workspace=workspace,
            checkpoint_revision=checkpoint_revision,
            tool_evidence=tuple(evidence),
        )

    async def _drive(
        self,
        *,
        task_id: str,
        messages: list[Message],
        usage: Usage,
        workspace: Path | None,
        checkpoint_revision: int,
        tool_evidence: tuple[ToolEvidence, ...] = (),
    ) -> RunResult:
        """Continue a new or resumed run from validated in-memory state."""

        evidence = self._evidence.get()
        if tool_evidence and not evidence:
            evidence.extend(tool_evidence)
        violations = 0
        # A fresh allowance per invocation: the wall clock that matters is how long
        # *this* process has been working, not how long the task has existed. A run
        # resumed after a crash therefore gets its own budget rather than inheriting
        # an already-expired one.
        deadline = (
            None
            if self._budget.max_wall_seconds is None
            else self._clock() + self._budget.max_wall_seconds
        )
        while usage.steps < self._budget.max_steps:
            if deadline is not None and self._clock() >= deadline:
                return await self._finish(
                    task_id=task_id,
                    status=RunStatus.FAILED,
                    messages=messages,
                    usage=usage,
                    checkpoint_revision=checkpoint_revision,
                    error=(
                        "deadline_exceeded: the run passed its "
                        f"{self._budget.max_wall_seconds}s wall-clock budget"
                    ),
                    error_type="deadline_exceeded",
                )
            if self._tokens_exhausted(usage):
                return await self._finish(
                    task_id=task_id,
                    status=RunStatus.EXHAUSTED,
                    messages=messages,
                    usage=usage,
                    checkpoint_revision=checkpoint_revision,
                    error="token budget exhausted",
                )
            outcome = await invoke_model_once(
                model=self._model,
                request=ModelRequest(
                    task_id=task_id, messages=tuple(messages), tools=self._registry.specs()
                ),
                budget=self._budget,
                assembler=self._context_assembler,
                trace=self._trace,
                usage=usage,
                phase="executor",
                execution_backend=self._execution_backend,
            )
            usage = outcome.usage
            if outcome.kind is ModelCallKind.CONTEXT_OVERFLOW:
                return await self._finish(
                    task_id=task_id,
                    status=RunStatus.FAILED,
                    messages=messages,
                    usage=usage,
                    checkpoint_revision=checkpoint_revision,
                    error=outcome.error,
                )
            if outcome.kind is ModelCallKind.PREPARATION_FAILED:
                return await self._finish(
                    task_id=task_id,
                    status=RunStatus.FAILED,
                    messages=messages,
                    usage=usage,
                    checkpoint_revision=checkpoint_revision,
                    error=outcome.error,
                    error_type=outcome.error_type,
                )
            if outcome.kind is ModelCallKind.MODEL_FAILED:
                return await self._finish(
                    task_id=task_id,
                    status=RunStatus.FAILED,
                    messages=messages,
                    usage=usage,
                    checkpoint_revision=checkpoint_revision,
                    error=outcome.error,
                )
            if outcome.kind is ModelCallKind.PROTOCOL_VIOLATION:
                assert outcome.violation is not None
                exc = outcome.violation
                violations += 1
                code = getattr(exc, "code", "model_protocol_error")
                self._trace.append(
                    "protocol.violation",
                    {
                        "error_code": code,
                        "received_tool_calls": getattr(exc, "received_tool_calls", None),
                        "occurrence": violations,
                        "max_protocol_violations": self._budget.max_protocol_violations,
                    },
                )
                if violations > self._budget.max_protocol_violations:
                    return await self._finish(
                        task_id=task_id,
                        status=RunStatus.FAILED,
                        messages=messages,
                        usage=usage,
                        checkpoint_revision=checkpoint_revision,
                        error=(
                            f"repeated_protocol_violation: {violations} violations of "
                            f"{code} exceeded the limit of "
                            f"{self._budget.max_protocol_violations}"
                        ),
                    )
                messages.append(Message(role=MessageRole.USER, content=_protocol_feedback(exc)))
                checkpoint_revision = await self._save_running(
                    task_id, messages, usage, checkpoint_revision
                )
                continue
            assert outcome.result is not None
            model_result = outcome.result

            if self._tokens_exhausted(usage):
                return await self._finish(
                    task_id=task_id,
                    status=RunStatus.EXHAUSTED,
                    messages=messages,
                    usage=usage,
                    checkpoint_revision=checkpoint_revision,
                    error="token budget exhausted",
                )

            action = model_result.action
            if self._execution_backend is not None:
                # Freeze nested arguments before awaiting checkpoint admission,
                # not just immediately before the physical dispatch closure.
                action = action.model_copy(deep=True)
            if isinstance(action, FinalAction):
                messages.append(Message(role=MessageRole.ASSISTANT, content=action.content))
                blocking = self._open_todos()
                if blocking is not None:
                    # Honouring the claim here would let a run with three unfinished
                    # steps report success because it finished *something*. The gate
                    # is deterministic and costs no tokens, like the verifier, but it
                    # answers a different question: not "is this evidenced" but "is
                    # this the work that was asked for".
                    rejected = self._todo_rejections + 1
                    self._trace.append(
                        "todo.final_rejected",
                        {
                            "occurrence": rejected,
                            "max_rejections": self._max_todo_rejections,
                            "revision": blocking.revision,
                            "open_count": blocking.open_count,
                            "items": [item.id for item in blocking.open_items],
                        },
                    )
                    if rejected > self._max_todo_rejections:
                        return await self._finish(
                            task_id=task_id,
                            status=RunStatus.FAILED,
                            messages=messages,
                            usage=usage,
                            checkpoint_revision=checkpoint_revision,
                            error=(
                                "unfinished_todos: the model finished "
                                f"{rejected} times with {blocking.open_count} todo item(s) "
                                f"still open after {self._max_todo_rejections} rejection(s)"
                            ),
                            error_type="unfinished_todos",
                        )
                    self._todo_rejections = rejected
                    messages.append(
                        Message(role=MessageRole.USER, content=_todo_feedback(blocking))
                    )
                    checkpoint_revision = await self._save_running(
                        task_id, messages, usage, checkpoint_revision
                    )
                    continue
                if self._verifier is None:
                    return await self._finish(
                        task_id=task_id,
                        status=RunStatus.SUCCEEDED,
                        messages=messages,
                        usage=usage,
                        checkpoint_revision=checkpoint_revision,
                        final_output=action.content,
                    )
                try:
                    verdict = await self._verifier.verify(
                        VerificationRequest(
                            task_id=task_id,
                            final=action,
                            workspace=workspace,
                            tool_results=tuple(evidence),
                            run_context=self._context.get(),
                        )
                    )
                except ExecutionSuspended:
                    raise
                except Exception as exc:
                    self._trace.append(
                        "verification.failed",
                        {"error_type": type(exc).__name__, "error": str(exc)},
                    )
                    return await self._finish(
                        task_id=task_id,
                        status=RunStatus.FAILED,
                        messages=messages,
                        usage=usage,
                        checkpoint_revision=checkpoint_revision,
                        error=f"verification failed: {type(exc).__name__}: {exc}",
                    )
                self._trace.append(
                    "verification.passed" if verdict.passed else "verification.rejected",
                    {"reason": verdict.reason, "evidence": list(verdict.evidence)},
                )
                if verdict.passed:
                    return await self._finish(
                        task_id=task_id,
                        status=RunStatus.SUCCEEDED,
                        messages=messages,
                        usage=usage,
                        checkpoint_revision=checkpoint_revision,
                        final_output=action.content,
                    )
                messages.append(
                    Message(role=MessageRole.USER, content=_verification_feedback(verdict))
                )
                checkpoint_revision = await self._save_running(
                    task_id, messages, usage, checkpoint_revision
                )
                continue

            if not isinstance(action, ToolAction):
                raise AssertionError(f"unhandled action type: {type(action).__name__}")

            if any(
                previous.id == action.call.id
                for message in messages
                for previous in message.tool_calls
            ):
                self._trace.append(
                    "protocol.violation",
                    {
                        "error_code": "duplicate_tool_call_id",
                        "call_id": action.call.id,
                        "recoverable": False,
                    },
                )
                return await self._finish(
                    task_id=task_id,
                    status=RunStatus.FAILED,
                    messages=messages,
                    usage=usage,
                    checkpoint_revision=checkpoint_revision,
                    error="model reused a prior tool call id",
                    error_type="duplicate_tool_call_id",
                )

            if usage.tool_calls >= self._budget.max_tool_calls:
                return await self._finish(
                    task_id=task_id,
                    status=RunStatus.EXHAUSTED,
                    messages=messages,
                    usage=usage,
                    checkpoint_revision=checkpoint_revision,
                    error="tool-call budget exhausted",
                )

            tool = self._registry.get(action.call.name)
            invalid_arguments = False
            if self._execution_backend is not None and tool is not None:
                try:
                    if tool.spec.schema_source is SchemaSource.EXTERNAL_JSON_SCHEMA:
                        Draft202012Validator(tool.spec.input_schema).validate(action.call.arguments)
                    normalized = tool.input_model.model_validate(action.call.arguments).model_dump(
                        mode="json"
                    )
                    changed = canonical_json(normalized) != canonical_json(action.call.arguments)
                except (TypeError, ValueError, JSONSchemaValidationError):
                    # Evaluate policy before reporting this deterministic refusal,
                    # but never admit malformed input as a physical hosted call.
                    invalid_arguments = True
                else:
                    action = ToolAction(
                        call=ToolCall(
                            id=action.call.id, name=action.call.name, arguments=normalized
                        )
                    )
                    if changed:
                        self._trace.append(
                            "tool.arguments.normalized",
                            {
                                "call_id": action.call.id,
                                "tool": action.call.name,
                                "arguments": action.call.model_dump(mode="json")["arguments"],
                            },
                        )
            messages.append(Message(role=MessageRole.ASSISTANT, tool_calls=(action.call,)))
            if tool is None:
                # The registry has no spec for an unknown tool, so policy cannot be
                # consulted; the failure still uses the shared structured contract so
                # the model sees one format for every tool failure.
                unknown = ToolFailure(
                    code=ToolErrorCode.INVALID_ARGUMENTS,
                    message="No such tool is registered.",
                    recoverable=True,
                    details={"tool": action.call.name},
                    suggestion="Choose one of the available tools.",
                )
                messages.append(self._tool_message(action, unknown.as_observation()))
                self._trace.append(
                    "tool.unknown",
                    {
                        "call_id": action.call.id,
                        "tool": action.call.name,
                        "error_code": unknown.code.value,
                        "recoverable": unknown.recoverable,
                    },
                )
                usage = usage.model_copy(update={"tool_calls": usage.tool_calls + 1})
                checkpoint_revision = await self._save_running(
                    task_id, messages, usage, checkpoint_revision
                )
                continue

            decision = self._tools.authorize(action.call, task_id=task_id)
            if invalid_arguments and (
                decision.type is not PolicyDecisionType.DENY
                or decision.rule == "harness.invalid_arguments"
            ):
                invalid = ToolFailure(
                    code=ToolErrorCode.INVALID_ARGUMENTS,
                    message="The arguments do not match the tool's required schema.",
                    recoverable=True,
                    suggestion="Send arguments that satisfy the tool's input schema.",
                )
                messages.append(self._tool_message(action, invalid.as_observation()))
                self._trace.append(
                    "tool.invalid_arguments",
                    {
                        "call_id": action.call.id,
                        "tool": action.call.name,
                        "error_code": invalid.code.value,
                        "recoverable": invalid.recoverable,
                    },
                )
                usage = usage.model_copy(update={"tool_calls": usage.tool_calls + 1})
                checkpoint_revision = await self._save_running(
                    task_id, messages, usage, checkpoint_revision
                )
                continue
            if decision.type == PolicyDecisionType.REQUIRE_APPROVAL:
                self._trace.append(
                    "run.awaiting_approval",
                    {"call_id": action.call.id, "tool": action.call.name},
                )
                return await self._checkpoint(
                    RunResult(
                        task_id=task_id,
                        status=RunStatus.AWAITING_APPROVAL,
                        messages=tuple(messages),
                        usage=usage,
                        checkpoint_revision=checkpoint_revision,
                        pending_approval=PendingApproval(call=action.call, reason=decision.reason),
                    )
                )
            if decision.type == PolicyDecisionType.DENY:
                messages.append(
                    self._tool_message(action, f"policy denied tool: {decision.reason}")
                )
                usage = usage.model_copy(update={"tool_calls": usage.tool_calls + 1})
                checkpoint_revision = await self._save_running(
                    task_id, messages, usage, checkpoint_revision
                )
                continue

            # Persist the admitted call before any side effect. If this task is
            # cancelled inside the tool, recovery can pair the checkpointed call
            # with its STARTED journal row and refuse an unsafe replay.
            self._authorized.set(action.call.id)
            checkpoint_revision = await self._save_running(
                task_id, messages, usage, checkpoint_revision
            )
            dispatch = await self._dispatch_journalled(
                action.call,
                workspace,
                usage,
                task_id=task_id,
                checkpoint_revision=checkpoint_revision,
            )
            usage = self._account_tool_usage(usage, dispatch.output.usage)
            evidence.append(
                _evidence(
                    action.call.name,
                    dispatch.output.ok,
                    dispatch.output.content,
                    dispatch.output.metadata,
                ).model_copy(
                    update={"logical_call_id": action.call.id, "receipt": dispatch.output.receipt}
                )
            )
            self._authorized.set(None)
            observation = self._observation(dispatch.output.content)
            messages.append(self._tool_message(action, observation))
            self._trace.append(
                "tool.completed",
                self._tool_event(action.call, dispatch, observation, usage),
            )
            self._trace_todo_update(action.call, dispatch)
            checkpoint_revision = await self._save_running(
                task_id, messages, usage, checkpoint_revision
            )

        return await self._finish(
            task_id=task_id,
            status=RunStatus.EXHAUSTED,
            messages=messages,
            usage=usage,
            checkpoint_revision=checkpoint_revision,
            error="step budget exhausted",
        )

    @property
    def budget(self) -> RunBudget:
        """Expose the immutable host budget to the planner composition."""
        return self._budget

    def _tokens_exhausted(self, usage: Usage) -> bool:
        return (
            usage.input_tokens >= self._budget.max_input_tokens
            or usage.output_tokens >= self._budget.max_output_tokens
        )

    async def _dispatch_journalled(
        self,
        call: ToolCall,
        workspace: Path | None,
        usage: Usage,
        *,
        task_id: str,
        checkpoint_revision: int,
    ) -> DispatchResult:
        if self._execution_backend is None:
            return await self._tools.dispatch(
                call, workspace, usage, task_id=task_id, checkpoint_revision=checkpoint_revision
            )
        # Keep backend-facing arguments detached from the dispatch snapshot. A
        # caller may retain the model's mutable dict even though ToolCall is frozen.
        dispatch_call = call.model_copy(deep=True)
        available = True

        async def dispatch_once(idempotency_key: str) -> DispatchResult:
            nonlocal available
            if not available:
                raise ExecutionSuspended("tool dispatch permit is no longer available")
            # Consume before the first await: concurrent closure calls cannot
            # both enter a tool, and an exception never renews the send permit.
            available = False
            key = self._tools.validate_idempotency_key(idempotency_key)
            return await self._tools.dispatch(
                dispatch_call,
                workspace,
                usage,
                task_id=task_id,
                checkpoint_revision=checkpoint_revision,
                idempotency_key=key,
            )

        try:
            return await self._execution_backend.invoke_tool(
                call=dispatch_call.model_copy(deep=True),
                usage=usage,
                task_id=task_id,
                checkpoint_revision=checkpoint_revision,
                dispatch=dispatch_once,
            )
        finally:
            # A backend cannot cache a closure and dispatch after it returned or
            # suspended. A later physical attempt needs a newly issued permit.
            available = False

    async def _run_attempts(
        self,
        *,
        call: ToolCall,
        record: InvocationRecord,
        started: InvocationRecord,
        workspace: Path | None,
        usage: Usage,
        task_id: str,
        effect_class: ToolEffectClass,
    ) -> DispatchResult:
        return await self._tools._run_attempts(
            call=call,
            record=record,
            started=started,
            workspace=workspace,
            usage=usage,
            task_id=task_id,
            effect_class=effect_class,
        )

    def recover_call(self, *, task_id: str, logical_call_id: str) -> RecoveryOutcome | None:
        return self._tools.recover_call(task_id=task_id, logical_call_id=logical_call_id)

    def _tool_event(
        self,
        call: ToolCall,
        dispatch: DispatchResult,
        observation: str,
        usage: Usage,
    ) -> dict[str, Any]:
        """Build the audit event for one executed call.

        The trace keeps the harness-facing classification (error code, exception
        class, recoverability) while the model only receives the structured
        observation. Keeping them in one place stops the two tool event sites from
        drifting apart.
        """
        return {
            "call_id": call.id,
            "tool": call.name,
            "ok": dispatch.output.ok,
            "elapsed_ms": dispatch.elapsed_ms,
            "observation": observation,
            "metadata": dispatch.output.metadata,
            "error_code": (
                dispatch.output.error.code.value if dispatch.output.error is not None else None
            ),
            "budget": self._budget_remaining(usage),
        }

    def _budget_remaining(self, usage: Usage) -> dict[str, int]:
        """Return the budget left after this step for trace-side diagnosis.

        Recording the remainder on each event lets a reader see *why* a run
        stopped — for example, steps reaching zero before a third model decision
        was refused — instead of only the final `EXHAUSTED` status.
        """
        return {
            "steps": max(0, self._budget.max_steps - usage.steps),
            "tool_calls": max(0, self._budget.max_tool_calls - usage.tool_calls),
            "input_tokens": max(0, self._budget.max_input_tokens - usage.input_tokens),
            "output_tokens": max(0, self._budget.max_output_tokens - usage.output_tokens),
        }

    def _bind_observation_spill(self, workspace: Path | None) -> None:
        """Point both compressors at this run's workspace so originals are kept.

        Compression must not destroy what it projects: a lossy projection makes the
        model re-run the tool to recover a line it needed, and that re-run costs more
        tokens than the projection saved. The directory is workspace-scoped because
        the workspace is what the run owns; a run without one keeps observations in
        memory only, which is the pre-existing behaviour, and cannot spill messages
        because there is nowhere to put them.
        """
        if self._observation_spill:
            self._observation_compressor.bind_spill_dir(
                observation_spill_dir(workspace) if workspace is not None else None
            )
        if self._message_spill and workspace is not None:
            self._context_assembler.bind_spill(MessageSpill(workspace))
        elif self._message_spill:
            self._context_assembler.bind_spill(None)
        # Live todo state is re-rendered from the board each turn instead of being
        # appended to history, so it cannot drift and cannot be summarized away.
        # Bound unconditionally: a runtime without a board answers with an empty
        # tuple, which is one code path instead of two that can disagree.
        self._context_assembler.bind_pinned(self._pinned_messages)

    def _reset_todos(self) -> None:
        """Start a run with a clean board so one task's plan is never another's."""
        self._todo_rejections = 0
        if self._todos is not None:
            self._todos.reset()

    def _restore_todos(
        self, messages: tuple[Message, ...], evidence: tuple[ToolEvidence, ...]
    ) -> None:
        """Recover the board from the transcript when resuming.

        The board is not part of the checkpoint: it is derivable, and a second copy
        of derived state is a second thing that can disagree with the transcript.
        Only a correlated successful tool output can change the board; assistant
        proposals, policy denials and failed dispatches cannot complete work.
        """
        self._todo_rejections = 0
        if self._todos is not None:
            restored = replay_board(messages, evidence=evidence)
            self._todos.restore(restored.items, revision=restored.revision)

    def _trace_todo_update(self, call: ToolCall, dispatch: DispatchResult) -> None:
        """Record every accepted board mutation, including a no-op idempotent write."""
        if self._todos is None or call.name != "write_todos" or not dispatch.output.ok:
            return
        metadata = dispatch.output.metadata
        self._trace.append(
            "todo.updated",
            {
                "call_id": call.id,
                "revision": metadata.get("revision"),
                "open_count": metadata.get("open_count"),
                "completed_count": metadata.get("completed_count"),
                "digest": metadata.get("digest"),
                "rationale": metadata.get("rationale"),
            },
        )

    def _pinned_messages(self) -> tuple[Message, ...]:
        """Render the current todo board as the block pinned into every request."""
        if self._todos is None:
            return ()
        rendered = todo_pinned_message(self._todos.snapshot())
        if rendered is None:
            return ()
        return (Message(role=MessageRole.SYSTEM, content=rendered),)

    def _open_todos(self) -> TodoSnapshot | None:
        """Return the snapshot blocking a finish, or ``None`` when the board is clear."""
        if self._todos is None:
            return None
        snapshot = self._todos.snapshot()
        return snapshot if snapshot.open_count else None

    def _observation(self, content: str) -> str:
        return self._observation_compressor.compress(content)

    @staticmethod
    def _tool_message(action: ToolAction, content: str) -> Message:
        return Message(
            role=MessageRole.TOOL,
            content=content,
            tool_call_id=action.call.id,
            tool_name=action.call.name,
        )

    async def _finish(
        self,
        *,
        task_id: str,
        status: RunStatus,
        messages: list[Message],
        usage: Usage,
        checkpoint_revision: int,
        final_output: str | None = None,
        error: str | None = None,
        error_type: str | None = None,
    ) -> RunResult:
        self._trace.append(
            "run.finished",
            {"status": status.value, "error": error, "error_type": error_type},
        )
        return await self._checkpoint(
            RunResult(
                task_id=task_id,
                status=status,
                messages=tuple(messages),
                usage=usage,
                checkpoint_revision=checkpoint_revision,
                final_output=final_output,
                error=error,
            )
        )

    async def _save_running(
        self, task_id: str, messages: list[Message], usage: Usage, revision: int
    ) -> int:
        saved = await self._checkpoint(
            RunResult(
                task_id=task_id,
                status=RunStatus.RUNNING,
                messages=tuple(messages),
                usage=usage,
                checkpoint_revision=revision,
            )
        )
        return saved.checkpoint_revision

    @staticmethod
    def _account_tool_usage(usage: Usage, child: Usage | None) -> Usage:
        return ToolExecutor.account_usage(usage, child)

    def _snapshot(self, result: RunResult) -> RunResult:
        return result.model_copy(
            update={
                "run_context": self._context.get(),
                "tool_evidence": tuple(self._evidence.get([])),
                "authorized_call_id": self._authorized.get(),
                "recovery_semantics_version": (
                    CURRENT_RECOVERY_SEMANTICS_VERSION
                    if self._invocation_journal or self._execution_backend
                    else 0
                ),
            }
        )

    async def _checkpoint(self, result: RunResult) -> RunResult:
        result = self._snapshot(result)
        if self._execution_backend is not None:
            # Hosted audit events must be part of the backend's commit, including
            # terminal checkpoints with no later operation to flush a tail.
            return await self._execution_backend.save(result)
        elif self._checkpoint_store is not None:
            saved = self._checkpoint_store.save(result)
        else:
            return result
        self._trace.append(
            "checkpoint.saved", {"revision": saved.checkpoint_revision, "status": saved.status}
        )
        return saved

    async def _assert_current(self, result: RunResult) -> None:
        if result.state_version != 1 or (
            result.recovery_semantics_version > CURRENT_RECOVERY_SEMANTICS_VERSION
        ):
            raise ValueError("checkpoint is incompatible with ReAct recovery")
        if self._execution_backend is not None:
            await self._execution_backend.assert_current(result)
            return
        if self._checkpoint_store is None:
            return
        current = self._checkpoint_store.load(result.task_id)
        if current is None or current.checkpoint_revision != result.checkpoint_revision:
            raise CheckpointConflict(f"run {result.task_id} is not the current checkpoint")


def _outstanding_call(messages: list[Message]) -> ToolCall | None:
    """Return the tool call whose result is missing, if the transcript ends mid-call.

    A crash between dispatch and the checkpoint write leaves the transcript on an
    assistant message that requests a tool call with no tool result after it.
    """
    if not messages:
        return None
    last = messages[-1]
    if last.role is MessageRole.ASSISTANT and last.tool_calls:
        return last.tool_calls[0]
    return None


def _protocol_feedback(exc: Exception) -> str:
    """Turn a recoverable violation into an actionable instruction for the model.

    Says what was wrong, that nothing was executed, and what to do instead — the
    three things a model needs to correct itself. It never names an action to
    take, because choosing the action is the model's job.
    """
    code = getattr(exc, "code", "model_protocol_error")
    received = getattr(exc, "received_tool_calls", None)
    if code == "multiple_tool_calls_not_allowed":
        detail = f" You returned {received} tool calls." if received else ""
        return (
            "Harness protocol error: this runtime allows exactly one tool call per "
            f"step.{detail} Nothing was executed. Reply with a single tool call, or a "
            "final answer if the task is complete."
        )
    return (
        f"Harness protocol error ({code}): the previous response could not be executed "
        f"as one action ({exc}). Nothing was executed. Reply with a single valid action."
    )


def _evidence(name: str, ok: bool, content: str, metadata: dict[str, object]) -> ToolEvidence:
    """Capture one executed call as structured evidence for a verifier."""
    return ToolEvidence(name=name, ok=ok, content=content, metadata=metadata)


def _todo_feedback(snapshot: TodoSnapshot) -> str:
    """Feedback for a finish refused because the board is not clear."""
    open_lines = "\n".join(
        f"- {item.id} ({item.status.value}): {item.content}" for item in snapshot.open_items
    )
    return (
        "Harness rejected the final answer: the todo board still has "
        f"{snapshot.open_count} open item(s) at revision {snapshot.revision}."
        f"\nOpen items:\n{open_lines}"
        "\nEither do the remaining work and mark each item completed with write_todos, "
        "or drop the items you no longer intend to do (and explain why in rationale), "
        "then finish again."
    )


def _verification_feedback(verdict: VerificationResult) -> str:
    """Return the model-visible reason a final answer was not accepted."""
    lines = [
        "Harness verification rejected the final answer.",
        f"Reason: {verdict.reason}",
    ]
    if verdict.evidence:
        lines.append("Evidence: " + "; ".join(verdict.evidence))
    lines.append("Provide the missing evidence or correct the work, then finish again.")
    return "\n".join(lines)
