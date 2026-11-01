"""Trusted, immutable registration contracts for hosted serial applications."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from forgeharness.cloud.execution_contract import CallEnvelope, PhysicalResult
from forgeharness.domain.models import RunContext
from forgeharness.models.prepared import canonical_json
from forgeharness.runtime.model_call import ModelCallOutcome
from forgeharness.sdk.spec import ApplicationSpec, ScopeError
from forgeharness.tools.dispatcher import DispatchResult


@dataclass(frozen=True)
class PhysicalCall:
    owner: RunContext
    envelope: CallEnvelope
    idempotency_key: str


ModelVerifier = Callable[[PhysicalCall, ModelCallOutcome], Awaitable[PhysicalResult | None]]
ToolVerifier = Callable[[PhysicalCall, DispatchResult], Awaitable[PhysicalResult | None]]
ResultResolver = Callable[[PhysicalCall], Awaitable[PhysicalResult | None]]


@dataclass(frozen=True)
class HostedApplication:
    """Only trusted deployed Python registrations, never client import paths.

    Verifiers authenticate provider/business facts independently. Returning None
    means unknown, not free/nonexecution. release_digest binds implementation;
    model_binding_digest binds the secret-free provider/account/config version.
    """

    application: ApplicationSpec
    deployment_id: str
    release_digest: str
    model_binding_digest: str
    max_run_units: int
    model_reservation_units: int
    tool_reservation_units: Mapping[str, int]
    model_verifier: ModelVerifier
    tool_verifier: ToolVerifier
    resolver: ResultResolver | None = None
    resources: tuple[str, ...] = ()
    roles: tuple[str, ...] = ("user",)

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", self.deployment_id):
            raise ValueError("hosted deployment needs a bounded identifier")
        for digest in (self.release_digest, self.model_binding_digest):
            if not re.fullmatch(r"[a-f0-9]{64}", digest):
                raise ValueError("hosted deployment needs immutable release/config digests")
        if self.application.budget.max_attempts_per_call != 1:
            raise ValueError("hosted serial execution supports one physical attempt")
        reservations = dict(self.tool_reservation_units)
        if set(reservations) != {tool.spec.name for tool in self.application.tools}:
            raise ValueError("every registered tool needs an explicit unit reservation")
        for value in (self.max_run_units, self.model_reservation_units, *reservations.values()):
            if type(value) is not int or not 0 <= value <= 10**12:
                raise ValueError("hosted unit limits must be explicit nonnegative integers")
        for tool in self.application.tools:
            if tool.spec.requires_workspace:
                raise ValueError("hosted workspace mapping is not implemented")
            if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", tool.spec.name):
                raise ValueError("unsupported hosted tool identifier")
        object.__setattr__(self, "tool_reservation_units", MappingProxyType(reservations))
        object.__setattr__(self, "resources", tuple(self.resources))
        object.__setattr__(self, "roles", tuple(self.roles))

    @property
    def binding_digest(self) -> str:
        spec = self.application
        body = {
            "profile": "hosted-serial-v1",
            "deployment": self.deployment_id,
            "release": self.release_digest,
            "model_binding": self.model_binding_digest,
            "application": [spec.id, spec.version, spec.policy_version],
            "tools": [tool.spec.model_dump(mode="json") for tool in spec.tools],
            "instructions": spec.instructions,
            "context_providers": [
                f"{type(provider).__module__}.{type(provider).__qualname__}"
                for provider in spec.context_providers
            ],
            "composition": (
                {
                    "capabilities": spec.composition.capability_names,
                    "digest": spec.composition.digest,
                }
                if spec.composition is not None
                else None
            ),
            "budget": spec.budget.model_dump(mode="json"),
            "resources": self.resources,
            "roles": self.roles,
            "max_units": self.max_run_units,
            "model_reservation": self.model_reservation_units,
            "tool_reservations": dict(self.tool_reservation_units),
        }
        return hashlib.sha256(canonical_json(body).encode()).hexdigest()

    def validate(self, owner: RunContext, expected_binding: str, *, authorize: bool = True) -> None:
        if self.binding_digest != expected_binding:
            raise ScopeError("hosted deployment changed during execution")
        spec = self.application
        if (
            owner.application_id != spec.id
            or owner.application_version != spec.version
            or owner.policy_version != spec.policy_version
            or owner.scope.workspace is not None
            or owner.scope.resources != self.resources
            or owner.actor.roles != self.roles
        ):
            raise ScopeError("hosted owner scope does not match registered deployment")
        if authorize and spec.authorize_run is not None and spec.authorize_run(owner) is not True:
            raise ScopeError("hosted owner is no longer authorized")
