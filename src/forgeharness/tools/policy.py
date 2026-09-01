"""Deterministic policy decisions applied before tool execution."""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol

from forgeharness.domain.models import FrozenModel, ToolCall
from forgeharness.tools.base import RiskLevel, ToolSpec


class PolicyDecisionType(StrEnum):
    """Possible pre-execution policy outcomes."""

    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


class PolicyDecision(FrozenModel):
    """Policy outcome with a user- and trace-visible reason."""

    type: PolicyDecisionType
    reason: str


class ToolPolicy(Protocol):
    """Interface for task-aware tool authorization."""

    def evaluate(self, *, task_id: str, spec: ToolSpec, call: ToolCall) -> PolicyDecision:
        """Decide whether a normalized tool call may execute."""
        ...


class RiskBasedPolicy:
    """Allow reads, require approval for writes, and deny external effects by default."""

    def evaluate(self, *, task_id: str, spec: ToolSpec, call: ToolCall) -> PolicyDecision:
        """Return the default least-authority decision for a tool risk level."""
        del task_id, call
        if spec.risk == RiskLevel.READ:
            return PolicyDecision(type=PolicyDecisionType.ALLOW, reason="read-only tool")
        if spec.risk == RiskLevel.WRITE:
            return PolicyDecision(
                type=PolicyDecisionType.REQUIRE_APPROVAL,
                reason="workspace mutation requires approval",
            )
        return PolicyDecision(
            type=PolicyDecisionType.DENY,
            reason=f"{spec.risk.value} tools are disabled by the default policy",
        )
