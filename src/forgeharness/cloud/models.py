"""Validated identities and facts for the PostgreSQL execution store.

TenantContext is supplied by a trusted authenticated host, never deserialized as
an authorization decision from a user's request. These models contain no DSN or
provider credentials. Unit accounting is integral and dimensioned by the host's
immutable deployment binding; it is not a customer currency invoice.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CloudDenied(PermissionError):
    """An absent, foreign or unauthorized object is unavailable."""


class CloudConflict(ValueError):
    """A known request conflicts with immutable or durable state."""


class CloudBudgetExceeded(CloudConflict):
    """Admission or settled actual usage exceeds a durable execution limit.

    Settled provider facts remain authoritative even when they exceed their
    reservation. The controller must preserve their cost and exhaust the run;
    rejecting only its success checkpoint would otherwise prevent convergence.
    """


class StaleLease(CloudConflict):
    """The supplied execution owner is not current."""


class Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


Identifier = str
MAX_UNITS = 10**12


class TenantContext(Frozen):
    org_id: Identifier = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    actor_id: Identifier = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")


class RunDefinition(Frozen):
    application_id: str = Field(min_length=1, max_length=128)
    application_version: str = Field(min_length=1, max_length=64)
    policy_version: str = Field(min_length=1, max_length=64)
    binding_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    input_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    max_units: int = Field(strict=True, ge=0, le=MAX_UNITS)
    max_calls: int = Field(default=100, strict=True, ge=1, le=10_000)
    deadline_at: datetime

    @model_validator(mode="after")
    def timezone_required(self) -> Self:
        if self.deadline_at.tzinfo is None or self.deadline_at.utcoffset() is None:
            raise ValueError("deadline needs an explicit timezone")
        return self


RunState = Literal[
    "queued",
    "running",
    "awaiting_approval",
    "reconciling",
    "succeeded",
    "failed",
    "cancelled",
    "exhausted",
]


class RunRecord(Frozen):
    org_id: str
    run_id: str
    owner_id: str
    request_key: str
    definition: RunDefinition
    status: RunState
    used_units: int = Field(ge=0)
    reserved_units: int = Field(ge=0)
    epoch: int = Field(ge=0)
    worker_id: str | None = None
    lease_actor_id: str | None = None
    lease_until: datetime | None = None
    cancel_requested: bool


class LeaseTicket(Frozen):
    org_id: str
    run_id: str
    actor_id: str
    worker_id: str
    epoch: int = Field(strict=True, ge=1)
    expires_at: datetime


class CallRequest(Frozen):
    invocation_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    tool_name: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    effect_class: Literal["read", "write", "idempotent_write"]
    arguments_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    reserve_units: int = Field(strict=True, ge=0, le=MAX_UNITS)


class CallRecord(Frozen):
    org_id: str
    run_id: str
    invocation_id: str
    tool_name: str
    effect_class: Literal["read", "write", "idempotent_write"]
    arguments_digest: str
    reserve_units: int
    state: Literal["admitted", "sent", "unknown", "settled"]
    admission_epoch: int
    receipt_id: str | None = None


class Receipt(Frozen):
    receipt_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    actual_units: int = Field(strict=True, ge=0, le=MAX_UNITS)
    result_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    outcome: Literal["completed", "not_executed"] = "completed"

    @model_validator(mode="after")
    def not_executed_cost(self) -> Self:
        if self.outcome == "not_executed" and self.actual_units:
            raise ValueError("nonexecution receipt cannot claim execution units")
        return self


def approval_fingerprint(run: RunRecord, call: CallRequest) -> str:
    """Bind approval to full deployment, owner, run and exact admission request."""
    payload = {
        "org_id": run.org_id,
        "run_id": run.run_id,
        "owner_id": run.owner_id,
        "definition": run.definition.model_dump(mode="json"),
        "call": call.model_dump(mode="json"),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
