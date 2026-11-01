"""Compatibility shim for ``forgeharness.sdk.application``.

Canonical modules: :mod:`forgeharness.sdk.spec` (contracts),
:mod:`forgeharness.sdk.kernel` (RunKernel/Orchestrator/bridges) and
:mod:`forgeharness.sdk.harness` (Harness). This module only re-exports the
public names so existing imports keep working; it is removed at API v2.
"""

from forgeharness.sdk.harness import Harness, binding_digest
from forgeharness.sdk.kernel import (
    KernelBudgetExceeded,
    KernelToolDenied,
    OrchestrationError,
    Orchestrator,
    RunKernel,
)
from forgeharness.sdk.spec import ApplicationComposition, ApplicationSpec, ScopeError

__all__ = [
    "ApplicationComposition",
    "ApplicationSpec",
    "Harness",
    "KernelBudgetExceeded",
    "KernelToolDenied",
    "OrchestrationError",
    "Orchestrator",
    "RunKernel",
    "ScopeError",
    "binding_digest",
]
