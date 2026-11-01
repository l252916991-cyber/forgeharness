"""Application contracts: scope error, composition record and the spec itself.

Canonical home of ``ApplicationSpec``; ``forgeharness.sdk.application`` remains a
compatibility shim. Adding fields here changes the public composition surface —
prefer contributing through ``Capability`` (see ``forgeharness.sdk.capability``).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from forgeharness.context.provider import ContextProvider
from forgeharness.domain.models import RunContext
from forgeharness.models.base import Model
from forgeharness.runtime.budget import RunBudget
from forgeharness.runtime.verification import Verifier
from forgeharness.sdk.policy import ContextualPolicy
from forgeharness.sdk.privacy import CredentialMask, EvidenceTransform
from forgeharness.tools.base import Tool
from forgeharness.tools.registry import ToolRegistry


class ScopeError(PermissionError):
    """A host execution binding cannot access the requested run/material."""


@dataclass(frozen=True)
class ApplicationComposition:
    """Audit record of how an ``ApplicationSpec`` was composed.

    ``digest`` is a structural digest over capability names, tool names,
    instruction texts and context-provider type names. It identifies the
    composition shape for tracing and comparison; it is **not** a content hash of
    tool/provider implementations. Implementation changes still require the host
    to bump the application version.
    """

    capability_names: tuple[str, ...]
    digest: str


@dataclass(frozen=True)
class ApplicationSpec:
    """Only trusted registered Python capabilities, never arbitrary import paths."""

    id: str
    version: str
    policy_version: str
    model_factory: Callable[[], Model]
    tools: tuple[Tool, ...]
    policy: ContextualPolicy
    verifier: Verifier
    budget: RunBudget = field(default_factory=RunBudget)
    instructions: tuple[str, ...] = ()
    context_providers: tuple[ContextProvider, ...] = ()
    evidence_transform: EvidenceTransform = field(default_factory=CredentialMask)
    authorize_run: Callable[[RunContext], bool] | None = None
    composition: ApplicationComposition | None = None

    def __post_init__(self) -> None:
        if not self.id or not self.version or not self.policy_version:
            raise ValueError("application and policy identity/version must be nonempty")
        registry = ToolRegistry()
        for tool in self.tools:
            registry.register(tool)
