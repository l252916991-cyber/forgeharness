from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_consultation import SKU, build, identity

from forgeharness.applications.commerce.control import commerce_router
from forgeharness.sdk import ResourceScope


def test_console_and_data_endpoints_enforce_current_roles_and_scope(tmp_path):
    store, records, sessions = build(tmp_path)
    caller = identity()

    async def authenticate(request):
        return caller

    app = FastAPI()
    app.include_router(
        commerce_router(sessions=sessions, store=store, records=records, authenticate=authenticate)
    )
    with TestClient(app) as client:
        assert client.get("/commerce/console").status_code == 200
        assert "运行证据" in client.get("/commerce/console").text
        assert "<h2>后台任务</h2>" not in client.get("/commerce/console").text
        assert (
            client.post(
                "/commerce/consult", json={"request_id": "one", "query": "price", "skus": [SKU]}
            ).json()["status"]
            == "succeeded"
        )
        assert (
            client.post(
                "/commerce/consult", json={"request_id": "one", "query": "price", "skus": [SKU]}
            ).json()["sequence"]
            == 1
        )
        assert (
            client.post(
                "/commerce/consult", json={"request_id": "one", "query": "price", "actor": "admin"}
            ).status_code
            == 422
        )
        assert client.get("/commerce/knowledge").status_code == 403
        assert (
            client.post(
                "/commerce/knowledge/faq:faq-01/review", json={"expected_version": 1}
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/commerce/knowledge/faq:faq-01/publish", json={"expected_version": 1}
            ).status_code
            == 403
        )
        assert client.get("/commerce/tickets").status_code == 403
        ticket = store.ticket(
            key="key",
            tenant="shop-a",
            sku=SKU,
            request_id="test",
            reason="synthetic",
            expected_version=3,
        )
        caller = identity("supervisor")
        assert client.get("/commerce/tickets").json()[0]["id"] == ticket
        assert client.get("/commerce/tickets/" + ticket).json()["sku"] == SKU
        caller = caller.model_copy(
            update={"scope": ResourceScope(tenant_id="shop-a", resources=())}
        )
        assert client.get("/commerce/tickets").json() == []
        assert client.get("/commerce/tickets/" + ticket).status_code == 403
        caller = identity("supervisor", tenant="shop-b")
        assert client.get("/commerce/tickets/" + ticket).status_code == 403


def test_support_can_read_tenant_general_ticket_without_product_scope(tmp_path):
    store, records, sessions = build(tmp_path)
    ticket = store.general_ticket(
        key="general-key",
        tenant="shop-a",
        request_id="general-request",
        reason="no product matched",
    )
    caller = identity("support").model_copy(
        update={"scope": ResourceScope(tenant_id="shop-a", resources=())}
    )

    async def authenticate(request):
        return caller

    app = FastAPI()
    app.include_router(
        commerce_router(sessions=sessions, store=store, records=records, authenticate=authenticate)
    )
    with TestClient(app) as client:
        rows = client.get("/commerce/tickets")
        assert rows.status_code == 200
        assert [row["id"] for row in rows.json()] == [ticket]
        detail = client.get(f"/commerce/tickets/{ticket}")
        assert detail.status_code == 200
        assert detail.json()["sku"] == ""
