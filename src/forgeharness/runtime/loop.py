"""Minimal model/tool loop with Harness-owned policy, budget, and trace."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Any

from forgeharness.context.assembly import ContextAssembler
from forgeharness.context.compiler import ContextBudgetError
from forgeharness.context.compression import ObservationCompressor
from forgeharness.domain.models import (
    CURRENT_RECOVERY_SEMANTICS_VERSION,
    FinalAction,
    FrozenModel,
    Message,
    MessageRole,
    PendingApproval,
    RunResult,
    RunStatus,
    ToolAction,
    ToolCall,
    Usage,
)
from forgeharness.models.base import Model, ModelRequest
from forgeharness.observability.trace import TraceRecorder
from forgeharness.runtime.budget import RunBudget
from forgeharness.runtime.retry import RetryDecision, backoff_ms, decide_retry
from forgeharness.runtime.verification import (
    ToolEvidence,
    VerificationRequest,
    VerificationResult,
    Verifier,
)
from forgeharness.state.approval import ApprovalGrant, ApprovalLedger
from forgeharness.state.checkpoint import CheckpointConflict, CheckpointStore
from forgeharness.state.invocation_journal import (
    AttemptOutcome,
    AttemptRecord,
    InvocationJournal,
    InvocationRecord,
    InvocationState,
    JournalError,
    RecoveryDecision,
    args_digest,
    decide_recovery,
    idempotency_key_for,
    settle_invocation,
)
from forgeharness.tools.base import (
    ExecutionAllowance,
    ToolContext,
    ToolEffectClass,
    ToolErrorCode,
    ToolFailure,
    ToolOutput,
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
        context_assembler: ContextAssembler | None = None,
        verifier: Verifier | None = None,
        invocation_journal: InvocationJournal | None = None,
        sleep_fn: Callable[[float], Awaitable[None]] | None = None,
        clock_fn: Callable[[], float] | None = None,
    ) -> None:
        self._model = model
        self._registry = registry
        self._dispatcher = dispatcher
        self._policy = policy
        self._trace = trace
        self._budget = budget or RunBudget()
        self._approval_ledger = approval_ledger
        self._checkpoint_store = checkpoint_store
        self._observation_compressor = observation_compressor or ObservationCompressor()
        self._context_assembler = context_assembler or ContextAssembler(
            max_tokens=self._budget.max_context_tokens
        )
        self._verifier = verifier
        # Optional for the same reason the checkpoint store is: a runtime without
        # durable state still runs, it just cannot promise side-effect recovery.
        self._invocation_journal = invocation_journal
        # Injected so tests exercise backoff and deadline paths without real waiting.
        self._sleep_fn = sleep_fn
        self._clock = clock_fn or monotonic

    async def run(
        self,
        *,
        task_id: str,
        task: str,
        workspace: Path,
        instructions: tuple[Message, ...] = (),
        initial_usage: Usage | None = None,
    ) -> RunResult:
        """Run until a final answer, approval suspension, failure, or budget exhaustion."""
        if any(message.role != MessageRole.SYSTEM for message in instructions):
            raise ValueError("runtime instructions must be system messages")
        messages = [*instructions, Message(role=MessageRole.USER, content=task)]
        usage = initial_usage or Usage()
        self._trace.append("run.started", {"workspace": str(workspace)})
        initial = self._checkpoint(
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
        self, *, previous: RunResult, grant: ApprovalGrant, workspace: Path
    ) -> RunResult:
        """Consume an exact approval, execute the suspended call, and continue the run."""
        if previous.status != RunStatus.AWAITING_APPROVAL or previous.pending_approval is None:
            raise ValueError("only an awaiting-approval run can be resumed")
        if self._approval_ledger is None:
            raise RuntimeError("runtime has no approval ledger")
        self._assert_current(previous)
        call = previous.pending_approval.call
        tool = self._registry.get(call.name)
        if tool is None:
            raise ValueError(f"approved tool is no longer registered: {call.name}")
        current_decision = self._policy.evaluate(
            task_id=previous.task_id, spec=tool.spec, call=call
        )
        if current_decision.type == PolicyDecisionType.DENY:
            raise ValueError(f"approved tool is now denied: {current_decision.reason}")
        self._approval_ledger.consume(grant, task_id=previous.task_id, call=call)
        # Claim the checkpoint before any asynchronous side effect. SQLite's
        # compare-and-swap rejects competing processes as well as duplicate requests.
        claimed = self._checkpoint(
            previous.model_copy(update={"status": RunStatus.RUNNING, "pending_approval": None})
        )
        self._trace.append("approval.consumed", {"tool": call.name, "granted_by": grant.granted_by})
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
        )
        observation = self._observation(dispatch.output.content)
        messages.append(
            Message(
                role=MessageRole.TOOL,
                content=observation,
                tool_call_id=call.id,
                tool_name=call.name,
            )
        )
        self._trace.append(
            "tool.completed",
            self._tool_event(call, dispatch, observation, usage),
        )
        running = self._checkpoint(
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
            tool_evidence=(evidence,),
        )

    async def resume_running(self, *, previous: RunResult, workspace: Path) -> RunResult:
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
        * ``claimed``, replay-safe ``started``, or no journal row — automatic
          replay is not implemented; the run fails closed with
          ``recovery_unsupported`` instead of advancing the model past an
          unresolved tool call.

        A run without an invocation journal cannot make these guarantees; an
        unresolved call therefore fails closed.
        """
        if previous.status is not RunStatus.RUNNING:
            raise ValueError("only a running run can be resumed this way")
        self._assert_current(previous)
        messages = list(previous.messages)
        usage = previous.usage
        checkpoint_revision = previous.checkpoint_revision
        evidence: list[ToolEvidence] = []
        outstanding = _outstanding_call(messages)
        if outstanding is not None and previous.recovery_semantics_version < (
            CURRENT_RECOVERY_SEMANTICS_VERSION
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
            return self._finish(
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
            outcome = self.recover_call(task_id=previous.task_id, logical_call_id=outstanding.id)
            if outcome is not None and outcome.decision is RecoveryDecision.REUSE_RESULT:
                stored = outcome.record.result
                if stored is None:
                    raise JournalError(
                        f"invocation {outcome.record.invocation_id} is completed but has no result"
                    )
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
                    _evidence(outstanding.name, stored.ok, stored.content, stored.metadata)
                )
                self._trace.append(
                    "tool.completed",
                    self._tool_event(
                        outstanding, DispatchResult(output=stored, elapsed_ms=0), observation, usage
                    ),
                )
                checkpoint_revision = self._save_running(
                    previous.task_id, messages, usage, checkpoint_revision
                )
            elif outcome is not None and outcome.decision is RecoveryDecision.REFUSE_REPLAY:
                return self._finish(
                    task_id=previous.task_id,
                    status=RunStatus.FAILED,
                    messages=messages,
                    usage=usage,
                    checkpoint_revision=checkpoint_revision,
                    error=(
                        "indeterminate_side_effect: cannot confirm whether "
                        f"{outstanding.name} already ran, so it is not replayed"
                    ),
                )
            else:
                reason = "missing_journal_record" if outcome is None else outcome.decision.value
                self._trace.append(
                    "tool.recovery.unsupported",
                    {
                        "logical_call_id": outstanding.id,
                        "tool": outstanding.name,
                        "reason": reason,
                    },
                )
                return self._finish(
                    task_id=previous.task_id,
                    status=RunStatus.FAILED,
                    messages=messages,
                    usage=usage,
                    checkpoint_revision=checkpoint_revision,
                    error=(
                        "recovery_unsupported: automatic execution or replay is not "
                        f"implemented for {outstanding.name} ({reason})"
                    ),
                    error_type="recovery_unsupported",
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
        workspace: Path,
        checkpoint_revision: int,
        tool_evidence: tuple[ToolEvidence, ...] = (),
    ) -> RunResult:
        """Continue a new or resumed run from validated in-memory state."""

        evidence = list(tool_evidence)
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
                return self._finish(
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
                return self._finish(
                    task_id=task_id,
                    status=RunStatus.EXHAUSTED,
                    messages=messages,
                    usage=usage,
                    checkpoint_revision=checkpoint_revision,
                    error="token budget exhausted",
                )
            try:
                assembled = self._context_assembler.assemble(messages)
            except ContextBudgetError as exc:
                self._trace.append("context.overflow", {"error": str(exc)})
                return self._finish(
                    task_id=task_id,
                    status=RunStatus.FAILED,
                    messages=messages,
                    usage=usage,
                    checkpoint_revision=checkpoint_revision,
                    error=f"context budget exceeded: {exc}",
                )
            if assembled.compacted:
                self._trace.append(
                    "context.compacted",
                    {
                        "sent_messages": len(assembled.messages),
                        "dropped_messages": assembled.dropped_messages,
                        "estimated_tokens": assembled.estimated_tokens,
                        "max_tokens": self._budget.max_context_tokens,
                        "decisions": [
                            decision.model_dump(mode="json")
                            for decision in assembled.decisions
                            if not decision.included
                        ],
                    },
                )
            try:
                request = ModelRequest(
                    task_id=task_id,
                    messages=assembled.messages,
                    tools=self._registry.specs(),
                )
                # The phase tag lets a projection pair each request with its own
                # action and attribute latency without guessing from event order.
                self._trace.append(
                    "model.request", {"phase": "executor", **request.model_dump(mode="json")}
                )
                model_result = await self._model.decide(request)
            except Exception as exc:
                # A provider may raise a typed protocol error carrying a stable
                # code; recording it keeps the reason machine-readable instead of
                # leaving only a class name and a prose message.
                recoverable = getattr(exc, "recoverable", False) is True
                self._trace.append(
                    "model.failed",
                    {
                        "error_type": type(exc).__name__,
                        "error_code": getattr(exc, "code", None),
                        "received_tool_calls": getattr(exc, "received_tool_calls", None),
                        "recoverable": getattr(exc, "recoverable", None),
                        # A rejected request still consumed tokens; recording them
                        # keeps cost accounting honest and lets a replay rebuild
                        # the same exception without contacting a provider.
                        "usage": _usage_payload(getattr(exc, "usage", None)),
                        "error": str(exc),
                    },
                )
                if not recoverable:
                    return self._finish(
                        task_id=task_id,
                        status=RunStatus.FAILED,
                        messages=messages,
                        usage=usage,
                        checkpoint_revision=checkpoint_revision,
                        error=f"model failed: {type(exc).__name__}: {exc}",
                    )
                # The response arrived intact but broke the single-action protocol,
                # so the model can plausibly fix it. The inference really happened,
                # so the step and its tokens are charged; nothing is admitted or
                # executed, and the harness never picks an action on the model's
                # behalf.
                usage = Usage(
                    steps=usage.steps + 1,
                    tool_calls=usage.tool_calls,
                    input_tokens=usage.input_tokens + _rejected_input_tokens(exc),
                    output_tokens=usage.output_tokens + _rejected_output_tokens(exc),
                )
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
                    return self._finish(
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
                checkpoint_revision = self._save_running(
                    task_id, messages, usage, checkpoint_revision
                )
                continue

            usage = Usage(
                steps=usage.steps + 1,
                tool_calls=usage.tool_calls,
                input_tokens=usage.input_tokens + model_result.usage.input_tokens,
                output_tokens=usage.output_tokens + model_result.usage.output_tokens,
            )
            self._trace.append(
                "model.action",
                {
                    "kind": model_result.action.kind,
                    "action": model_result.action.model_dump(mode="json"),
                    "model_name": model_result.model_name,
                    "input_tokens": model_result.usage.input_tokens,
                    "output_tokens": model_result.usage.output_tokens,
                    "budget": self._budget_remaining(usage),
                },
            )

            if self._tokens_exhausted(usage):
                return self._finish(
                    task_id=task_id,
                    status=RunStatus.EXHAUSTED,
                    messages=messages,
                    usage=usage,
                    checkpoint_revision=checkpoint_revision,
                    error="token budget exhausted",
                )

            action = model_result.action
            if isinstance(action, FinalAction):
                messages.append(Message(role=MessageRole.ASSISTANT, content=action.content))
                if self._verifier is None:
                    return self._finish(
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
                        )
                    )
                except Exception as exc:
                    self._trace.append(
                        "verification.failed",
                        {"error_type": type(exc).__name__, "error": str(exc)},
                    )
                    return self._finish(
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
                    return self._finish(
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
                checkpoint_revision = self._save_running(
                    task_id, messages, usage, checkpoint_revision
                )
                continue

            if not isinstance(action, ToolAction):
                raise AssertionError(f"unhandled action type: {type(action).__name__}")

            if usage.tool_calls >= self._budget.max_tool_calls:
                return self._finish(
                    task_id=task_id,
                    status=RunStatus.EXHAUSTED,
                    messages=messages,
                    usage=usage,
                    checkpoint_revision=checkpoint_revision,
                    error="tool-call budget exhausted",
                )

            messages.append(Message(role=MessageRole.ASSISTANT, tool_calls=(action.call,)))
            tool = self._registry.get(action.call.name)
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
                checkpoint_revision = self._save_running(
                    task_id, messages, usage, checkpoint_revision
                )
                continue

            decision = self._policy.evaluate(task_id=task_id, spec=tool.spec, call=action.call)
            self._trace.append(
                "policy.decided",
                {
                    "call_id": action.call.id,
                    "tool": action.call.name,
                    "decision": decision.type.value,
                    "reason": decision.reason,
                },
            )
            if decision.type == PolicyDecisionType.REQUIRE_APPROVAL:
                self._trace.append(
                    "run.awaiting_approval",
                    {"call_id": action.call.id, "tool": action.call.name},
                )
                return self._checkpoint(
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
                checkpoint_revision = self._save_running(
                    task_id, messages, usage, checkpoint_revision
                )
                continue

            # Persist the admitted call before any side effect. If this task is
            # cancelled inside the tool, recovery can pair the checkpointed call
            # with its STARTED journal row and refuse an unsafe replay.
            checkpoint_revision = self._save_running(task_id, messages, usage, checkpoint_revision)
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
                )
            )
            observation = self._observation(dispatch.output.content)
            messages.append(self._tool_message(action, observation))
            self._trace.append(
                "tool.completed",
                self._tool_event(action.call, dispatch, observation, usage),
            )
            checkpoint_revision = self._save_running(task_id, messages, usage, checkpoint_revision)

        return self._finish(
            task_id=task_id,
            status=RunStatus.EXHAUSTED,
            messages=messages,
            usage=usage,
            checkpoint_revision=checkpoint_revision,
            error="step budget exhausted",
        )

    def _tokens_exhausted(self, usage: Usage) -> bool:
        return (
            usage.input_tokens >= self._budget.max_input_tokens
            or usage.output_tokens >= self._budget.max_output_tokens
        )

    def _tool_context(
        self,
        task_id: str,
        workspace: Path,
        usage: Usage,
        *,
        logical_call_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> ToolContext:
        return ToolContext(
            task_id=task_id,
            workspace=workspace,
            logical_call_id=logical_call_id,
            idempotency_key=idempotency_key,
            allowance=ExecutionAllowance(
                remaining_steps=max(0, self._budget.max_steps - usage.steps),
                remaining_tool_calls=max(0, self._budget.max_tool_calls - usage.tool_calls - 1),
                remaining_input_tokens=max(0, self._budget.max_input_tokens - usage.input_tokens),
                remaining_output_tokens=max(
                    0, self._budget.max_output_tokens - usage.output_tokens
                ),
            ),
        )

    async def _dispatch_journalled(
        self,
        call: ToolCall,
        workspace: Path,
        usage: Usage,
        *,
        task_id: str,
        checkpoint_revision: int,
    ) -> DispatchResult:
        """Dispatch one call, bookending it with durable journal writes.

        The ordering is the whole point: `started` is committed before the tool is
        awaited, so a crash inside the tool leaves evidence that it may have run. A
        runtime without a journal dispatches directly and makes no such promise.
        """
        if self._invocation_journal is None:
            return await self._dispatcher.dispatch(
                call, self._tool_context(task_id, workspace, usage)
            )
        tool = self._registry.get(call.name)
        effect_class = (
            tool.spec.effect_class if tool is not None else ToolEffectClass.NON_IDEMPOTENT
        )
        now = datetime.now(UTC)
        record = InvocationRecord(
            invocation_id=f"{task_id}:{call.id}",
            run_id=task_id,
            logical_call_id=call.id,
            tool_name=call.name,
            effect_class=effect_class,
            args_digest=args_digest(call.arguments),
            idempotency_key=idempotency_key_for(run_id=task_id, logical_call_id=call.id),
            state=InvocationState.CLAIMED,
            claimed_at=now,
            checkpoint_revision=checkpoint_revision,
        )
        self._invocation_journal.claim(record)
        self._trace.append(
            "tool.invocation.claimed",
            {
                "invocation_id": record.invocation_id,
                "logical_call_id": call.id,
                "tool": call.name,
                "effect_class": effect_class.value,
                "attempt_no": 0,
            },
        )
        started = self._invocation_journal.mark_started(
            record.invocation_id, checkpoint_revision=checkpoint_revision
        )
        self._trace.append(
            "tool.invocation.started",
            {
                "invocation_id": record.invocation_id,
                "logical_call_id": call.id,
                "tool": call.name,
                "effect_class": effect_class.value,
                "attempt_no": started.attempt_count,
            },
        )
        return await self._run_attempts(
            call=call,
            record=record,
            started=started,
            workspace=workspace,
            usage=usage,
            task_id=task_id,
            effect_class=effect_class,
        )

    async def _run_attempts(
        self,
        *,
        call: ToolCall,
        record: InvocationRecord,
        started: InvocationRecord,
        workspace: Path,
        usage: Usage,
        task_id: str,
        effect_class: ToolEffectClass,
    ) -> DispatchResult:
        """Execute one logical call, retrying only where retry is provably safe.

        An attempt's failure does not settle the invocation: only the *final*
        attempt does. That keeps `failed` meaning "this call did not happen" rather
        than "one attempt did not happen", which is what recovery relies on.
        """
        journal = self._invocation_journal
        assert journal is not None  # the caller only reaches here with a journal
        context = self._tool_context(
            task_id,
            workspace,
            usage,
            logical_call_id=call.id,
            idempotency_key=record.idempotency_key,
        )
        max_attempts = self._budget.max_attempts_per_call
        attempt_no = 0
        while True:
            attempt_no += 1
            delay = (
                backoff_ms(
                    attempt_no=attempt_no - 1,
                    initial_ms=self._budget.initial_backoff_ms,
                    multiplier=self._budget.backoff_multiplier,
                    max_ms=self._budget.max_backoff_ms,
                )
                if attempt_no > 1
                else 0
            )
            if delay:
                self._trace.append(
                    "tool.retry.backoff",
                    {
                        "invocation_id": record.invocation_id,
                        "attempt_no": attempt_no,
                        "delay_ms": delay,
                    },
                )
                await self._sleep(delay / 1000)
            attempt = AttemptRecord(
                attempt_id=f"{record.invocation_id}#{attempt_no}",
                invocation_id=record.invocation_id,
                attempt_no=attempt_no,
                started_at=datetime.now(UTC),
                timeout_seconds=self._effective_tool_timeout(call),
                backoff_before_ms=delay,
            )
            journal.start_attempt(attempt)
            self._trace.append(
                "tool.attempt.started",
                {
                    "invocation_id": record.invocation_id,
                    "logical_call_id": call.id,
                    "tool": call.name,
                    "effect_class": effect_class.value,
                    "attempt_no": attempt_no,
                    "timeout_seconds": attempt.timeout_seconds,
                },
            )
            dispatch = await self._dispatcher.dispatch(call, context)
            finished = attempt.model_copy(
                update={
                    "finished_at": datetime.now(UTC),
                    "outcome": _attempt_outcome(dispatch.output),
                    "error_code": (
                        dispatch.output.error.code.value
                        if dispatch.output.error is not None
                        else None
                    ),
                }
            )
            journal.finish_attempt(finished)
            if dispatch.output.ok:
                self._trace.append(
                    "tool.attempt.completed",
                    {
                        "invocation_id": record.invocation_id,
                        "logical_call_id": call.id,
                        "tool": call.name,
                        "attempt_no": attempt_no,
                    },
                )
                self._settle_invocation(started, dispatch.output, call, effect_class, attempt_no)
                return dispatch

            self._trace.append(
                "tool.attempt.failed",
                {
                    "invocation_id": record.invocation_id,
                    "logical_call_id": call.id,
                    "tool": call.name,
                    "attempt_no": attempt_no,
                    "error_code": finished.error_code,
                },
            )
            decision = decide_retry(
                effect_class=effect_class,
                error_code=_error_code_of(dispatch.output),
                attempt_no=attempt_no,
                max_attempts=max_attempts,
            )
            self._trace.append(
                "tool.retry.decided",
                {
                    "invocation_id": record.invocation_id,
                    "logical_call_id": call.id,
                    "tool": call.name,
                    "attempt_no": attempt_no,
                    "error_code": finished.error_code,
                    "effect_class": effect_class.value,
                    "decision": decision.value,
                    "max_attempts": max_attempts,
                },
            )
            if decision is RetryDecision.RETRY:
                continue
            self._settle_invocation(
                started, dispatch.output, call, effect_class, attempt_no, decision=decision
            )
            return dispatch

    def _settle_invocation(
        self,
        started: InvocationRecord,
        output: ToolOutput,
        call: ToolCall,
        effect_class: ToolEffectClass,
        attempt_no: int,
        *,
        decision: RetryDecision | None = None,
    ) -> None:
        """Persist the invocation's terminal state and trace it.

        The state comes from the retry decision when one was made, so the journal
        cannot disagree with what the runtime decided.
        """
        journal = self._invocation_journal
        assert journal is not None
        failed_state = (
            decision.terminal_state
            if decision is not None and decision.terminal_state is not None
            else InvocationState.FAILED
        )
        settled = settle_invocation(
            started, output, datetime.now(UTC), failed_state=failed_state
        ).model_copy(update={"attempt_count": attempt_no})
        journal.settle(settled)
        event = {
            InvocationState.COMPLETED: "tool.invocation.completed",
            InvocationState.FAILED: "tool.invocation.failed",
            InvocationState.INDETERMINATE: "tool.invocation.indeterminate",
        }[settled.state]
        self._trace.append(
            event,
            {
                "invocation_id": started.invocation_id,
                "logical_call_id": call.id,
                "tool": call.name,
                "effect_class": effect_class.value,
                "attempt_no": attempt_no,
                "error_code": settled.error_code,
            },
        )

    def _effective_tool_timeout(self, call: ToolCall) -> float:
        """The ceiling for this call, from the tool's own declaration."""
        tool = self._registry.get(call.name)
        return tool.spec.timeout_seconds if tool is not None else self._budget.tool_timeout_seconds

    async def _sleep(self, seconds: float) -> None:
        """Wait between attempts. Overridable so tests do not sleep in real time."""
        if self._sleep_fn is not None:
            await self._sleep_fn(seconds)
            return
        await asyncio.sleep(seconds)

    def recover_call(self, *, task_id: str, logical_call_id: str) -> RecoveryOutcome | None:
        """Look up one logical call and decide what recovery should do with it.

        This is what stops a resumed run from repeating a completed side effect: if
        the journal already holds a terminal state for this call, the decision is
        made from durable state rather than by dispatching again. Returns ``None``
        when the call never entered execution, in which case the caller proceeds
        normally.
        """
        if self._invocation_journal is None:
            return None
        record = self._invocation_journal.load(run_id=task_id, logical_call_id=logical_call_id)
        if record is None:
            return None
        decision = decide_recovery(record)
        state = (
            InvocationState.INDETERMINATE
            if decision is RecoveryDecision.REFUSE_REPLAY
            else record.state
        )
        self._invocation_journal.record_recovery_decision(
            record.invocation_id, state=state, decision=decision
        )
        resolved = record.model_copy(update={"state": state, "recovery_decision": decision})
        self._trace.append(
            "tool.recovery.decided",
            {
                "invocation_id": record.invocation_id,
                "logical_call_id": record.logical_call_id,
                "tool": record.tool_name,
                "effect_class": record.effect_class.value,
                "attempt_no": record.attempt_count,
                "journal_state": record.state.value,
                "decision": decision.value,
            },
        )
        if state is InvocationState.INDETERMINATE:
            self._trace.append(
                "tool.invocation.indeterminate",
                {
                    "invocation_id": record.invocation_id,
                    "logical_call_id": record.logical_call_id,
                    "tool": record.tool_name,
                    "effect_class": record.effect_class.value,
                    "attempt_no": record.attempt_count,
                    "error_code": "indeterminate_side_effect",
                },
            )
        return RecoveryOutcome(record=resolved, decision=decision)

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

    def _finish(
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
        return self._checkpoint(
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

    def _save_running(
        self, task_id: str, messages: list[Message], usage: Usage, revision: int
    ) -> int:
        saved = self._checkpoint(
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
        nested = child or Usage()
        return Usage(
            steps=usage.steps + nested.steps,
            tool_calls=usage.tool_calls + 1 + nested.tool_calls,
            input_tokens=usage.input_tokens + nested.input_tokens,
            output_tokens=usage.output_tokens + nested.output_tokens,
        )

    def _checkpoint(self, result: RunResult) -> RunResult:
        if self._checkpoint_store is None:
            return result
        # Stamp the semantics this run is written under at the single funnel every
        # persisted snapshot passes through. A run without a journal records 0, so
        # recovery can tell that no durable side-effect record exists for it.
        result = result.model_copy(
            update={
                "recovery_semantics_version": (
                    CURRENT_RECOVERY_SEMANTICS_VERSION
                    if self._invocation_journal is not None
                    else 0
                )
            }
        )
        saved = self._checkpoint_store.save(result)
        self._trace.append(
            "checkpoint.saved", {"revision": saved.checkpoint_revision, "status": saved.status}
        )
        return saved

    def _assert_current(self, result: RunResult) -> None:
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


def _error_code_of(output: ToolOutput) -> ToolErrorCode:
    """The classified code for a failed output, defaulting to unclassified."""
    return output.error.code if output.error is not None else ToolErrorCode.TOOL_EXECUTION_ERROR


def _attempt_outcome(output: ToolOutput) -> AttemptOutcome:
    """Whether one physical execution completed, failed, or timed out."""
    if output.ok:
        return AttemptOutcome.COMPLETED
    if _error_code_of(output) is ToolErrorCode.TIMEOUT:
        return AttemptOutcome.TIMED_OUT
    return AttemptOutcome.FAILED


class RecoveryOutcome(FrozenModel):
    """What recovery decided about one unfinished invocation, and why."""

    record: InvocationRecord
    decision: RecoveryDecision

    @property
    def blocks_resume(self) -> bool:
        """True when the run must not continue automatically."""
        return self.decision is RecoveryDecision.REFUSE_REPLAY


def _usage_payload(usage: object) -> dict[str, int] | None:
    """Serialize a rejected request's token usage for the trace, if it was reported."""
    if usage is None:
        return None
    input_tokens = getattr(usage, "input_tokens", None)
    output_tokens = getattr(usage, "output_tokens", None)
    if input_tokens is None and output_tokens is None:
        return None
    return {"input_tokens": int(input_tokens or 0), "output_tokens": int(output_tokens or 0)}


def _rejected_input_tokens(exc: Exception) -> int:
    """Input tokens the rejected request consumed, when the provider reported them."""
    usage = getattr(exc, "usage", None)
    value = getattr(usage, "input_tokens", 0) if usage is not None else 0
    return int(value or 0)


def _rejected_output_tokens(exc: Exception) -> int:
    """Output tokens the rejected request produced, when the provider reported them."""
    usage = getattr(exc, "usage", None)
    value = getattr(usage, "output_tokens", 0) if usage is not None else 0
    return int(value or 0)


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
