"""Account-authenticated entrypoint: current identity and safe local operations."""

import shutil
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from forgeharness.applications.commerce.catalog import load_catalog
from forgeharness.applications.commerce.consultation import ConsultationFixtureModel
from forgeharness.applications.commerce.identity import (
    APPROVAL_HEADER,
    CSRF_HEADER,
    SESSION_HEADER,
    IdentityStore,
)
from forgeharness.applications.commerce.operations import (
    BackupError,
    bind_application_state,
    create_backup,
    restore_backup,
    service_lock,
)
from forgeharness.applications.commerce.workbench import bootstrap_admin, create_workbench
from forgeharness.sdk import ModelRequest, ModelResult, RecordDraft, ScopeError

DATA = Path(__file__).resolve().parents[2] / "data/synthetic/commerce/catalog.json"
PASSWORD = "account-test correct horse battery"


@dataclass
class Workbench:
    app: FastAPI
    state: Path
    accounts: dict[str, str]
    identities: IdentityStore

    def client(
        self, name: str, *, workspace: str = "shop-a", session: str = "session-a"
    ) -> TestClient:
        client = TestClient(self.app, base_url="https://testserver")
        response = client.post(
            "/identity/login",
            json={"email": f"{name}@example.test", "password": PASSWORD, "workspace_id": workspace},
        )
        assert response.status_code == 200, response.text
        client.headers[CSRF_HEADER] = response.json()["csrf"]
        client.headers[SESSION_HEADER] = session
        return client


@pytest.fixture
def workbench(tmp_path: Path) -> Iterator[Workbench]:
    state = tmp_path / "state"
    bootstrap_admin(state, workspace_id="shop-a", email="admin@example.test", password=PASSWORD)
    identities = IdentityStore(state / "identity.sqlite")
    accounts = {
        name: identities.add_user(
            workspace_id="shop-a",
            email=f"{name}@example.test",
            password=PASSWORD,
            display_name=name,
            role=role,
        )
        for name, role in (
            ("customer", "customer"),
            ("other", "customer"),
            ("supervisor", "supervisor"),
            ("support", "support"),
            ("editor", "editor"),
            ("publisher", "publisher"),
        )
    }
    identities.create_workspace("Shop B", workspace_id="shop-b")
    identities.add_user(
        workspace_id="shop-b",
        email="foreign@example.test",
        password=PASSWORD,
        display_name="Foreign",
        role="customer",
    )
    app = create_workbench(state, catalog=load_catalog(DATA))
    with TestClient(app, base_url="https://testserver"):
        yield Workbench(app, state, accounts, identities)


