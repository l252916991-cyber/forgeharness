"""Synthetic consultation composition: comparisons, FAQs and approved handoffs.

The fixture model accepts structured consultation requests. It is an executable
integration example, not a natural-language model or a business-quality benchmark.
"""

import json
from collections.abc import Callable, Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from forgeharness.applications.commerce.application import (
    CommercePolicy,
    CommerceStore,
    CommerceVerifier,
    GeneralHandoffTicketTool,
    GetPriceStockTool,
    HandoffTicketTool,
    PriceStockInput,
)
from forgeharness.applications.commerce.catalog import CatalogProduct, search_catalog
from forgeharness.sdk import (
    ApplicationSpec,
    AuthorizationRequest,
    BusinessReceipt,
    Capability,
    FinalAction,
    Model,
    ModelRequest,
    ModelResult,
    PolicyDecision,
    PolicyDecisionType,
    RevisionStore,
    RunContext,
    ScopeError,
    SourceRef,
    ToolAction,
    ToolCall,
    ToolContext,
    ToolEffectClass,
    ToolOutput,
    ToolSpec,
    VerificationRequest,
    VerificationResult,
    compose_application,
)
from forgeharness.sdk.records import RecordUnavailable
from forgeharness.sdk.sources import governed_source_refs

EMPTY_ANSWER = json.dumps({"status": "needs_human", "reason": "no_evidence"}, sort_keys=True)


class ConsultationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=200)
    skus: tuple[str, ...] = Field(default=(), max_length=5)
    faq_ids: tuple[str, ...] = Field(default=(), max_length=5)


class ConsultationTool:
    input_model = ConsultationInput
    spec = ToolSpec(
        name="consult_catalog",
        description="Compare scoped live product facts and published FAQs.",
        input_schema=ConsultationInput.model_json_schema(),
        effect_class=ToolEffectClass.READ_ONLY,
        governed_sources=True,
    )

    def __init__(
        self, store: CommerceStore, records: RevisionStore, catalog: Sequence[CatalogProduct]
    ):
        self.store, self.records, self.catalog = store, records, tuple(catalog)
        self.prices = GetPriceStockTool(store, self.catalog)

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolOutput:
        args = ConsultationInput.model_validate(arguments.model_dump())
        owner = context.run_context
        if owner is None:
            raise ScopeError("trusted consultation context required")
        skus = args.skus or tuple(
            item.sku for item in search_catalog(self.catalog, owner, args.query, limit=5)
        )
        prices: list[dict[str, Any]] = []
        faqs: list[dict[str, Any]] = []
        anchors: list[str] = []
        unavailable = False
        for sku in dict.fromkeys(skus):
            output = await self.prices.execute(PriceStockInput(sku=sku), context)
            prices.append(json.loads(output.content))
            assert output.receipt is not None
            anchors.extend(output.receipt.evidence_refs)
        for faq_id in dict.fromkeys(args.faq_ids):
            resource = f"faq:{faq_id}"
            try:
                record = self.records.published(owner, resource)
            except RecordUnavailable:
                unavailable = True
                continue
            content = json.loads(record.content)
            faqs.append(
                {
                    "resource_id": resource,
                    "version": record.version,
                    "question": content["question"],
                    "answer": content["answer"],
                    "conflict_with": content.get("conflict_with"),
                }
            )
            anchors.append(SourceRef(resource_id=resource, version=str(record.version)).anchor)
        if not anchors:
            return ToolOutput(ok=True, content="", metadata={"source_empty": True})
        conflict = any(faq["conflict_with"] for faq in faqs)
        result = {
            "status": "needs_human" if conflict or unavailable else "answered",
            "reason": "source_conflict"
            if conflict
            else ("no_evidence" if unavailable else "verified_records"),
            "products": prices,
            "faqs": faqs,
            "sources": anchors,
        }
        answer = json.dumps(result, ensure_ascii=False, sort_keys=True)
        return ToolOutput(
            ok=True,
            content=answer,
            receipt=BusinessReceipt(
                resource_id=SourceRef.from_anchor(anchors[0]).resource_id,  # type: ignore[union-attr]
                status="completed",
                version="1",
                evidence_refs=tuple(anchors),
            ),
        )


def source_validator(
    store: CommerceStore, records: RevisionStore
) -> Callable[[RunContext, tuple[SourceRef, ...]], bool]:
    def validate(owner: RunContext, refs: tuple[SourceRef, ...]) -> bool:
        try:
            for ref in refs:
                if ref.resource_id not in owner.scope.resources:
                    return False
                if ref.resource_id.startswith("product:"):
                    if "customer" not in owner.actor.roles:
                        return False
                    row = store.product(
                        owner.scope.tenant_id, ref.resource_id.removeprefix("product:")
                    )
                    if str(row["version"]) != ref.version:
                        return False
                elif ref.resource_id.startswith("faq:"):
                    if str(records.published(owner, ref.resource_id).version) != ref.version:
                        return False
                else:
                    return False
        except (ValueError, ScopeError):
            return False
        return True

    return validate


