"""Shared tool invocation, policy, journal, retry and allowance mechanics (ADR 030)."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from forgeharness.domain.models import (
    FrozenModel,
    RunContext,
    ToolCall,
    Usage,
)
from forgeharness.runtime.budget import RunBudget
from forgeharness.runtime.retry import RetryDecision, backoff_ms, decide_retry
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
    RiskLevel,
    ToolContext,
    ToolEffectClass,
    ToolErrorCode,
    ToolOutput,
)
from forgeharness.tools.dispatcher import DispatchResult, ToolDispatcher
from forgeharness.tools.policy import PolicyDecision, ToolPolicy
from forgeharness.tools.registry import ToolRegistry


class TraceSink(Protocol):
    def append(self, event_type: str, payload: dict[str, Any] | None = None) -> Any: ...


class ToolExecutionDenied(RuntimeError):
    """The execution mode cannot safely handle this declared effect."""


class ToolExecutor:
    def __init__(
        self,
        *,
        registry: ToolRegistry,
        dispatcher: ToolDispatcher,
        policy: ToolPolicy,
        trace: TraceSink,
        budget: RunBudget,
        context: Callable[[], RunContext | None],
        journal: InvocationJournal | None = None,
        sleep_fn: Callable[[float], Awaitable[None]] | None = None,
        before_attempt: Callable[[], None] | None = None,
        attempt_usage: Callable[[], Usage] | None = None,
        step_limit: int | None = None,
    ) -> None:
        self._registry, self._dispatcher, self._policy = registry, dispatcher, policy
        self._trace, self._budget, self._get_context = trace, budget, context
        self._invocation_journal, self._sleep_fn = journal, sleep_fn
        self._before_attempt, self._attempt_usage = before_attempt, attempt_usage
        if step_limit is not None and step_limit < 0:
            raise ValueError("tool step limit must be nonnegative")
        # A node may grant zero nested steps while RunBudget retains its public
        # positive run-step invariant. Do not round that allowance up to one.
        self._step_limit = budget.max_steps if step_limit is None else step_limit

    def authorize(
        self, call: ToolCall, *, task_id: str, read_only: bool = False, emit: bool = True
    ) -> PolicyDecision:
        tool = self._registry.get(call.name)
        if tool is None:
            raise ToolExecutionDenied(f"unknown tool: {call.name}")
        if read_only and (
            tool.spec.risk is not RiskLevel.READ
            or tool.spec.effect_class is not ToolEffectClass.READ_ONLY
        ):
            raise ToolExecutionDenied(
                "custom orchestrators may only call read-only tools; "
                "write/approval tools require the ReAct path"
            )
        decision = self._policy.evaluate(task_id=task_id, spec=tool.spec, call=call)
        if emit:
            self._trace.append(
                "policy.decided",
                {
                    "call_id": call.id,
                    "tool": call.name,
                    "decision": decision.type.value,
                    "reason": decision.reason,
                },
            )
        return decision

    async def dispatch(
        self,
        call: ToolCall,
        workspace: Path | None,
        usage: Usage,
        *,
        task_id: str,
        checkpoint_revision: int = 0,
        idempotency_key: str | None = None,
    ) -> DispatchResult:
        """Execute an authorized call after its caller persists admission."""
        return await self._dispatch_journalled(
            call,
            workspace,
            usage,
            task_id=task_id,
            checkpoint_revision=checkpoint_revision,
            idempotency_key=idempotency_key,
        )

    @staticmethod
    def validate_idempotency_key(value: str) -> str:
        """Validate a hosted key before it can enter a physical tool context."""
        if type(value) is not str or re.fullmatch(r"[A-Za-z0-9_.:-]{1,256}", value) is None:
            raise ValueError("tool idempotency key needs 1-256 identifier characters")
        return value

    @staticmethod
    def account_usage(usage: Usage, child: Usage | None) -> Usage:
        nested = child or Usage()
        return Usage(
            steps=usage.steps + nested.steps,
            tool_calls=usage.tool_calls + 1 + nested.tool_calls,
            input_tokens=usage.input_tokens + nested.input_tokens,
            output_tokens=usage.output_tokens + nested.output_tokens,
        )

    def _tool_context(
        self,
        task_id: str,
        workspace: Path | None,
        usage: Usage,
        *,
        logical_call_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> ToolContext:
        return ToolContext(
            task_id=task_id,
            workspace=workspace,
            run_context=self._get_context(),
            logical_call_id=logical_call_id,
            idempotency_key=idempotency_key,
            allowance=ExecutionAllowance(
                remaining_steps=max(0, self._step_limit - usage.steps),
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
        workspace: Path | None,
        usage: Usage,
        *,
        task_id: str,
        checkpoint_revision: int,
        idempotency_key: str | None = None,
    ) -> DispatchResult:
        """Dispatch one call, bookending it with durable journal writes.

        The ordering is the whole point: `started` is committed before the tool is
        awaited, so a crash inside the tool leaves evidence that it may have run. A
        runtime without a journal dispatches directly and makes no such promise.
        """
        if idempotency_key is not None:
            self.validate_idempotency_key(idempotency_key)
            if self._invocation_journal is not None:
                raise ValueError("a hosted idempotency key cannot share authority with a journal")
        if self._invocation_journal is None:
            return await self._dispatcher.dispatch(
                call,
                self._tool_context(
                    task_id,
                    workspace,
                    usage,
                    logical_call_id=call.id,
                    idempotency_key=(
                        idempotency_key
                        if idempotency_key is not None
                        else idempotency_key_for(run_id=task_id, logical_call_id=call.id)
                    ),
                ),
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
        workspace: Path | None,
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
        existing = journal.attempts_for(record.invocation_id)
        attempt_no = max((item.attempt_no for item in existing), default=0)
        # A crash cannot reset the physical attempt budget.
        if attempt_no >= max_attempts:
            raise JournalError("physical attempt budget exhausted during recovery")
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
            if self._before_attempt is not None:
                self._before_attempt()
            if self._attempt_usage is not None:
                context = self._tool_context(
                    task_id,
                    workspace,
                    self._attempt_usage(),
                    logical_call_id=call.id,
                    idempotency_key=record.idempotency_key,
                )
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
            dispatch = await self._dispatcher.dispatch(
                call, context.model_copy(update={"attempt_id": attempt.attempt_id})
            )
            finished = attempt.model_copy(
                update={
                    "finished_at": datetime.now(UTC),
                    "outcome": _attempt_outcome(dispatch.output),
                    "error_code": (
                        dispatch.output.error.code.value
                        if dispatch.output.error is not None
                        else None
                    ),
                    "usage": (
                        self.account_usage(Usage(), dispatch.output.usage)
                        if dispatch.output.ok or dispatch.output.usage is not None
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
