import json
from pathlib import Path

import pytest

from forgeharness.applications.commerce.application import CommerceStore
from forgeharness.applications.commerce.catalog import load_catalog, seed_catalog
from forgeharness.applications.commerce.consultation import (
    consultation_application,
    source_validator,
)
from forgeharness.applications.commerce.knowledge import load_faq_corpus, publish_faq
from forgeharness.sdk import (
    Actor,
    ResourceScope,
    RevisionStore,
    RoleRecordPolicy,
    RunContext,
    RunStatus,
)
from forgeharness.sdk.sessions import SessionService, SessionUnavailable

DATA = Path(__file__).resolve().parents[2] / "data/synthetic/commerce"
SKU = "SYN-NB-001"


def identity(role="customer", *, session="session", tenant="shop-a"):
    catalog = load_catalog(DATA / "catalog.json")
    faqs = load_faq_corpus(DATA / "faq", tenant_id=tenant)
    return RunContext(
        application_id="commerce-consultation",
        application_version="2",
        policy_version="2",
        run_id="authenticated",
        session_id=session,
        correlation_id="demo",
        actor=Actor(id=role, roles=(role, "reader") if role == "customer" else (role,)),
        scope=ResourceScope(
            tenant_id=tenant,
            resources=tuple(f"product:{p.sku}" for p in catalog if p.tenant_id == tenant)
            + tuple(f"faq:{f.id}" for f in faqs),
        ),
    )


def build(root):
    catalog = load_catalog(DATA / "catalog.json")
    store = CommerceStore(root / "commerce.sqlite")
    seed_catalog(store, catalog)
    records = RevisionStore(root / "records.sqlite", policy=RoleRecordPolicy())
    publish_faq(
        store=records,
        entries=load_faq_corpus(DATA / "faq", tenant_id="shop-a"),
        editor=identity("editor"),
        reviewer=identity("reviewer"),
        publisher=identity("publisher"),
    )
    service = SessionService(
        consultation_application(store=store, records=records, catalog=catalog),
        state_dir=root / "sessions",
        authorize=lambda *_: True,
        validate_sources=source_validator(store, records),
    )
    return store, records, service


async def ask(service, owner, request="one", **parameters):
    return await service.submit(
        owner,
        request_id=request,
        text=json.dumps({"query": "price", "request_id": request, **parameters}),
    )


async def test_comparison_follow_up_restart_and_source_revalidation(tmp_path):
    store, records, service = build(tmp_path)
    caller = identity()
    service.create(caller)
    first = await ask(service, caller, skus=[SKU, "SYN-NB-002"])
    assert first.status is RunStatus.SUCCEEDED
    answer = json.loads(first.output)
    assert len(answer["products"]) == 2
    assert all(p["as_of"] and p["price_cents"] > 0 for p in answer["products"])
    rebuilt = SessionService(
        service.application,
        state_dir=tmp_path / "sessions",
        authorize=lambda *_: True,
        validate_sources=source_validator(store, records),
    )
    second = await ask(rebuilt, caller, "two", follow_up=True)
    assert second.status is RunStatus.SUCCEEDED
    assert len(json.loads(second.output)["products"]) == 2
    store.seed(tenant="shop-a", sku=SKU, name="new", price_cents=2000, stock=2, version=4)
    with pytest.raises(SessionUnavailable):
        rebuilt.history(caller)


@pytest.mark.parametrize(
    "parameters", [{"handoff": True}, {"faq_ids": ["faq-01"]}, {"faq_ids": ["faq-missing"]}]
)
async def test_handoff_has_context_and_retry_does_not_duplicate(tmp_path, parameters):
    store, _, service = build(tmp_path)
    # Missing evidence must still name an authorized (but unpublished) resource.
    caller = identity().model_copy(
        update={
            "scope": ResourceScope(
                tenant_id="shop-a", resources=(*identity().scope.resources, "faq:faq-missing")
            )
        }
    )
    for number in range(2):
        current = caller.model_copy(update={"session_id": f"s{number}"})
        service.create(current)
        turn = await ask(service, current, "issue-one", skus=[SKU], **parameters)
        assert turn.status is RunStatus.AWAITING_APPROVAL, turn.error
        assert len(store.list_tickets("shop-a")) == number
        approver = current.model_copy(
            update={"actor": Actor(id="supervisor", roles=("supervisor",))}
        )
        grant = service.approve(current, turn.id, approver=approver)
        finished = await service.resume(current, turn.id, grant=grant, approver=approver)
        assert finished.status is RunStatus.SUCCEEDED, finished.error
    tickets = store.list_tickets("shop-a")
    assert len(tickets) == 1
    assert json.loads(tickets[0]["reason"])["sources"]
    assert store.list_tickets("shop-b") == ()


async def test_no_evidence_and_cross_scope_are_never_fabricated_answers(tmp_path):
    _, _, service = build(tmp_path)
    caller = identity()
    service.create(caller)
    answer = await ask(service, caller, query="unfindable-xyz")
    assert answer.status is RunStatus.SUCCEEDED
    assert json.loads(answer.output) == {"status": "needs_human", "reason": "no_evidence"}
    other = identity(session="other", tenant="shop-b").model_copy(
        update={"scope": ResourceScope(tenant_id="shop-b", resources=())}
    )
    service.create(other)
    denied = await ask(service, other, skus=[SKU])
    assert denied.status is not RunStatus.SUCCEEDED


async def test_explicit_handoff_without_product_creates_one_general_ticket(tmp_path):
    store, _, service = build(tmp_path)
    caller = identity()
    ticket_ids = []
    for number in range(2):
        current = caller.model_copy(update={"session_id": f"general-{number}"})
        service.create(current)
        pending = await ask(
            service,
            current,
            "general-issue",
            query="unfindable-xyz",
            handoff=True,
        )
        assert pending.status is RunStatus.AWAITING_APPROVAL
        assert len(store.list_tickets("shop-a")) == number
        approver = current.model_copy(
            update={"actor": Actor(id="supervisor", roles=("supervisor",))}
        )
        grant = service.approve(current, pending.id, approver=approver)
        finished = await service.resume(current, pending.id, grant=grant, approver=approver)
        assert finished.status is RunStatus.SUCCEEDED
        ticket_ids.append(finished.output)
    tickets = store.list_tickets("shop-a")
    assert len(tickets) == 1
    assert ticket_ids[0] == ticket_ids[1] == tickets[0]["id"]
    assert tickets[0]["sku"] == ""
    reason = json.loads(tickets[0]["reason"])
    assert reason == {"query": "unfindable-xyz", "reason": "no_evidence", "sources": []}
