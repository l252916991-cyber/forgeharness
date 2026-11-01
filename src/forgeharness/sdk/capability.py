"""Composable, trusted application capabilities.

A capability is a data-only contribution of tools, instructions and context
providers. It is not a plugin loader: the host registers trusted Python objects
explicitly, and there is no arbitrary import path or model-authored code here.

Policies, verifiers, budgets and identity stay application-level. Those are
governance decisions whose composition needs explicit semantics (deny/approval
precedence, scoped applicability); silently AND-ing them would be unsafe.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from forgeharness.context.provider import ContextProvider
from forgeharness.domain.models import RunContext
from forgeharness.models.base import Model
from forgeharness.runtime.budget import RunBudget
from forgeharness.runtime.verification import Verifier
from forgeharness.sdk.policy import ContextualPolicy
from forgeharness.sdk.privacy import CredentialMask, EvidenceTransform
from forgeharness.sdk.spec import ApplicationComposition, ApplicationSpec
from forgeharness.tools.base import Tool


@dataclass(frozen=True)
class Capability:
    """A named, self-contained contribution to an application.

    ``Capability`` is deliberately data, not a base class with hooks. A host can
    compose existing tools, instructions and context providers without changing
    the runtime; later capability types can add policy/verifier contributions
    once their composition semantics are defined.

    This is an application capability, distinct from ``ModelCapability``, which
    describes what a model can do.
    """

    name: str
    tools: tuple[Tool, ...] = ()
    instructions: tuple[str, ...] = ()
    context_providers: tuple[ContextProvider, ...] = ()

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("capability name must be nonempty")
        if not (self.tools or self.instructions or self.context_providers):
            raise ValueError(f"capability {self.name!r} contributes nothing")
        names = [tool.spec.name for tool in self.tools]
        duplicates = sorted(name for name, count in Counter(names).items() if count > 1)
        if duplicates:
            raise ValueError(
                f"capability {self.name!r} contains duplicate tool name(s): "
                + ", ".join(duplicates)
            )


def compose_application(
    *,
    id: str,
    version: str,
    policy_version: str,
    model_factory: Callable[[], Model],
    capabilities: Sequence[Capability],
    policy: ContextualPolicy,
    verifier: Verifier,
    budget: RunBudget | None = None,
    instructions: tuple[str, ...] = (),
    context_providers: tuple[ContextProvider, ...] = (),
    evidence_transform: EvidenceTransform | None = None,
    authorize_run: Callable[[RunContext], bool] | None = None,
) -> ApplicationSpec:
    """Build an ``ApplicationSpec`` from ordered capabilities.

    Application-level instructions and context providers come first, followed by
    each capability in declaration order. Tool names must be unique across the
    whole application; ``ApplicationSpec`` would also reject duplicates, but this
    function reports the offending names before construction.
    """

    ordered = tuple(capabilities)
    if not ordered:
        raise ValueError("at least one capability is required")
    capability_names = [capability.name for capability in ordered]
    duplicate_capabilities = sorted(
        name for name, count in Counter(capability_names).items() if count > 1
    )
    if duplicate_capabilities:
        raise ValueError("duplicate capability name(s): " + ", ".join(duplicate_capabilities))

    merged_tools = tuple(tool for capability in ordered for tool in capability.tools)
    merged_instructions = tuple(instructions) + tuple(
        text for capability in ordered for text in capability.instructions
    )
    merged_providers = tuple(context_providers) + tuple(
        provider for capability in ordered for provider in capability.context_providers
    )

    tool_names = [tool.spec.name for tool in merged_tools]
    duplicate_tools = sorted(name for name, count in Counter(tool_names).items() if count > 1)
    if duplicate_tools:
        raise ValueError(
            "duplicate tool name(s) across capabilities: " + ", ".join(duplicate_tools)
        )

    return ApplicationSpec(
        id=id,
        version=version,
        policy_version=policy_version,
        model_factory=model_factory,
        tools=merged_tools,
        policy=policy,
        verifier=verifier,
        budget=budget if budget is not None else RunBudget(),
        instructions=merged_instructions,
        context_providers=merged_providers,
        evidence_transform=(
            evidence_transform if evidence_transform is not None else CredentialMask()
        ),
        authorize_run=authorize_run,
        composition=ApplicationComposition(
            capability_names=tuple(capability_names),
            digest=_composition_digest(
                capability_names, tool_names, merged_instructions, merged_providers
            ),
        ),
    )


def _composition_digest(
    capability_names: Sequence[str],
    tool_names: Sequence[str],
    instructions: Sequence[str],
    providers: Sequence[ContextProvider],
) -> str:
    """Structural digest of the composition shape.

    Provider/tool *implementations* are not hashed; the digest identifies which
    named capabilities, tool names, instruction texts and provider types were
    composed, in order. Implementation changes remain the host's responsibility
    to version via ``ApplicationSpec.version``.
    """

    payload = json.dumps(
        {
            "capabilities": list(capability_names),
            "tools": list(tool_names),
            "instructions": list(instructions),
            "context_providers": [type(provider).__name__ for provider in providers],
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
