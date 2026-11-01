"""Reject ambiguous governance inputs before any database operation."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from forgeharness.cloud.models import Receipt, RunDefinition, TenantContext
from forgeharness.cloud.provision import grant_runtime


def definition(**changes):
    return RunDefinition.model_validate(
        {
            "application_id": "app",
            "application_version": "1",
            "policy_version": "1",
            "binding_digest": "a" * 64,
            "input_digest": "b" * 64,
            "max_units": 100,
            "deadline_at": datetime.now(UTC) + timedelta(minutes=1),
        }
        | changes
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"max_units": True},
        {"max_units": "100"},
        {"max_units": -1},
        {"max_calls": 0},
        {"max_calls": True},
        {"max_units": 10**12 + 1},
        {"deadline_at": datetime(2030, 1, 1)},
        {"binding_digest": "not-a-digest"},
        {"extra": "untrusted"},
    ],
)
def test_run_definition_rejects_ambiguous_limits_and_deadlines(changes):
    with pytest.raises(ValidationError):
        definition(**changes)


def test_receipt_cannot_claim_nonexecution_with_cost():
    with pytest.raises(ValidationError, match="nonexecution"):
        Receipt(receipt_id="r", actual_units=1, result_digest="d" * 64, outcome="not_executed")


@pytest.mark.parametrize("value", ["", "tenant\x00", "tenant with spaces", "a" * 129])
def test_tenant_identity_rejects_unbounded_or_control_values(value):
    with pytest.raises(ValidationError):
        TenantContext(org_id=value, actor_id="actor")


@pytest.mark.parametrize("role", ["bad;DROP ROLE x", "has spaces", "UpperCase", "a" * 64, ""])
async def test_runtime_grant_rejects_sql_role_injection_before_query(role):
    connection = AsyncMock()
    with pytest.raises(ValueError):
        await grant_runtime(connection, role)
    connection.execute.assert_not_called()
