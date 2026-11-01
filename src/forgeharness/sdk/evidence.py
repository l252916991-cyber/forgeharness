"""Published context and exact-span citation checks; no semantic/legal judgment."""

import hashlib
from collections.abc import Callable
from dataclasses import dataclass

from pydantic import BaseModel, Field, ValidationError

from forgeharness.context.provider import ContextMaterial
from forgeharness.domain.models import BusinessReceipt, FrozenModel, RunContext
from forgeharness.runtime.verification import VerificationRequest, VerificationResult, Verifier
from forgeharness.sdk.application import ScopeError
from forgeharness.sdk.records import RecordConflict, RecordUnavailable, RevisionStore
from forgeharness.sdk.sources import SourceRef, anchors_for
from forgeharness.tools.base import ToolContext, ToolEffectClass, ToolOutput, ToolSpec


class Citation(FrozenModel):
    resource_id: str = Field(min_length=1, max_length=200)
    version: int = Field(ge=1, strict=True)
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    start: int = Field(ge=0, strict=True)
    end: int = Field(gt=0, strict=True)
    quote: str = Field(min_length=1, max_length=4000)


class CitedAnswer(FrozenModel):
    answer: str = Field(min_length=1, max_length=30_000)
    citations: tuple[Citation, ...] = Field(min_length=1, max_length=50)


class CitationVerifier:
    """Accept only exact spans of currently published records visible to the actor."""

    def __init__(self, store: RevisionStore) -> None:
        self.store = store

    async def verify(self, request: VerificationRequest) -> VerificationResult:
        if request.run_context is None:
            return VerificationResult(passed=False, reason="trusted context required")
        try:
            answer = CitedAnswer.model_validate_json(request.final.content)
            for citation in answer.citations:
                record = self.store.published(request.run_context, citation.resource_id)
                if (
                    record.version != citation.version
                    or record.digest != citation.digest
                    or citation.end > len(record.content)
                    or citation.end <= citation.start
                    or record.content[citation.start : citation.end] != citation.quote
                ):
                    return VerificationResult(
                        passed=False, reason="citation span or version mismatch"
                    )
        except (ValidationError, ScopeError, RecordUnavailable, RecordConflict):
            return VerificationResult(passed=False, reason="citation is malformed or unavailable")
        return VerificationResult(
            passed=True,
            reason="current scoped source and exact quote verified; semantic support not assessed",
            evidence=tuple(
                f"{c.resource_id}@{c.version}:{c.start}-{c.end}" for c in answer.citations
            ),
        )


@dataclass(frozen=True)
class AllOfVerifier:
    """Compose independently useful business completion checks."""

    verifiers: tuple[Verifier, ...]

    def __post_init__(self) -> None:
        if not self.verifiers:
            raise ValueError("at least one verifier required")

    async def verify(self, request: VerificationRequest) -> VerificationResult:
        evidence: list[str] = []
        for verifier in self.verifiers:
            result = await verifier.verify(request)
            evidence.extend(result.evidence)
            if not result.passed:
                return VerificationResult(
                    passed=False, reason=result.reason, evidence=tuple(evidence)
                )
        return VerificationResult(
            passed=True, reason="all configured checks passed", evidence=tuple(evidence)
        )


@dataclass(frozen=True)
class PublishedContextProvider:
    store: RevisionStore
    resources: tuple[str, ...]

    async def provide(self, *, task: str, context: RunContext) -> tuple[ContextMaterial, ...]:
        del task
        materials = []
        for resource in self.resources:
            record = self.store.published(context, resource)
            materials.append(
                ContextMaterial(
                    id=hashlib.sha256(resource.encode()).hexdigest(),
                    source=f"record:{resource}",
                    content=record.content,
                    required=True,
                    version=str(record.version),
                    tenant_id=context.scope.tenant_id,
                    application_id=context.application_id,
                    resource_id=resource,
                    expires_at=record.expires_at,
                )
            )
        return tuple(materials)


class RecordReadInput(FrozenModel):
    resource_id: str = Field(min_length=1, max_length=200)


class PublishedRecordTool:
    """Model-callable read capability. Review/publication remain host operations."""

    input_model = RecordReadInput
    spec = ToolSpec(
        name="read_published_record",
        description="Read a current authorized published record and its exact source version.",
        input_schema=RecordReadInput.model_json_schema(),
        effect_class=ToolEffectClass.READ_ONLY,
        # A read of a published revision is a governed source: its version-bound
        # anchors must be revalidated before this output is served again (ADR 016).
        governed_sources=True,
    )

    def __init__(self, store: RevisionStore) -> None:
        self.store = store

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolOutput:
        values = RecordReadInput.model_validate(arguments.model_dump())
        if context.run_context is None:
            raise ScopeError("trusted context required")
        record = self.store.published(context.run_context, values.resource_id)
        return ToolOutput(
            ok=True,
            content=record.model_dump_json(),
            receipt=BusinessReceipt(
                resource_id=record.resource_id,
                version=str(record.version),
                status="completed",
                evidence_refs=anchors_for(
                    record.resource_id,
                    str(record.version),
                    tuple(s.locator for s in record.sources) or ("",),
                ),
            ),
        )


def published_source_validator(
    store: RevisionStore,
) -> Callable[[RunContext, tuple[SourceRef, ...]], bool]:
    """Host validator for governed tool reads: every reference must still be current.

    This is the callback a host passes to ``SessionService`` (ADR 016). It answers one
    question only — "is this still the published version this caller may read?" — and
    returns ``False`` for anything it cannot confirm, so a withdrawal, a superseding
    version or a lost permission all fail closed.
    """

    def validate(context: RunContext, refs: tuple[SourceRef, ...]) -> bool:
        for ref in refs:
            try:
                record = store.published(context, ref.resource_id)
            except (RecordUnavailable, RecordConflict, ScopeError):
                return False
            if str(record.version) != ref.version:
                return False
            if ref.locator and ref.locator not in {source.locator for source in record.sources}:
                return False
        return True

    return validate