class ConsultationPolicy(CommercePolicy):
    def evaluate(self, request: AuthorizationRequest) -> PolicyDecision:
        if request.spec.name == "consult_catalog":
            return PolicyDecision(
                type=PolicyDecisionType.ALLOW
                if "customer" in request.context.actor.roles
                else PolicyDecisionType.DENY,
                reason="consultation requires current customer grant",
            )
        return super().evaluate(request)


class ConsultationVerifier(CommerceVerifier):
    def __init__(self, store: CommerceStore, records: RevisionStore):
        super().__init__(store)
        self.validate_sources = source_validator(store, records)

    async def verify(self, request: VerificationRequest) -> VerificationResult:
        if request.tool_results and request.tool_results[-1].name == "consult_catalog":
            evidence = request.tool_results[-1]
            owner = request.run_context
            if owner is None or not evidence.ok:
                return VerificationResult(passed=False, reason="consultation evidence missing")
            try:
                refs = governed_source_refs((evidence,), frozenset({"consult_catalog"}))
            except ValueError:
                return VerificationResult(passed=False, reason="invalid consultation sources")
            valid = self.validate_sources(owner, refs)
            expected = evidence.content or EMPTY_ANSWER
            return VerificationResult(
                passed=valid and request.final.content == expected,
                reason="exact currently authorized consultation result",
            )
        return await super().verify(request)


class ConsultationFixtureModel:
    """Scripted model for the demo UI's JSON requests; never interprets credentials.

    A follow-up may omit skus: use the previous consultation's product selection,
    then query the live tool again. History is user data, not an authority source.
    Handoff request ids are supplied by the client and enforced by the ledger.
    """

    async def decide(self, request: ModelRequest) -> ModelResult:
        observations = [message for message in request.messages if message.role.value == "tool"]
        task = next(message.content for message in request.messages if message.role.value == "user")
        parameters = json.loads(task or "{}")
        if observations:
            latest = observations[-1]
            if latest.tool_name in {
                "create_handoff_ticket",
                "create_general_handoff_ticket",
            }:
                return ModelResult(action=FinalAction(content=latest.content or ""))
            answer = json.loads(latest.content or EMPTY_ANSWER)
            if parameters.get("handoff") or answer["status"] == "needs_human":
                products = answer.get("products", [])
                if products:
                    product = products[0]
                    return ModelResult(
                        action=ToolAction(
                            call=ToolCall(
                                id="handoff",
                                name="create_handoff_ticket",
                                arguments={
                                    "sku": product["sku"],
                                    "expected_version": product["version"],
                                    "request_id": parameters["request_id"],
                                    "reason": json.dumps(
                                        {
                                            "query": parameters["query"],
                                            "reason": answer["reason"],
                                            "sources": answer.get("sources", []),
                                        },
                                        ensure_ascii=False,
                                        sort_keys=True,
                                        separators=(",", ":"),
                                    ),
                                },
                            )
                        )
                    )
                if parameters.get("handoff"):
                    return ModelResult(
                        action=ToolAction(
                            call=ToolCall(
                                id="general-handoff",
                                name="create_general_handoff_ticket",
                                arguments={
                                    "request_id": parameters["request_id"],
                                    "reason": json.dumps(
                                        {
                                            "query": parameters["query"],
                                            "reason": answer["reason"],
                                            "sources": answer.get("sources", []),
                                        },
                                        ensure_ascii=False,
                                        sort_keys=True,
                                        separators=(",", ":"),
                                    ),
                                },
                            )
                        )
                    )
            return ModelResult(action=FinalAction(content=latest.content or EMPTY_ANSWER))
        skus = parameters.get("skus", [])
        if parameters.get("follow_up") and not skus:
            # Find a previous structured assistant answer in the compiled history.
            for message in request.messages:
                if "untrusted_conversation_history" not in (message.content or ""):
                    continue
                content = message.content or ""
                decoder = json.JSONDecoder()
                for position, char in enumerate(content):
                    if char != "{":
                        continue
                    try:
                        history, _ = decoder.raw_decode(content[position:])
                        answer = json.loads(history.get("assistant") or "{}")
                        skus = [row["sku"] for row in answer.get("products", [])] or skus
                    except (ValueError, TypeError, AttributeError, KeyError):
                        continue
        args = ConsultationInput(
            query=parameters["query"],
            skus=tuple(skus),
            faq_ids=tuple(parameters.get("faq_ids", [])),
        )
        return ModelResult(
            action=ToolAction(
                call=ToolCall(
                    id="consult", name="consult_catalog", arguments=args.model_dump(mode="json")
                )
            )
        )


def consultation_application(
    *,
    store: CommerceStore,
    records: RevisionStore,
    catalog: Sequence[CatalogProduct],
    model_factory: Callable[[], Model] = ConsultationFixtureModel,
) -> ApplicationSpec:
    return compose_application(
        id="commerce-consultation",
        version="2",
        policy_version="2",
        model_factory=model_factory,
        capabilities=(
            Capability(
                "consultation",
                tools=(
                    ConsultationTool(store, records, catalog),
                    HandoffTicketTool(store),
                    GeneralHandoffTicketTool(store),
                ),
            ),
        ),
        policy=ConsultationPolicy(store),
        verifier=ConsultationVerifier(store, records),
    )