def submit(client: TestClient, *, handoff: bool = False) -> dict:
    response = client.post(
        "/commerce/consult",
        json={
            "request_id": "request-a",
            "query": "price",
            "skus": ["SYN-NB-001"],
            "handoff": handoff,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def approve(client: TestClient, turn_id: str) -> dict:
    identity = client.post(
        "/identity/approval",
        json={"email": "supervisor@example.test", "password": PASSWORD, "workspace_id": "shop-a"},
    )
    assert identity.status_code == 200, identity.text
    client.headers[APPROVAL_HEADER] = identity.json()["approval_token"]
    result = client.post(f"/harness/sessions/session-a/turns/{turn_id}/approve", json={})
    assert result.status_code == 200, result.text
    return result.json()


def test_entrypoint_has_account_ui_and_refuses_public_role_header(workbench: Workbench) -> None:
    client = TestClient(workbench.app, base_url="https://testserver")
    page = client.get("/")
    assert page.status_code == 200 and "接受邀请" in page.text
    assert page.headers["cache-control"] == "no-store"
    assert page.headers["referrer-policy"] == "no-referrer"
    assert client.get("/commerce/console").status_code == 404
    assert (
        client.post(
            "/commerce/consult",
            headers={"Authorization": "Bearer supervisor"},
            json={"request_id": "forged", "query": "price"},
        ).status_code
        == 401
    )
    assert workbench.client("customer").get("/identity/me").headers["cache-control"] == "no-store"


def test_start_before_bootstrap_does_not_retire_the_empty_identity_path(tmp_path: Path) -> None:
    state = tmp_path / "uninitialized"
    with pytest.raises(ValueError, match="bootstrap"):
        create_workbench(state)
    assert not (state / "identity.sqlite").exists()
    bootstrap_admin(state, workspace_id="shop-a", email="admin@example.test", password=PASSWORD)


def test_account_session_csrf_stable_request_and_same_role_owner_boundary(
    workbench: Workbench,
) -> None:
    customer = workbench.client("customer")
    csrf = customer.headers.pop(CSRF_HEADER)
    assert (
        customer.post("/commerce/consult", json={"request_id": "bad", "query": "price"}).status_code
        == 403
    )
    customer.headers[CSRF_HEADER] = csrf
    first = submit(customer)
    assert first["status"] == "succeeded"
    assert submit(customer)["id"] == first["id"]
    for suffix in ("", "/history", f"/turns/{first['id']}/inspect", f"/turns/{first['id']}/events"):
        assert customer.get("/harness/sessions/session-a" + suffix).status_code == 200
        assert (
            workbench.client("other").get("/harness/sessions/session-a" + suffix).status_code == 403
        )
    foreign = workbench.client("foreign", workspace="shop-b")
    assert foreign.get(f"/harness/sessions/session-a/turns/{first['id']}").status_code == 403


def test_independent_approval_and_tenant_support_ticket_access(workbench: Workbench) -> None:
    customer = workbench.client("customer")
    pending = submit(customer, handoff=True)
    assert pending["status"] == "awaiting_approval"
    base = f"/harness/sessions/session-a/turns/{pending['id']}"
    assert customer.post(base + "/approve", json={}).status_code == 403
    assert (
        customer.post(
            "/identity/approval",
            json={"email": "customer@example.test", "password": PASSWORD, "workspace_id": "shop-a"},
        ).status_code
        == 403
    )
    grant = approve(customer, pending["id"])
    resumed = customer.post(base + "/resume", json={"grant": grant})
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["status"] == "succeeded"
    tickets = workbench.client("support").get("/commerce/tickets")
    assert tickets.status_code == 200 and len(tickets.json()) == 1
    assert customer.get("/commerce/tickets").status_code == 403
    assert workbench.client("admin").get("/commerce/tickets").status_code == 403


def test_revoked_approver_cannot_consume_existing_grant(workbench: Workbench) -> None:
    customer = workbench.client("customer")
    pending = submit(customer, handoff=True)
    grant = approve(customer, pending["id"])
    admin = workbench.identities.login(
        email="admin@example.test", password=PASSWORD, workspace_id="shop-a"
    ).principal
    workbench.identities.disable_user(actor=admin, user_id=workbench.accounts["supervisor"])
    refused = customer.post(
        f"/harness/sessions/session-a/turns/{pending['id']}/resume", json={"grant": grant}
    )
    assert refused.status_code in {401, 403}
    assert workbench.app.state.workbench.commerce.list_tickets("shop-a") == ()


def test_current_authorizer_refuses_a_previously_trusted_owner_after_disable(
    workbench: Workbench,
) -> None:
    customer = workbench.client("customer")
    turn = submit(customer)
    services = workbench.app.state.workbench
    context = services.sessions.get_turn(
        services.identities.context_for(
            services.identities.current_principal(
                user_id=workbench.accounts["customer"], workspace_id="shop-a"
            ),
            application_id="commerce-workbench",
            application_version="1",
            policy_version="1",
            session_id="session-a",
            resources=tuple(
                f"product:{p.sku}" for p in load_catalog(DATA) if p.tenant_id == "shop-a"
            ),
        ),
        turn["id"],
    ).context
    admin = workbench.identities.login(
        email="admin@example.test", password=PASSWORD, workspace_id="shop-a"
    ).principal
    workbench.identities.disable_user(actor=admin, user_id=workbench.accounts["customer"])
    assert customer.get("/harness/sessions/session-a/history").status_code == 401
    with pytest.raises(ScopeError):
        services.sessions.history(context)


def test_non_customer_cannot_submit_or_read_sessions(workbench: Workbench) -> None:
    for name in ("admin", "support", "editor", "publisher", "supervisor"):
        client = workbench.client(name)
        assert (
            client.post(
                "/commerce/consult", json={"request_id": name, "query": "price"}
            ).status_code
            == 403
        )
        assert client.get("/harness/sessions/session-a/history").status_code == 403
    assert workbench.client("support").get("/commerce/knowledge").status_code == 403
    assert workbench.client("editor").get("/commerce/knowledge").status_code == 200


def test_knowledge_permissions_recheck_current_grants_and_keep_independent_review(
    tmp_path: Path,
) -> None:
    state = tmp_path / "knowledge"
    bootstrap_admin(state, workspace_id="shop-a", email="admin@example.test", password=PASSWORD)
    identities = IdentityStore(state / "identity.sqlite")
    users = {}
    for name, role in (("editor", "editor"), ("publisher", "publisher")):
        users[name] = identities.add_user(
            workspace_id="shop-a",
            email=f"{name}@example.test",
            password=PASSWORD,
            display_name=name,
            role=role,
        )
    app = create_workbench(state, resources=lambda principal: ("faq:test",))
    with TestClient(app, base_url="https://testserver"):
        services = app.state.workbench
        author = identities.context_for(
            identities.current_principal(user_id=users["editor"], workspace_id="shop-a"),
            application_id="commerce-workbench",
            application_version="1",
            policy_version="1",
            session_id="knowledge-seed",
            resources=("faq:test",),
        )
        draft = services.records.save(
            author,
            RecordDraft(resource_id="faq:test", kind="faq", content='{"answer":"fixture"}'),
            expected_version=0,
            request_id="draft-test",
        )
        workbench = Workbench(app, state, users, identities)
        editor = workbench.client("editor")
        assert (
            editor.post(
                "/commerce/knowledge/faq:test/review", json={"expected_version": draft.version}
            ).status_code
            == 403
        )
        publisher = workbench.client("publisher")
        reviewed = publisher.post(
            "/commerce/knowledge/faq:test/review", json={"expected_version": draft.version}
        )
        assert reviewed.status_code == 200, reviewed.text
        published = publisher.post(
            "/commerce/knowledge/faq:test/publish", json={"expected_version": draft.version}
        )
        assert published.status_code == 200, published.text
        admin = identities.login(
            email="admin@example.test", password=PASSWORD, workspace_id="shop-a"
        ).principal
        identities.grant_role(actor=admin, user_id=users["publisher"], role="editor")
        dual = identities.context_for(
            identities.current_principal(user_id=users["publisher"], workspace_id="shop-a"),
            application_id="commerce-workbench",
            application_version="1",
            policy_version="1",
            session_id="knowledge-seed",
            resources=("faq:test",),
        )
        revision = services.records.save(
            dual,
            RecordDraft(resource_id="faq:test", kind="faq", content='{"answer":"new"}'),
            expected_version=draft.version,
            request_id="draft-dual",
        )
        assert (
            publisher.post(
                "/commerce/knowledge/faq:test/review", json={"expected_version": revision.version}
            ).status_code
            == 403
        )
        identities.revoke_membership(actor=admin, user_id=users["publisher"], role="publisher")
        with pytest.raises(ScopeError):
            services.records.read(dual, "faq:test")


def test_workbench_initialization_and_backup_are_mutually_exclusive(
    workbench: Workbench, tmp_path: Path
) -> None:
    assert TestClient(workbench.app).get("/health/ready").status_code == 200
    with pytest.raises(BackupError, match="active"):
        create_backup(workbench.state, tmp_path / "backup")
    with pytest.raises(BackupError, match="active"):
        create_workbench(workbench.state)


def test_bootstrap_is_private_offline_and_repeated_bootstrap_is_refused(tmp_path: Path) -> None:
    state = tmp_path / "bootstrap"
    bootstrap_admin(state, workspace_id="private", email="admin@example.test", password=PASSWORD)
    assert state.stat().st_mode & 0o077 == 0
    with pytest.raises(ValueError, match="already exists"):
        bootstrap_admin(
            state, workspace_id="another", email="other@example.test", password=PASSWORD
        )
    with service_lock(state), pytest.raises(BackupError, match="active"):
        bootstrap_admin(
            state, workspace_id="blocked", email="blocked@example.test", password=PASSWORD
        )


def test_failed_bootstrap_does_not_retire_the_empty_identity_path(tmp_path: Path) -> None:
    state = tmp_path / "bootstrap"
    with pytest.raises(ValueError):
        bootstrap_admin(state, workspace_id="private", email="admin@example.test", password="short")
    assert not (state / "identity.sqlite").exists()
    bootstrap_admin(state, workspace_id="private", email="admin@example.test", password=PASSWORD)


def test_state_binding_refuses_another_application(tmp_path: Path) -> None:
    state = tmp_path / "bound"
    bootstrap_admin(state, workspace_id="shop-a", email="admin@example.test", password=PASSWORD)
    with service_lock(state), pytest.raises(BackupError):
        bind_application_state(
            state, application_id="commerce-consultation", application_version="2"
        )


def test_public_role_demo_cannot_reopen_private_workbench_state(tmp_path: Path) -> None:
    import asyncio

    from examples.commerce_demo import build, demo, web_app

    state = tmp_path / "private-workbench"
    bootstrap_admin(state, workspace_id="shop-a", email="admin@example.test", password=PASSWORD)
    identities = IdentityStore(state / "identity.sqlite")
    for role in ("customer", "supervisor", "support"):
        identities.add_user(
            workspace_id="shop-a",
            email=f"{role}@example.test",
            password=PASSWORD,
            display_name=role,
            role=role,
        )
    app = create_workbench(state)
    services = Workbench(app, state, {}, identities)
    with TestClient(app, base_url="https://testserver"):
        customer = services.client("customer")
        pending = customer.post(
            "/commerce/consult",
            json={"request_id": "private-ticket", "query": "need support", "handoff": True},
        )
        assert pending.status_code == 200
        turn_id = pending.json()["id"]
        grant = approve(customer, turn_id)
        resumed = customer.post(
            f"/harness/sessions/session-a/turns/{turn_id}/resume", json={"grant": grant}
        )
        assert resumed.status_code == 200 and resumed.json()["status"] == "succeeded"
    for legacy_entry in (build, web_app):
        with pytest.raises(BackupError, match="binding differs"):
            legacy_entry(state)
    with pytest.raises(BackupError, match="binding differs"):
        asyncio.run(demo(state))
    with TestClient(create_workbench(state), base_url="https://testserver") as client:
        assert (
            client.get("/commerce/tickets", headers={"Authorization": "Bearer support"}).status_code
            == 401
        )


def test_account_and_pending_approval_restore_with_workbench_binding(tmp_path: Path) -> None:
    state = tmp_path / "restored"
    bootstrap_admin(state, workspace_id="shop-a", email="admin@example.test", password=PASSWORD)
    identities = IdentityStore(state / "identity.sqlite")
    for role in ("customer", "supervisor", "support"):
        identities.add_user(
            workspace_id="shop-a",
            email=f"{role}@example.test",
            password=PASSWORD,
            display_name=role,
            role=role,
        )
    app = create_workbench(state)
    with TestClient(app, base_url="https://testserver"):
        customer = Workbench(app, state, {}, identities).client("customer")
        response = customer.post(
            "/commerce/consult",
            json={"request_id": "restore", "query": "no matching product", "handoff": True},
        )
        assert response.status_code == 200, response.text
        pending = response.json()
        assert pending["status"] == "awaiting_approval"
    backup = tmp_path / "backup"
    manifest = create_backup(state, backup)
    assert manifest.application_id == "commerce-workbench"
    assert manifest.application_version == "1"
    assert "identity.sqlite" in manifest.files
    shutil.rmtree(state)
    restore_backup(backup, state)
    restored = create_workbench(state)
    with TestClient(restored, base_url="https://testserver"):
        services = Workbench(restored, state, {}, IdentityStore(state / "identity.sqlite"))
        customer = services.client("customer")
        grant = approve(customer, pending["id"])
        base = f"/harness/sessions/session-a/turns/{pending['id']}"
        resumed = customer.post(base + "/resume", json={"grant": grant})
        assert resumed.status_code == 200, resumed.text
        assert resumed.json()["status"] == "succeeded"
        assert customer.get(base + "/events").status_code == 200
        assert customer.post(base + "/resume", json={"grant": grant}).status_code == 409
        assert len(services.client("support").get("/commerce/tickets").json()) == 1


def test_ready_checks_required_tables_not_just_database_connectivity(workbench: Workbench) -> None:
    import sqlite3

    with sqlite3.connect(workbench.state / "identity.sqlite") as connection:
        connection.execute("DROP TABLE users")
    response = TestClient(workbench.app).get("/health/ready")
    assert response.status_code == 503


def test_revocation_after_model_send_blocks_following_tool_execution(tmp_path: Path) -> None:
    state = tmp_path / "revoked"
    bootstrap_admin(state, workspace_id="shop-a", email="admin@example.test", password=PASSWORD)
    identities = IdentityStore(state / "identity.sqlite")
    user = identities.add_user(
        workspace_id="shop-a",
        email="customer@example.test",
        password=PASSWORD,
        display_name="Customer",
        role="customer",
    )
    admin = identities.login(
        email="admin@example.test", password=PASSWORD, workspace_id="shop-a"
    ).principal

    class RevokeDuringModel(ConsultationFixtureModel):
        async def decide(self, request: ModelRequest) -> ModelResult:
            result = await super().decide(request)
            identities.disable_user(actor=admin, user_id=user)
            return result

    app = create_workbench(state, catalog=load_catalog(DATA), model_factory=RevokeDuringModel)
    with TestClient(app, base_url="https://testserver"):
        client = Workbench(app, state, {"customer": user}, identities).client("customer")
        response = client.post(
            "/commerce/consult",
            json={"request_id": "revoked-in-flight", "query": "price", "skus": ["SYN-NB-001"]},
        )
        assert response.status_code == 403
        assert app.state.workbench.commerce.list_tickets("shop-a") == ()
