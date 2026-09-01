"""Tests for least-authority default policy decisions."""

from forgeharness.domain.models import ToolCall
from forgeharness.tools.base import RiskLevel, ToolSpec
from forgeharness.tools.policy import PolicyDecisionType, RiskBasedPolicy


def decide(risk: RiskLevel) -> PolicyDecisionType:
    policy = RiskBasedPolicy()
    decision = policy.evaluate(
        task_id="task-1",
        spec=ToolSpec(
            name="candidate", description="A candidate tool.", input_schema={}, risk=risk
        ),
        call=ToolCall(id="call-1", name="candidate"),
    )
    return decision.type


def test_policy_allows_read() -> None:
    assert decide(RiskLevel.READ) == PolicyDecisionType.ALLOW


def test_policy_requires_approval_for_write() -> None:
    assert decide(RiskLevel.WRITE) == PolicyDecisionType.REQUIRE_APPROVAL


def test_policy_denies_process_and_network() -> None:
    assert decide(RiskLevel.PROCESS) == PolicyDecisionType.DENY
    assert decide(RiskLevel.NETWORK) == PolicyDecisionType.DENY
