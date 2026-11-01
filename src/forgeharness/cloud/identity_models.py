"""Application-neutral internal Alpha identity values.

These server-produced values are not HTTP authorization payloads. Credentials
have deliberately redacted reprs; only their digests enter PostgreSQL.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True, slots=True)
class Principal:
    account_id: str
    email: str
    org_id: str | None
    session_id: str

    @property
    def actor_id(self) -> str:
        return self.account_id


@dataclass(frozen=True, slots=True)
class SessionCredentials:
    token: str = field(repr=False)
    csrf_token: str = field(repr=False)
    principal: Principal
    expires_at: datetime
