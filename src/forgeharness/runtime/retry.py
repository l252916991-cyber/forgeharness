"""Whether the runtime may retry one failed tool call. Pure decision logic.

The rule that matters: **"the agent could recover" is not "the runtime may retry".**
A `path_not_found` failure is recoverable *by the agent* — the model can pick a
different path when it reads the observation — but re-running the identical call
would just fail again. So retryability is decided from the error code, the tool's
effect class and the attempt budget, never from the agent-facing `recoverable` flag.
Keeping those two meanings apart is why `decide_retry` does not accept
`recoverable` at all.

The second rule: for a `non_idempotent` tool whose body has already run, an
uncertain outcome is never retried. A timeout after the write began cannot be
resolved by trying again, so it becomes `indeterminate` — the run stops instead of
maybe writing twice.
"""

from __future__ import annotations

from enum import StrEnum

from forgeharness.state.invocation_journal import InvocationState
from forgeharness.tools.base import ToolEffectClass, ToolErrorCode


class RetryDecision(StrEnum):
    """What the runtime does after one failed attempt."""

    RETRY = "retry"
    FAIL = "fail"
    INDETERMINATE = "indeterminate"

    @property
    def terminal_state(self) -> InvocationState | None:
        """The invocation state this decision settles to, or None to keep going."""
        if self is RetryDecision.RETRY:
            return None
        if self is RetryDecision.FAIL:
            return InvocationState.FAILED
        return InvocationState.INDETERMINATE


# Errors worth another attempt when the effect is safe to repeat. Deliberately
# short: only a failure we positively know is transient qualifies. An unclassified
# `tool_execution_error` is not known to be transient, so it does not.
_TRANSIENT_CODES = frozenset({ToolErrorCode.TIMEOUT})


def decide_retry(
    *,
    effect_class: ToolEffectClass,
    error_code: ToolErrorCode,
    attempt_no: int,
    max_attempts: int,
) -> RetryDecision:
    """Decide whether one failed attempt may be repeated.

    ``attempt_no`` is the attempt that just failed (1-based); ``max_attempts`` is the
    total allowance for this logical call, so a retry is allowed while
    ``attempt_no < max_attempts``.
    """
    # A side-effecting tool whose body may have started: the failure leaves an
    # unknown extent, so no further attempt is permitted whatever the budget says.
    # Both codes below come from the execution path (a rejection raised before
    # dispatch is reported as `invalid_arguments` instead).
    outcome_unknown = effect_class is ToolEffectClass.NON_IDEMPOTENT and error_code in (
        _TRANSIENT_CODES | {ToolErrorCode.TOOL_EXECUTION_ERROR}
    )
    if outcome_unknown:
        return RetryDecision.INDETERMINATE
    if error_code not in _TRANSIENT_CODES:
        # A defect no identical attempt can fix: bad arguments, a missing path, or a
        # failure the harness could not classify.
        return RetryDecision.FAIL
    if attempt_no >= max_attempts:
        return RetryDecision.FAIL
    return RetryDecision.RETRY


def backoff_ms(
    *,
    attempt_no: int,
    initial_ms: int,
    multiplier: float,
    max_ms: int,
) -> int:
    """Deterministic delay before the next attempt.

    No jitter on purpose: the benchmark, replay and latency evidence all need
    reproducible timing, and a single local client has no thundering herd to spread.
    """
    if attempt_no < 1:
        raise ValueError("attempt_no must be at least 1")
    delay = initial_ms * (multiplier ** (attempt_no - 1))
    return int(min(delay, max_ms))
