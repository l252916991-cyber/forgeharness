"""Retry decision matrix and deterministic backoff (M10-B).

The full grid is asserted cell by cell because this function is the single place
that decides whether a failed side effect may be repeated. Everything else in the
retry path is plumbing around it.
"""

from __future__ import annotations

import pytest

from forgeharness.runtime.retry import RetryDecision, backoff_ms, decide_retry
from forgeharness.state.invocation_journal import InvocationState
from forgeharness.tools.base import ToolEffectClass, ToolErrorCode

READ = ToolEffectClass.READ_ONLY
IDEMPOTENT = ToolEffectClass.IDEMPOTENT
NON_IDEMPOTENT = ToolEffectClass.NON_IDEMPOTENT

# (effect_class, error_code, attempt_no, max_attempts) -> expected decision.
# attempt_no < max_attempts means budget remains.
MATRIX: list[tuple[ToolEffectClass, ToolErrorCode, int, int, RetryDecision]] = [
    # timeout, budget remaining
    (READ, ToolErrorCode.TIMEOUT, 1, 3, RetryDecision.RETRY),
    (IDEMPOTENT, ToolErrorCode.TIMEOUT, 1, 3, RetryDecision.RETRY),
    # the headline safety cell: a side effect may already have happened
    (NON_IDEMPOTENT, ToolErrorCode.TIMEOUT, 1, 3, RetryDecision.INDETERMINATE),
    # timeout, budget exhausted
    (READ, ToolErrorCode.TIMEOUT, 3, 3, RetryDecision.FAIL),
    (IDEMPOTENT, ToolErrorCode.TIMEOUT, 3, 3, RetryDecision.FAIL),
    (NON_IDEMPOTENT, ToolErrorCode.TIMEOUT, 3, 3, RetryDecision.INDETERMINATE),
    # an unclassified execution failure is not known to be transient
    (READ, ToolErrorCode.TOOL_EXECUTION_ERROR, 1, 3, RetryDecision.FAIL),
    (IDEMPOTENT, ToolErrorCode.TOOL_EXECUTION_ERROR, 1, 3, RetryDecision.FAIL),
    (NON_IDEMPOTENT, ToolErrorCode.TOOL_EXECUTION_ERROR, 1, 3, RetryDecision.INDETERMINATE),
    # agent-recoverable but not runtime-retryable
    (READ, ToolErrorCode.PATH_NOT_FOUND, 1, 3, RetryDecision.FAIL),
    (IDEMPOTENT, ToolErrorCode.PATH_NOT_FOUND, 1, 3, RetryDecision.FAIL),
    (NON_IDEMPOTENT, ToolErrorCode.PATH_NOT_FOUND, 1, 3, RetryDecision.FAIL),
    (READ, ToolErrorCode.INVALID_ARGUMENTS, 1, 3, RetryDecision.FAIL),
    (IDEMPOTENT, ToolErrorCode.INVALID_ARGUMENTS, 1, 3, RetryDecision.FAIL),
    (NON_IDEMPOTENT, ToolErrorCode.INVALID_ARGUMENTS, 1, 3, RetryDecision.FAIL),
]


@pytest.mark.parametrize(
    ("effect_class", "error_code", "attempt_no", "max_attempts", "expected"), MATRIX
)
def test_retry_decision_matrix(
    effect_class: ToolEffectClass,
    error_code: ToolErrorCode,
    attempt_no: int,
    max_attempts: int,
    expected: RetryDecision,
) -> None:
    assert (
        decide_retry(
            effect_class=effect_class,
            error_code=error_code,
            attempt_no=attempt_no,
            max_attempts=max_attempts,
        )
        is expected
    )


def test_only_timeout_is_treated_as_transient() -> None:
    """Retry requires positively knowing the failure is transient.

    An unclassified failure is not evidence of transience, so it is not retried
    even for a read-only tool.
    """
    for effect_class in (READ, IDEMPOTENT):
        for error_code in (
            ToolErrorCode.TOOL_EXECUTION_ERROR,
            ToolErrorCode.PATH_NOT_FOUND,
            ToolErrorCode.INVALID_ARGUMENTS,
        ):
            assert (
                decide_retry(
                    effect_class=effect_class,
                    error_code=error_code,
                    attempt_no=1,
                    max_attempts=5,
                )
                is RetryDecision.FAIL
            )


def test_a_non_idempotent_tool_is_never_retried_after_a_transient_failure() -> None:
    """The safety invariant of M10-B, asserted directly across the whole budget."""
    for attempt in range(1, 6):
        assert (
            decide_retry(
                effect_class=NON_IDEMPOTENT,
                error_code=ToolErrorCode.TIMEOUT,
                attempt_no=attempt,
                max_attempts=5,
            )
            is RetryDecision.INDETERMINATE
        )


def test_retry_never_exceeds_the_attempt_budget() -> None:
    """`max_attempts` is a hard ceiling: the last allowed attempt cannot retry."""
    assert (
        decide_retry(
            effect_class=READ, error_code=ToolErrorCode.TIMEOUT, attempt_no=1, max_attempts=2
        )
        is RetryDecision.RETRY
    )
    assert (
        decide_retry(
            effect_class=READ, error_code=ToolErrorCode.TIMEOUT, attempt_no=2, max_attempts=2
        )
        is RetryDecision.FAIL
    )


def test_single_attempt_budget_never_retries() -> None:
    assert (
        decide_retry(
            effect_class=READ, error_code=ToolErrorCode.TIMEOUT, attempt_no=1, max_attempts=1
        )
        is RetryDecision.FAIL
    )


def test_decision_terminal_states() -> None:
    """Only RETRY continues; the other two map to exactly one terminal state."""
    assert RetryDecision.RETRY.terminal_state is None
    assert RetryDecision.FAIL.terminal_state is InvocationState.FAILED
    assert RetryDecision.INDETERMINATE.terminal_state is InvocationState.INDETERMINATE


def test_backoff_is_deterministic_and_capped() -> None:
    """Doubling from a fixed base, capped: reproducible for latency evidence."""
    schedule = [
        backoff_ms(attempt_no=n, initial_ms=100, multiplier=2.0, max_ms=1000) for n in range(1, 6)
    ]
    assert schedule == [100, 200, 400, 800, 1000]
    # Repeated calls agree: no jitter, no clock.
    assert backoff_ms(attempt_no=2, initial_ms=100, multiplier=2.0, max_ms=1000) == 200


def test_backoff_rejects_an_invalid_attempt_number() -> None:
    with pytest.raises(ValueError, match="attempt_no"):
        backoff_ms(attempt_no=0, initial_ms=100, multiplier=2.0, max_ms=1000)


def test_backoff_handles_a_multiplier_of_one() -> None:
    """A constant delay is a legal policy, not an infinite loop."""
    assert backoff_ms(attempt_no=5, initial_ms=250, multiplier=1.0, max_ms=1000) == 250
