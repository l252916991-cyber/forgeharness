"""Policy for the first software-engineering vertical."""

from __future__ import annotations

from forgeharness.domain.models import ToolCall
from forgeharness.tools.base import RiskLevel, ToolSpec
from forgeharness.tools.policy import PolicyDecision, PolicyDecisionType


class CodingToolPolicy:
    """Allow inspection and fixed tests, require approval for workspace writes."""

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
