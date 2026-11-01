"""Read-only case question answering and pending-review notes (plan phase S3).

The pilot composes the public SDK: a case adapter supplies page-level material, two
read-only tools expose search and page reading, and the answer contract is enforced by
a verifier rather than by asking the model nicely. A factual answer must carry
citations that resolve to *currently published* revisions and to exact character spans;
an answer that declares insufficient evidence must carry none. An uncited factual claim
therefore cannot end a run successfully.

Publishing case pages is a host operation (``publish_case_pages``) and is deliberately
not reachable from a tool: review and publication stay outside the model's control.
"""

from __future__ import annotations

import json
from collections.abc import Callable

from pydantic import BaseModel, ConfigDict, Field

from forgeharness.applications.legal.adapter import CaseAdapter, CaseUnavailable
from forgeharness.sdk import (
    ApplicationSpec,
    AuthorizationRequest,
    BusinessReceipt,
    Capability,
    Citation,
    CitationVerifier,
    CitedAnswer,
    FinalAction,
    FrozenModel,
    Model,
    PolicyDecision,
    PolicyDecisionType,
    RecordUnavailable,
    RevisionStore,
    RiskLevel,
    RunContext,
    ScopeError,
    ToolEffectClass,
    ToolOutput,
    ToolSpec,
    VerificationRequest,
    VerificationResult,
    anchors_for,
    compose_application,
)

MAX_QUERY = 300
CASE_TOOLS = ("search_case", "read_case_page")


class CaseSearchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str = Field(min_length=1, max_length=100)
    query: str = Field(min_length=1, max_length=MAX_QUERY)
    limit: int = Field(default=5, ge=1, le=20)


class CasePageInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str = Field(min_length=1, max_length=100)
    locator: str = Field(min_length=1, max_length=100)


class LegalAnswer(FrozenModel):
    """The answer contract: facts need citations, insufficiency needs silence."""

    answer: str = Field(min_length=1, max_length=4_000)
    citations: tuple[Citation, ...] = ()
    insufficient_evidence: bool = False


class LegalAnswerVerifier:
    """Enforce the answer contract before a run may report success.

    Citation checking is delegated to :class:`CitationVerifier`, so a citation is only
    accepted when it is an exact span of the revision published *now*. The additional
    rule is that a refusal may not carry citations and an answer may not omit them: that
    is what stops "no evidence" from becoming a free pass for a fabricated statement.
    """

    def __init__(self, store: RevisionStore) -> None:
        self.citations = CitationVerifier(store)

    async def verify(self, request: VerificationRequest) -> VerificationResult:
        if request.run_context is None:
            return VerificationResult(passed=False, reason="trusted context required")
        try:
            answer = LegalAnswer.model_validate_json(request.final.content)
        except ValueError:
            return VerificationResult(
                passed=False, reason="answer is not the declared legal answer contract"
            )
        if answer.insufficient_evidence:
            if answer.citations:
                return VerificationResult(
                    passed=False, reason="an insufficiency claim must not carry citations"
                )
            return VerificationResult(
                passed=True,
                reason="declared insufficient evidence; semantic support not assessed",
                evidence=("legal-answer:insufficient",),
            )
        if not answer.citations:
            return VerificationResult(
                passed=False, reason="a factual answer must cite currently published material"
            )
        # CitationVerifier validates the narrower `CitedAnswer` contract, so hand it that
        # shape rather than the pilot's wrapper with its extra field.
        cited = CitedAnswer(answer=answer.answer, citations=answer.citations)
        return await self.citations.verify(
            request.model_copy(update={"final": FinalAction(content=cited.model_dump_json())})
        )


def _require_context(context: object) -> RunContext:
    run_context = getattr(context, "run_context", None)
    if not isinstance(run_context, RunContext):
        raise ScopeError("trusted context required")
    return run_context


def _page_resource(case_id: str, locator: str) -> str:
    return f"case:{case_id}/{locator}"


def _citation_ready(
    store: RevisionStore,
    context: RunContext,
    case_id: str,
    locator: str,
    quote: str,
    page_text: str,
) -> dict[str, object] | None:
    """Resolve a page hit against the *published* revision, or drop it.

    A page that is not published is not evidence: the pilot answers only from material
    a human has reviewed. The adapter's live page must still equal the published text,
    otherwise the reader would cite offsets that no longer mean the same thing.
    """
    resource_id = _page_resource(case_id, locator)
    try:
        record = store.published(context, resource_id)
    except (RecordUnavailable, ValueError):
        return None
    if record.content != page_text:
        return None
    start = record.content.find(quote)
    if start < 0:
        return None
    return {
        "resource_id": record.resource_id,
        "version": record.version,
        "digest": record.digest,
        "start": start,
        "end": start + len(quote),
        "quote": quote,
        "locator": locator,
    }


