"""Commerce compositions: reviewed knowledge plus live facts; separately reviewed drafts."""

from forgeharness.applications.commerce.application import (
    CommerceStore,
    CommerceVerifier,
    commerce_application,
)
from forgeharness.applications.records import cited_draft_workflow, record_application
from forgeharness.sdk import (
    CitationVerifier,
    FinalAction,
    ModelRequest,
    ModelResult,
    RevisionStore,
    ToolAction,
    ToolCall,
    VerificationRequest,
    VerificationResult,
    WorkflowSpec,
    WorkflowStep,
    WorkflowVerificationRequest,
)


class ProductSnapshotModel:
    def __init__(self, sku: str) -> None:
        self.sku = sku

    async def decide(self, request: ModelRequest) -> ModelResult:
        latest = request.messages[-1]
        if latest.role.value == "tool":
            return ModelResult(action=FinalAction(content=latest.content or ""))
        return ModelResult(
            action=ToolAction(
                call=ToolCall(
                    id="product",
                    name="product_lookup",
                    arguments={"sku": self.sku},
                )
            )
        )


class ConsultationVerifier:
    def __init__(self, products: CommerceStore, knowledge: RevisionStore) -> None:
        self.products, self.knowledge = products, knowledge

    async def verify(self, request: WorkflowVerificationRequest) -> VerificationResult:
        for node, verifier in (
            ("knowledge", CitationVerifier(self.knowledge)),
            ("current_product", CommerceVerifier(self.products)),
        ):
            result = request.nodes[node]
            check = await verifier.verify(
                VerificationRequest(
                    task_id=result.task_id,
                    workspace=None,
                    run_context=request.context,
                    final=FinalAction(content=result.final_output or ""),
                    tool_results=result.tool_evidence,
                )
            )
            if not check.passed:
                return check
        return VerificationResult(passed=True, reason="knowledge and product versions rechecked")


def consultation_workflow(
    *, products: CommerceStore, knowledge: RevisionStore, sku: str, knowledge_id: str
) -> WorkflowSpec:
    """Expose separately verified current price/stock and reviewed descriptive knowledge."""
    return WorkflowSpec(
        "commerce",
        "1",
        "1",
        (
            WorkflowStep(
                "knowledge",
                record_application(
                    application_id="commerce",
                    store=knowledge,
                    resource_id=knowledge_id,
                ),
                "Read reviewed product explanations with source quotes.",
            ),
            WorkflowStep(
                "current_product",
                commerce_application(
                    store=products,
                    model_factory=lambda: ProductSnapshotModel(sku),
                ),
                "Read current price and stock from the product ledger.",
                ("knowledge",),
            ),
        ),
        verifier=ConsultationVerifier(products, knowledge),
    )


def content_workflow(
    *,
    knowledge: RevisionStore,
    knowledge_id: str,
    draft_id: str,
    business_request_id: str,
    expected_version: int = 0,
) -> WorkflowSpec:
    """Prepare a source-backed content excerpt; publication is a separate host action."""
    return cited_draft_workflow(
        application_id="commerce",
        store=knowledge,
        source_id=knowledge_id,
        target_id=draft_id,
        business_request_id=business_request_id,
        kind="content_draft",
        expected_version=expected_version,
    )
