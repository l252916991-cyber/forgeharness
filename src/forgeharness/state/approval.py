"""Exact, expiring, one-shot approval grants."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import uuid4

from pydantic import Field

from forgeharness.domain.models import FrozenModel, ToolCall


class ApprovalError(RuntimeError):
    """An approval grant is invalid for the requested action."""


class ApprovalGrant(FrozenModel):
    """Authorization bound to one task and canonical tool call."""

    id: str = Field(default_factory=lambda: str(uuid4()))
    task_id: str
    call_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    granted_by: str = Field(min_length=1, max_length=200)
    expires_at: datetime


class ApprovalLedger(Protocol):
    """Issue and atomically consume approval grants."""

    def issue(
        self,
        *,
        task_id: str,
        call: ToolCall,
        granted_by: str,
        ttl: timedelta = timedelta(minutes=10),
    ) -> ApprovalGrant:
        """Create a grant for one exact normalized action."""
        ...

    def consume(self, grant: ApprovalGrant, *, task_id: str, call: ToolCall) -> None:
        """Verify and mark a grant consumed in one operation."""
        ...


def call_fingerprint(task_id: str, call: ToolCall) -> str:
    """Hash a task-bound canonical representation of a tool call."""
    canonical = json.dumps(
        {"task_id": task_id, "name": call.name, "arguments": call.arguments},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(canonical).hexdigest()


class InMemoryApprovalLedger:
    """One-process approval ledger for tests and local non-durable runs."""

    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        self._issued: dict[str, ApprovalGrant] = {}
        self._consumed: set[str] = set()
        self._clock = clock or (lambda: datetime.now(UTC))

    def issue(
        self,
        *,
        task_id: str,
        call: ToolCall,
        granted_by: str,
        ttl: timedelta = timedelta(minutes=10),
    ) -> ApprovalGrant:
        """Create and retain a task-bound grant with a positive lifetime."""
        if ttl <= timedelta(0):
            raise ValueError("approval ttl must be positive")
        grant = ApprovalGrant(
            task_id=task_id,
            call_fingerprint=call_fingerprint(task_id, call),
            granted_by=granted_by,
            expires_at=self._clock() + ttl,
        )
        self._issued[grant.id] = grant
        return grant

    def consume(self, grant: ApprovalGrant, *, task_id: str, call: ToolCall) -> None:
        """Reject forged, expired, mismatched, or previously consumed grants."""
        issued = self._issued.get(grant.id)
        if issued is None or issued != grant:
            raise ApprovalError("approval grant was not issued by this ledger")
        if grant.id in self._consumed:
            raise ApprovalError("approval grant has already been consumed")
        if self._clock() >= grant.expires_at:
            raise ApprovalError("approval grant has expired")
        expected = call_fingerprint(task_id, call)
        if grant.task_id != task_id or not hmac.compare_digest(grant.call_fingerprint, expected):
            raise ApprovalError("approval grant does not match this tool call")
        self._consumed.add(grant.id)