class CaseSearchTool:
    """Literal page search inside one authorized case, resolved to published revisions."""

    input_model = CaseSearchInput
    spec = ToolSpec(
        name="search_case",
        description=(
            "Search one authorized case. Returns citation-ready spans "
            "(resource_id, version, digest, start, end, quote) from published pages only."
        ),
        input_schema=CaseSearchInput.model_json_schema(),
        risk=RiskLevel.READ,
        effect_class=ToolEffectClass.READ_ONLY,
        governed_sources=True,
    )

    def __init__(self, adapter: CaseAdapter, store: RevisionStore) -> None:
        self.adapter = adapter
        self.store = store

    async def execute(self, arguments: BaseModel, context: object) -> ToolOutput:
        values = CaseSearchInput.model_validate(arguments.model_dump())
        run_context = _require_context(context)
        hits = self.adapter.search(run_context, values.case_id, values.query, limit=values.limit)
        found: list[dict[str, object]] = []
        anchors: list[tuple[str, str]] = []
        for hit in hits:
            resolved = _citation_ready(
                self.store,
                run_context,
                values.case_id,
                hit.locator,
                hit.quote,
                self.adapter.page(run_context, values.case_id, hit.locator).content,
            )
            if resolved is None:
                continue
            found.append(resolved)
            anchors.append((str(resolved["resource_id"]), str(resolved["version"])))
        if not found:
            return ToolOutput(
                ok=True,
                content="",
                metadata={"source_empty": True},
            )
        refs: list[str] = []
        for resource_id, version in anchors:
            refs.extend(anchors_for(resource_id, version, [resource_id.rsplit("/", 1)[-1]]))
        return ToolOutput(
            ok=True,
            content=json.dumps({"evidence": found}, ensure_ascii=False, sort_keys=True),
            metadata={"hits": len(found), "case_id": values.case_id},
            receipt=BusinessReceipt(
                resource_id=f"case:{values.case_id}",
                status="completed",
                version=str(found[0]["version"]),
                evidence_refs=tuple(dict.fromkeys(refs)),
            ),
        )


class CasePageTool:
    """Read one published page of an authorized case, citation-ready."""

    input_model = CasePageInput
    spec = ToolSpec(
        name="read_case_page",
        description="Read one published page of an authorized case with its locator.",
        input_schema=CasePageInput.model_json_schema(),
        risk=RiskLevel.READ,
        effect_class=ToolEffectClass.READ_ONLY,
        governed_sources=True,
    )

    def __init__(self, adapter: CaseAdapter, store: RevisionStore) -> None:
        self.adapter = adapter
        self.store = store

    async def execute(self, arguments: BaseModel, context: object) -> ToolOutput:
        values = CasePageInput.model_validate(arguments.model_dump())
        run_context = _require_context(context)
        page = self.adapter.page(run_context, values.case_id, values.locator)
        resource_id = _page_resource(values.case_id, values.locator)
        try:
            record = self.store.published(run_context, resource_id)
        except (RecordUnavailable, ValueError) as exc:
            raise CaseUnavailable("page is not published") from exc
        if record.content != page.content:
            raise CaseUnavailable("published page no longer matches the case material")
        return ToolOutput(
            ok=True,
            content=json.dumps(
                {
                    "resource_id": record.resource_id,
                    "version": record.version,
                    "digest": record.digest,
                    "locator": page.locator,
                    "content": record.content,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            metadata={"case_id": values.case_id, "locator": page.locator},
            receipt=BusinessReceipt(
                resource_id=record.resource_id,
                status="completed",
                version=str(record.version),
                evidence_refs=anchors_for(record.resource_id, str(record.version), (page.locator,)),
            ),
        )


class ReadOnlyCasePolicy:
    """Only the two read tools exist; everything else is denied outright."""

    def evaluate(self, request: AuthorizationRequest) -> PolicyDecision:
        if request.call.name in CASE_TOOLS:
            return PolicyDecision(type=PolicyDecisionType.ALLOW, reason="read-only case access")
        return PolicyDecision(
            type=PolicyDecisionType.DENY, reason="the legal pilot exposes no other capability"
        )

    def can_approve(self, *, requester: RunContext, owner: RunContext, call: object) -> bool:
        del requester, owner, call
        return False


def case_qa_application(
    adapter: CaseAdapter,
    store: RevisionStore,
    *,
    model_factory: Callable[[], Model],
    application_id: str = "legal-case-qa",
    version: str = "1",
    policy_version: str = "1",
) -> ApplicationSpec:
    """A read-only question-answering application over one case adapter.

    ``store`` is the citation authority; without it uncited text could pass as an
    answer, so it is required rather than optional.
    """
    if not callable(model_factory):
        raise ValueError("a model factory is required")
    return compose_application(
        id=application_id,
        version=version,
        policy_version=policy_version,
        model_factory=model_factory,
        capabilities=(
            Capability(
                "case-tools",
                tools=(CaseSearchTool(adapter, store), CasePageTool(adapter, store)),
            ),
        ),
        policy=ReadOnlyCasePolicy(),
        verifier=LegalAnswerVerifier(store),
    )


def publish_case_pages(
    adapter: CaseAdapter,
    store: RevisionStore,
    *,
    reader: RunContext,
    reviewer: RunContext,
    publisher: RunContext,
    case_id: str,
    request_prefix: str = "case-page",
) -> tuple[int, ...]:
    """Host operation: draft, review and publish every page of one case.

    Publication is deliberately outside the tool surface. The returned versions are the
    citation targets a read-only answer may reference.
    """
    versions: list[int] = []
    for index, (resource_id, draft) in enumerate(adapter.page_drafts(reader, case_id), start=1):
        revision = store.save(
            reader,
            draft,
            expected_version=0,
            request_id=f"{request_prefix}:{case_id}:{index}",
        )
        store.review(reviewer, resource_id, expected_version=revision.version, approve=True)
        published = store.publish(publisher, resource_id, expected_version=revision.version)
        versions.append(published.version)
    return tuple(versions)


__all__ = [
    "CASE_TOOLS",
    "CasePageInput",
    "CasePageTool",
    "CaseSearchInput",
    "CaseSearchTool",
    "LegalAnswer",
    "LegalAnswerVerifier",
    "ReadOnlyCasePolicy",
    "case_qa_application",
    "publish_case_pages",
]
