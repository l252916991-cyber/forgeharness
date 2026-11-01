"""Regression cases for independent approval and immutable commerce evidence."""

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_consultation import SKU, ask, build, identity

from forgeharness.applications.commerce.control import commerce_router
from forgeharness.sdk import Actor, RecordDraft, RunStatus, ScopeError
from forgeharness.sdk.sessions import SessionUnavailable


async def test_dual_role_owner_cannot_approve_own_handoff(tmp_path):
    store, _, sessions = build(tmp_path)
    owner = identity().model_copy(
        update={"actor": Actor(id="dual-role", roles=("customer", "reader", "supervisor"))}
    )
    sessions.create(owner)
    pending = await ask(sessions, owner, skus=[SKU], handoff=True)
    assert pending.status is RunStatus.AWAITING_APPROVAL
    with pytest.raises(ScopeError, match="approval is not authorized"):
        sessions.approve(owner, pending.id, approver=owner)
    assert store.list_tickets("shop-a") == ()

    supervisor = owner.model_copy(update={"actor": Actor(id="reviewer", roles=("supervisor",))})
    grant = sessions.approve(owner, pending.id, approver=supervisor)
    finished = await sessions.resume(owner, pending.id, grant=grant, approver=supervisor)
    assert finished.status is RunStatus.SUCCEEDED
    assert len(store.list_tickets("shop-a")) == 1


def test_customer_cannot_read_draft_through_management_endpoint(tmp_path):
    store, records, sessions = build(tmp_path)
    editor = identity("editor")
    revision = records.read(editor, "faq:faq-03")
    draft = records.save(
        editor,
        RecordDraft(
            resource_id="faq:faq-03", kind="commerce_faq", content="UNPUBLISHED REVIEW MATERIAL"
        ),
        expected_version=revision.version,
        request_id="updated-faq",
    )
    caller = identity()

    async def authenticate(request):
        return caller

    app = FastAPI()
    app.include_router(
        commerce_router(sessions=sessions, store=store, records=records, authenticate=authenticate)
    )
    with TestClient(app) as client:
        denied = client.get("/commerce/knowledge")
        assert denied.status_code == 403
        assert draft.content not in denied.text
        caller = identity("reviewer")
        reviewed = client.get("/commerce/knowledge")
        assert reviewed.status_code == 200
        assert any(row["content"] == draft.content for row in reviewed.json())
        caller = identity("reviewer", tenant="shop-b")
        assert draft.content not in client.get("/commerce/knowledge").text


async def test_product_version_cannot_change_content_or_move_backwards(tmp_path):
    store, _, sessions = build(tmp_path)
    owner = identity()
    sessions.create(owner)
    answer = await ask(sessions, owner, skus=[SKU])
    assert answer.status is RunStatus.SUCCEEDED
    original = store.product("shop-a", SKU)
    store.seed(**original)
    assert store.product("shop-a", SKU) == original
    for field, changed in (("name", "changed"), ("price_cents", 1), ("stock", 0)):
        with pytest.raises(ValueError, match="different content"):
            store.seed(**{**original, field: changed})
        assert store.product("shop-a", SKU) == original
    with pytest.raises(ValueError, match="stale product version"):
        store.seed(**{**original, "version": original["version"] - 1})
    assert json.loads(sessions.history(owner)[0].output)["products"][0]["price_cents"] == 1299
    store.seed(**{**original, "version": original["version"] + 1, "price_cents": 1})
    with pytest.raises(SessionUnavailable):
        sessions.history(owner)


@pytest.mark.parametrize("version", [0, -1, True, "1", 1.1])
def test_product_version_requires_positive_integer(tmp_path, version):
    store, _, _ = build(tmp_path)
    with pytest.raises(ValueError, match="positive integer"):
        store.seed(tenant="shop-a", sku="new", name="new", price_cents=1, stock=1, version=version)
    with pytest.raises(ValueError, match="product unavailable"):
        store.product("shop-a", "new")
