"""Legal and commerce consume the same SDK lifecycle without domain core branches."""

import json
from dataclasses import replace

import pytest

from forgeharness.applications.commerce import CommerceStore
from forgeharness.applications.commerce.workflows import consultation_workflow, content_workflow
from forgeharness.applications.legal import legal_review_workflow
from forgeharness.sdk import (
    Actor,
    CitedAnswer,
    RecordDraft,
    RecordUnavailable,
    ResourceScope,
    RevisionStore,
    RoleRecordPolicy,
    RunContext,
    RunStatus,
    SourceAnchor,
    WorkflowRunner,
)


def owner(application="legal", **changes):
    value = RunContext(
        application_id=application,
        application_version="1",
        policy_version="1",
        run_id="run1",
        session_id="s",
        correlation_id="c",
        actor=Actor(id="author", roles=("editor", "reader", "customer")),
        scope=ResourceScope(tenant_id="tenant", resources=("source", "draft", "product:a")),
    )
    return value.model_copy(update=changes)


def actor(context, role):
    return context.model_copy(update={"actor": Actor(id=role, roles=(role,))})


def seed(store, ctx):
    store.save(
        ctx,
        RecordDraft(
            resource_id="source",
            kind="synthetic",
            content="Synthetic source facts.",
            sources=(SourceAnchor(source="fixture", version="1", locator="page:2"),),
        ),
        expected_version=0,
        request_id="source",
    )
    store.review(actor(ctx, "reviewer"), "source", expected_version=1, approve=True)
    store.publish(actor(ctx, "publisher"), "source", expected_version=1)


def workflow(application, store):
    if application == "legal":
        return legal_review_workflow(
            store=store, evidence_id="source", note_id="draft", business_request_id="request"
        )
    return content_workflow(
        knowledge=store, knowledge_id="source", draft_id="draft", business_request_id="request"
    )


@pytest.mark.parametrize("application", ["legal", "commerce"])
async def test_shared_draft_pipeline_approval_review_and_request_deduplication(
    tmp_path, application
):
    store = RevisionStore(tmp_path / "records.db", policy=RoleRecordPolicy())
    ctx = owner(application)
    seed(store, ctx)
    definition = workflow(application, store)
    for run_id in ("r1", "r2"):
        ctx = ctx.model_copy(update={"run_id": run_id})
        runner = WorkflowRunner(definition, state_dir=tmp_path / "runs")
        result = await runner.run(context=ctx, task="Prepare an excerpt")
        assert result.status == RunStatus.AWAITING_APPROVAL
        approver = actor(ctx, "supervisor")
        grant = runner.approve(context=ctx, approver=approver)
        rebuilt = WorkflowRunner(definition, state_dir=tmp_path / "runs")
        result = await rebuilt.resume(context=ctx, grant=grant, approver=approver)
        assert result.status == RunStatus.SUCCEEDED
        record = store.read(ctx, "draft")
        assert record.version == 1 and record.state == "draft"
        assert record.dependencies[0].resource_id == "source"
        with pytest.raises(RecordUnavailable):
            store.published(ctx, "draft")
    store.review(actor(ctx, "reviewer"), "draft", expected_version=1, approve=True)
    store.publish(actor(ctx, "publisher"), "draft", expected_version=1)
    assert store.published(ctx, "draft").content == record.content
    store.withdraw(actor(ctx, "publisher"), "source", expected_version=1)
    with pytest.raises(RecordUnavailable):
        store.published(ctx, "draft")


async def test_source_edit_while_waiting_approval_prevents_stale_draft(tmp_path):
    store = RevisionStore(tmp_path / "records.db", policy=RoleRecordPolicy())
    ctx = owner()
    seed(store, ctx)
    runner = WorkflowRunner(workflow("legal", store), state_dir=tmp_path / "runs")
    await runner.run(context=ctx, task="note")
    approver = actor(ctx, "supervisor")
    grant = runner.approve(context=ctx, approver=approver)
    store.save(
        ctx,
        RecordDraft(resource_id="source", kind="synthetic", content="updated"),
        expected_version=1,
        request_id="edit",
    )
    result = await runner.resume(context=ctx, grant=grant, approver=approver)
    assert result.status != RunStatus.SUCCEEDED
    with pytest.raises(RecordUnavailable):
        store.read(ctx, "draft")


