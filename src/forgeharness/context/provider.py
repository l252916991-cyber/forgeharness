"""Scoped, versioned optional context providers."""

from datetime import datetime
from typing import Protocol

from pydantic import Field

from forgeharness.context.compiler import ContextItem
from forgeharness.domain.models import RunContext


class ContextMaterial(ContextItem):
    """A retrievable fragment with explicit source version and access scope."""

    version: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    application_id: str = Field(min_length=1)
    resource_id: str | None = None
    expires_at: datetime | None = None
    content_type: str = "text/plain"


class ContextProvider(Protocol):
    """Trusted adapter must filter before reading; SDK validates returned scope."""

    async def provide(self, *, task: str, context: RunContext) -> tuple[ContextMaterial, ...]: ...
