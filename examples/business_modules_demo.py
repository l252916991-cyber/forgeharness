"""Installed-wheel, keyless legal/commerce module demonstration with synthetic data."""

import asyncio
import json
import tempfile
from pathlib import Path

from forgeharness.applications.commerce import CommerceStore
from forgeharness.applications.commerce.workflows import consultation_workflow, content_workflow
from forgeharness.applications.legal import legal_review_workflow
from forgeharness.sdk import (
    Actor,
    RecordDraft,
    ResourceScope,
    RevisionStore,
    RoleRecordPolicy,
    RunContext,
    RunStatus,
    SourceAnchor,
    WorkflowRunner,
)


def identity(application: str, *, run_id: str = "demo") -> RunContext:
    return RunContext(
        application_id=application,
        application_version="1",
        policy_version="1",
        run_id=run_id,
        session_id="synthetic-session",
        correlation_id="synthetic-demo",
        actor=Actor(id="author", roles=("editor", "reader", "customer")),
        scope=ResourceScope(
            tenant_id="synthetic-tenant", resources=("source:1", "draft:1", "product:sample")
        ),
    )


def reviewer(context: RunContext, role: str) -> RunContext:
    return context.model_copy(update={"actor": Actor(id=role, roles=(role,))})


def seed(store: RevisionStore, context: RunContext, content: str) -> None:
    store.save(
        context,
        RecordDraft(
            resource_id="source:1",
            kind="synthetic_source",
            content=content,
            sources=(SourceAnchor(source="synthetic-fixture", version="1", locator="page:1"),),
        ),
        expected_version=0,
        request_id="seed",
    )
    store.review(reviewer(context, "reviewer"), "source:1", expected_version=1, approve=True)
    store.publish(reviewer(context, "publisher"), "source:1", expected_version=1)


async def demo(root: Path) -> dict[str, str]:
    store = RevisionStore(root / "records.sqlite", policy=RoleRecordPolicy())
    legal, commerce = identity("legal"), identity("commerce")
    seed(store, legal, "Synthetic case: a payment record lists 100 units on 2026-01-02.")
    seed(
        store,
        commerce,
        "Synthetic product: blue notebook with 80 pages. Prices require the live API.",
    )
    definition = legal_review_workflow(
        store=store,
        evidence_id="source:1",
        note_id="draft:1",
        business_request_id="legal-note-1",
    )
    runner = WorkflowRunner(definition, state_dir=root / "legal")
    assert (
        await runner.run(context=legal, task="Prepare a cited note for human review")
    ).status == RunStatus.AWAITING_APPROVAL
    approver = reviewer(legal, "supervisor")
    grant = runner.approve(context=legal, approver=approver)
    runner = WorkflowRunner(definition, state_dir=root / "legal")
    assert (
        await runner.resume(context=legal, grant=grant, approver=approver)
    ).status == RunStatus.SUCCEEDED
    assert store.read(legal, "draft:1").state == "draft"

    products = CommerceStore(root / "products.sqlite")
    products.seed(
        tenant=commerce.scope.tenant_id,
        sku="sample",
        name="Synthetic notebook",
        price_cents=500,
        stock=3,
    )
    consultation = WorkflowRunner(
        consultation_workflow(
            products=products,
            knowledge=store,
            sku="sample",
            knowledge_id="source:1",
        ),
        state_dir=root / "consultation",
    )
    result = await consultation.run(context=commerce, task="Read product details and current stock")
    assert result.status == RunStatus.SUCCEEDED
    assert json.loads(result.nodes["current_product"].final_output or "{}")["price_cents"] == 500

    definition = content_workflow(
        knowledge=store,
        knowledge_id="source:1",
        draft_id="draft:1",
        business_request_id="content-1",
    )
    content = WorkflowRunner(definition, state_dir=root / "content")
    author = commerce.model_copy(update={"run_id": "content"})
    assert (
        await content.run(context=author, task="Prepare a product excerpt")
    ).status == RunStatus.AWAITING_APPROVAL
    approver = reviewer(author, "supervisor")
    grant = content.approve(context=author, approver=approver)
    assert (
        await content.resume(context=author, grant=grant, approver=approver)
    ).status == RunStatus.SUCCEEDED
    store.review(reviewer(author, "reviewer"), "draft:1", expected_version=1, approve=True)
    store.publish(reviewer(author, "publisher"), "draft:1", expected_version=1)
    assert store.published(author, "draft:1").state == "published"
    return {
        "legal_note": "draft awaiting business review",
        "commerce_consultation": "verified",
        "commerce_content": "published locally after separate business review",
        "models": "deterministic synthetic fixtures; no model-quality measurement",
    }


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="forge-business-demo-") as directory:
        print(json.dumps(asyncio.run(demo(Path(directory))), ensure_ascii=False, indent=2))