async def test_commerce_uses_current_product_api_separate_from_knowledge(tmp_path):
    records = RevisionStore(tmp_path / "records.db", policy=RoleRecordPolicy())
    ctx = owner("commerce")
    seed(records, ctx)
    products = CommerceStore(tmp_path / "products.db")
    products.seed(tenant="tenant", sku="a", name="synthetic", price_cents=123, stock=4)
    definition = consultation_workflow(
        products=products, knowledge=records, sku="a", knowledge_id="source"
    )
    runner = WorkflowRunner(definition, state_dir=tmp_path / "runs")
    result = await runner.run(context=ctx, task="consult")
    assert result.status == RunStatus.SUCCEEDED
    assert json.loads(result.nodes["current_product"].final_output)["price_cents"] == 123
    assert CitedAnswer.model_validate_json(result.nodes["knowledge"].final_output).citations
    assert result.verification.passed
    products.seed(tenant="tenant", sku="a", name="synthetic", price_cents=456, stock=1, version=2)
    from forgeharness.sdk import WorkflowVerificationRequest

    assert not (
        await definition.verifier.verify(
            WorkflowVerificationRequest(context=ctx, nodes=result.nodes)
        )
    ).passed


async def test_workflow_final_verifier_catches_source_change_between_nodes(tmp_path):
    records = RevisionStore(tmp_path / "records.db", policy=RoleRecordPolicy())
    ctx = owner("commerce")
    seed(records, ctx)
    products = CommerceStore(tmp_path / "products.db")
    products.seed(tenant="tenant", sku="a", name="synthetic", price_cents=123, stock=4)
    definition = consultation_workflow(
        products=products, knowledge=records, sku="a", knowledge_id="source"
    )
    last = definition.steps[-1]
    original = last.application.model_factory

    def factory():
        records.withdraw(actor(ctx, "publisher"), "source", expected_version=1)
        return original()

    last = replace(last, application=replace(last.application, model_factory=factory))
    definition = replace(definition, steps=(definition.steps[0], last))
    runner = WorkflowRunner(definition, state_dir=tmp_path / "runs")
    result = await runner.run(context=ctx, task="consult")
    assert all(node.status == RunStatus.SUCCEEDED for node in result.nodes.values())
    assert result.status == RunStatus.FAILED and not result.verification.passed


async def test_draft_effect_before_journal_crash_recovers_with_no_duplicate(tmp_path, monkeypatch):
    from forgeharness.state.invocation_journal import SQLiteInvocationJournal

    class Crash(BaseException):
        pass

    store = RevisionStore(tmp_path / "records.db", policy=RoleRecordPolicy())
    ctx = owner()
    seed(store, ctx)
    definition = workflow("legal", store)
    runner = WorkflowRunner(definition, state_dir=tmp_path / "runs")
    await runner.run(context=ctx, task="prepare")
    approver = actor(ctx, "supervisor")
    grant = runner.approve(context=ctx, approver=approver)

    def crash(*args, **kwargs):
        raise Crash()

    with monkeypatch.context() as patch:
        patch.setattr(SQLiteInvocationJournal, "settle", crash)
        with pytest.raises(Crash):
            await runner.resume(context=ctx, grant=grant, approver=approver)
    assert store.read(ctx, "draft").version == 1
    rebuilt = WorkflowRunner(definition, state_dir=tmp_path / "runs")
    result = await rebuilt.resume(context=ctx)
    assert result.status == RunStatus.SUCCEEDED
    assert store.read(ctx, "draft").version == 1
    assert [e["operation"] for e in store.audit(ctx, "draft")] == ["draft.created"]
