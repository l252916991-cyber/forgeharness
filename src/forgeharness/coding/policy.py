"""Policy for the first software-engineering vertical."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from time import monotonic

from forgeharness.sdk import (
    DEFAULT_DENIED_COMMANDS,
    DEFAULT_SECRET_PATH_RULES,
    CommandRule,
    ParameterAwarePolicy,
    PathRule,
    PolicyDecision,
    PolicyDecisionType,
    RateLimit,
    RiskLevel,
    SlidingWindowLimiter,
    ToolCall,
    ToolSpec,
)


class _CodingRiskPolicy:
    """The risk-level baseline: inspect freely, edit with approval, run only fixed tests.

    This is the shape the vertical shipped with and it stays the fallback, so a call
    with no recognizable target is authorized exactly as it was before path and
    command rules existed.
    """

    def evaluate(self, *, task_id: str, spec: ToolSpec, call: ToolCall) -> PolicyDecision:
        """Authorize only capabilities whose side effects are constrained by their tool."""
        del task_id, call
        if spec.risk == RiskLevel.READ:
            return PolicyDecision(type=PolicyDecisionType.ALLOW, reason="read-only coding tool")
        if spec.risk == RiskLevel.WRITE:
            return PolicyDecision(
                type=PolicyDecisionType.REQUIRE_APPROVAL,
                reason="code modification requires human approval",
            )
        if spec.risk == RiskLevel.PROCESS and spec.name == "run_tests":
            return PolicyDecision(
                type=PolicyDecisionType.ALLOW,
                reason="run_tests uses an operator-configured fixed command",
            )
        return PolicyDecision(
            type=PolicyDecisionType.DENY,
            reason=f"coding policy denies {spec.risk.value} tool {spec.name}",
        )


class CodingToolPolicy:
    """Risk-level decisions refined by the path a write actually targets.

    Before this composition the policy discarded the call's arguments, so every
    ``write_file`` was equivalent: the same approval prompt covered ``src/app.py``
    and ``.env``. Approval fatigue then made the prompt a formality. Secret paths are
    now denied outright — a leaked key cannot be un-leaked by a careful reviewer —
    while ordinary source writes keep the approval gate they always had.
    """

    def __init__(
        self,
        *,
        paths: Sequence[PathRule] = DEFAULT_SECRET_PATH_RULES,
        commands: Sequence[CommandRule] = DEFAULT_DENIED_COMMANDS,
        rates: Sequence[RateLimit] = (),
        clock: Callable[[], float] = monotonic,
    ) -> None:
        """Bind the rule sets. Passing empty sequences restores the pre-0.11 behaviour."""
        self._inner = ParameterAwarePolicy(
            base=_CodingRiskPolicy(),
            paths=paths,
            commands=commands,
            rates=rates,
            clock=clock,
        )

    @property
    def limiter(self) -> SlidingWindowLimiter:
        """Expose the rate counter so a composition can reset it between runs."""
        return self._inner.limiter

    def evaluate(self, *, task_id: str, spec: ToolSpec, call: ToolCall) -> PolicyDecision:
        """Decide one call from its declared targets, then its declared risk."""
        return self._inner.evaluate(task_id=task_id, spec=spec, call=call)
