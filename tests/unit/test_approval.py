"""Tests for exact, expiring, one-shot approval grants."""

from datetime import UTC, datetime, timedelta

import pytest

from forgeharness.domain.models import ToolCall
from forgeharness.state.approval import (
    ApprovalError,
    InMemoryApprovalLedger,
    call_fingerprint,
)


def test_fingerprint_is_stable_across_argument_order() -> None:
    first = ToolCall(id="one", name="write", arguments={"path": "a", "content": "b"})
    second = ToolCall(id="two", name="write", arguments={"content": "b", "path": "a"})

    assert call_fingerprint("task", first) == call_fingerprint("task", second)


def test_grant_is_bound_to_exact_task_and_arguments() -> None:
    ledger = InMemoryApprovalLedger()
    call = ToolCall(id="one", name="write", arguments={"path": "allowed"})
    grant = ledger.issue(task_id="task", call=call, granted_by="user")
    changed = call.model_copy(update={"arguments": {"path": "different"}})

    with pytest.raises(ApprovalError, match="does not match"):
        ledger.consume(grant, task_id="task", call=changed)

    ledger.consume(grant, task_id="task", call=call)


def test_grant_can_only_be_consumed_once() -> None:
    ledger = InMemoryApprovalLedger()
    call = ToolCall(id="one", name="write")
    grant = ledger.issue(task_id="task", call=call, granted_by="user")
    ledger.consume(grant, task_id="task", call=call)

    with pytest.raises(ApprovalError, match="already been consumed"):
        ledger.consume(grant, task_id="task", call=call)


def test_ledger_rejects_forged_and_expired_grants() -> None:
    now = [datetime(2026, 9, 1, tzinfo=UTC)]
    ledger = InMemoryApprovalLedger(clock=lambda: now[0])
    call = ToolCall(id="one", name="write")
    grant = ledger.issue(task_id="task", call=call, granted_by="user", ttl=timedelta(minutes=1))
    forged = grant.model_copy(update={"granted_by": "attacker"})

    with pytest.raises(ApprovalError, match="not issued"):
        ledger.consume(forged, task_id="task", call=call)
    now[0] += timedelta(minutes=2)
    with pytest.raises(ApprovalError, match="expired"):
        ledger.consume(grant, task_id="task", call=call)


def test_ledger_requires_positive_ttl() -> None:
    with pytest.raises(ValueError, match="ttl must be positive"):
        InMemoryApprovalLedger().issue(
            task_id="task",
            call=ToolCall(id="one", name="write"),
            granted_by="user",
            ttl=timedelta(0),
        )
