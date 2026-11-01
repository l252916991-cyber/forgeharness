"""Regression cases for the application's own identity and authorization boundary.

These cases pin the properties an invitation-only, multi-user console depends on:
a role name is never an identity, credentials are re-read on every request,
revocation and disabling are immediate, an approver is authenticated separately
from the run owner, and object identifiers never authorize anything by themselves.
"""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_consultation import SKU
from test_consultation import build as build_commerce

from forgeharness.applications.commerce.application import CommerceStore
from forgeharness.applications.commerce.catalog import load_catalog
from forgeharness.applications.commerce.control import commerce_router
from forgeharness.applications.commerce.identity import (
    APPROVAL_HEADER,
    COOKIE_NAME,
    CSRF_HEADER,
    SESSION_HEADER,
    AuthenticationError,
    IdentityStore,
    LoginThrottled,
    Principal,
)
from forgeharness.applications.commerce.identity_http import (
    approver_dependency,
    context_dependency,
    identity_router,
)
from forgeharness.applications.commerce.knowledge import load_faq_corpus
from forgeharness.sdk import ScopeError
from forgeharness.sdk.session_control import session_router

DATA = Path(__file__).resolve().parents[2] / "data/synthetic/commerce"
WORKSPACE = "shop-a"
PASSWORD = "correct horse battery staple"
ROLES = ("admin", "customer", "editor", "publisher", "supervisor", "support")


class Clock:
    """Injectable UTC clock so expiry and throttling never need real sleeping."""

    def __init__(self) -> None:
        self.now = datetime(2026, 10, 28, 3, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


def email_for(role: str, suffix: str = "") -> str:
    return f"{role}{suffix}@example.test"


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def accounts(tmp_path: Path, clock: Clock) -> tuple[IdentityStore, dict[str, str]]:
    store = IdentityStore(tmp_path / "identity.sqlite", now=clock)
    store.create_workspace("Shop A", workspace_id=WORKSPACE)
    users = {
        role: store.add_user(
            workspace_id=WORKSPACE,
            email=email_for(role),
            password=PASSWORD,
            display_name=role.title(),
            role=role,
        )
        for role in ROLES
    }
    return store, users


def sign_in(store: IdentityStore, role: str, *, suffix: str = "") -> tuple[str, Principal]:
    result = store.login(email=email_for(role, suffix), password=PASSWORD, workspace_id=WORKSPACE)
    return result.token, result.principal


def workspace_resources() -> tuple[str, ...]:
    catalog = load_catalog(DATA / "catalog.json")
    faqs = load_faq_corpus(DATA / "faq", tenant_id=WORKSPACE)
    return tuple(f"product:{item.sku}" for item in catalog if item.tenant_id == WORKSPACE) + tuple(
        f"faq:{item.id}" for item in faqs
    )


@dataclass
class Console:
    app: FastAPI
    store: IdentityStore
    commerce: CommerceStore
    users: dict[str, str]
    clock: Clock

    def login(self, role: str, *, session_id: str = "console-1") -> TestClient:
        client = TestClient(self.app)
        response = client.post(
            "/identity/login",
            json={
                "email": email_for(role),
                "password": PASSWORD,
                "workspace_id": WORKSPACE,
            },
        )
        assert response.status_code == 200, response.text
        client.headers[CSRF_HEADER] = response.json()["csrf"]
        client.headers[SESSION_HEADER] = session_id
        return client


@pytest.fixture
def console(tmp_path: Path, clock: Clock) -> Console:
    commerce, records, sessions = build_commerce(tmp_path)
    store = IdentityStore(tmp_path / "identity.sqlite", now=clock)
    store.create_workspace("Shop A", workspace_id=WORKSPACE)
    users = {
        role: store.add_user(
            workspace_id=WORKSPACE,
            email=email_for(role),
            password=PASSWORD,
            display_name=role.title(),
            role=role,
        )
        for role in ROLES
    }
    resources = workspace_resources()
    owner = context_dependency(
        store,
        application_id="commerce-consultation",
        application_version="2",
        policy_version="2",
        resources=lambda principal: resources,
    )
    approver = approver_dependency(
        store,
        application_id="commerce-consultation",
        application_version="2",
        policy_version="2",
        resources=lambda principal: resources,
    )
    app = FastAPI()
    app.include_router(identity_router(store, secure_cookies=False))
    app.include_router(
        commerce_router(sessions=sessions, store=commerce, records=records, authenticate=owner)
    )
    app.include_router(session_router(sessions, authenticate=owner, authenticate_approver=approver))
    return Console(app=app, store=store, commerce=commerce, users=users, clock=clock)


# -- credentials -------------------------------------------------------------


def test_unknown_email_and_wrong_password_are_indistinguishable(
    accounts: tuple[IdentityStore, dict[str, str]],
) -> None:
    store, _ = accounts
    with pytest.raises(AuthenticationError) as unknown:
        store.login(email=email_for("ghost"), password=PASSWORD, workspace_id=WORKSPACE)
    with pytest.raises(AuthenticationError) as wrong:
        store.login(
            email=email_for("customer"), password="not the account password", workspace_id=WORKSPACE
        )
    assert type(unknown.value) is AuthenticationError
    assert type(wrong.value) is AuthenticationError
    assert str(unknown.value) == str(wrong.value)
    assert "ghost" not in str(unknown.value)


def test_raw_secrets_never_reach_the_database(
    accounts: tuple[IdentityStore, dict[str, str]],
) -> None:
    store, _ = accounts
    token, _ = sign_in(store, "customer")
    with sqlite3.connect(store.path) as connection:
        values: list[str] = []
        for table in ("users", "web_sessions", "approval_sessions", "audit"):
            for row in connection.execute(f"SELECT * FROM {table}"):
                values.extend(str(value) for value in row if isinstance(value, str))
    assert PASSWORD not in " ".join(values)
    assert token not in values
    with sqlite3.connect(store.path) as connection:
        stored = connection.execute("SELECT token_hash FROM web_sessions").fetchone()
        salt_and_hash = connection.execute(
            "SELECT password_salt, password_hash FROM users"
        ).fetchall()
    assert stored is not None and len(str(stored[0])) == 64
    assert all(len(str(row[0])) >= 32 and len(str(row[1])) == 64 for row in salt_and_hash)


def test_disabled_user_loses_access_on_the_next_request(
    accounts: tuple[IdentityStore, dict[str, str]],
) -> None:
    store, users = accounts
    admin_token, admin = sign_in(store, "admin")
    customer_token, customer = sign_in(store, "customer")
    assert store.authenticate(customer_token).user_id == customer.user_id
    store.disable_user(actor=admin, user_id=users["customer"])
    with pytest.raises(AuthenticationError):
        store.authenticate(customer_token)
    with pytest.raises(AuthenticationError):
        store.login(email=email_for("customer"), password=PASSWORD, workspace_id=WORKSPACE)
    assert store.authenticate(admin_token).roles == ("admin",)


def test_revoked_membership_drops_the_identity_immediately(
    accounts: tuple[IdentityStore, dict[str, str]],
) -> None:
    store, users = accounts
    _, admin = sign_in(store, "admin")
    token, _ = sign_in(store, "support")
    assert store.authenticate(token).roles == ("support",)
    store.revoke_membership(actor=admin, user_id=users["support"], role="support")
    with pytest.raises(AuthenticationError):
        store.authenticate(token)


def test_trusted_runtime_identity_lookup_rechecks_membership_and_workspace(
    accounts: tuple[IdentityStore, dict[str, str]],
) -> None:
    store, users = accounts
    _, admin = sign_in(store, "admin")
    current = store.current_principal(user_id=users["support"], workspace_id=WORKSPACE)
    assert current.roles == ("support",)
    other = store.create_workspace("Other")
    with pytest.raises(AuthenticationError):
        store.current_principal(user_id=current.user_id, workspace_id=other)
    store.revoke_membership(actor=admin, user_id=current.user_id, role="support")
    with pytest.raises(AuthenticationError):
        store.current_principal(user_id=current.user_id, workspace_id=WORKSPACE)


def test_session_expiry_needs_a_new_login(
    accounts: tuple[IdentityStore, dict[str, str]], clock: Clock
) -> None:
    store, _ = accounts
    token, _ = sign_in(store, "customer")
    clock.advance(timedelta(hours=8, seconds=1))
    with pytest.raises(AuthenticationError):
        store.authenticate(token)


def test_repeated_failures_throttle_the_address(
    accounts: tuple[IdentityStore, dict[str, str]],
) -> None:
    store, _ = accounts
    for _ in range(5):
        with pytest.raises(AuthenticationError):
            store.login(
                email=email_for("customer"), password="wrong password", workspace_id=WORKSPACE
            )
    with pytest.raises(LoginThrottled):
        store.login(email=email_for("customer"), password=PASSWORD, workspace_id=WORKSPACE)


def test_administrator_holds_neither_approval_nor_consult(
    accounts: tuple[IdentityStore, dict[str, str]],
) -> None:
    store, _ = accounts
    _, admin = sign_in(store, "admin")
    assert not admin.has("approval:decide")
    assert not admin.has("consult")
    assert admin.has("member:manage")
    with pytest.raises(ScopeError):
        store.approve_session(email=email_for("admin"), password=PASSWORD, workspace_id=WORKSPACE)


def test_actor_identifier_is_a_stable_user_not_a_role(
    accounts: tuple[IdentityStore, dict[str, str]],
) -> None:
    store, _ = accounts
    store.add_user(
        workspace_id=WORKSPACE,
        email=email_for("customer", ".second"),
        password=PASSWORD,
        display_name="Second Customer",
        role="customer",
    )
    _, first = sign_in(store, "customer")
    _, second = sign_in(store, "customer", suffix=".second")
    assert first.actor_id != second.actor_id
    assert first.actor_id.startswith("user:") and second.actor_id.startswith("user:")
    assert "customer" not in first.actor_id
    assert first.roles == second.roles == ("customer",)


def test_csrf_token_is_bound_to_the_live_session(
    accounts: tuple[IdentityStore, dict[str, str]],
) -> None:
    store, _ = accounts
    result = store.login(email=email_for("customer"), password=PASSWORD, workspace_id=WORKSPACE)
    assert store.verify_csrf(token=result.token, csrf=result.csrf)
    assert not store.verify_csrf(token=result.token, csrf="forged-token")
    store.logout(token=result.token)
    with pytest.raises(AuthenticationError):
        store.verify_csrf(token=result.token, csrf=result.csrf)


# -- invitations and memberships --------------------------------------------


def test_invitation_grants_only_the_invited_role(
    accounts: tuple[IdentityStore, dict[str, str]],
) -> None:
    store, _ = accounts
    _, admin = sign_in(store, "admin")
    token = store.invite(actor=admin, email="new.support@example.test", role="support")
    result = store.accept_invitation(token=token, password=PASSWORD, display_name="New Support")
    assert result.principal.roles == ("support",)
    assert result.principal.has("ticket:claim")
    assert not result.principal.has("consult")
    with pytest.raises(AuthenticationError):
        store.accept_invitation(token=token, password=PASSWORD, display_name="Replay")


def test_only_member_managers_can_invite(
    accounts: tuple[IdentityStore, dict[str, str]],
) -> None:
    store, _ = accounts
    _, customer = sign_in(store, "customer")
    with pytest.raises(ScopeError):
        store.invite(actor=customer, email="x@example.test", role="supervisor")


def test_session_binding_has_exactly_one_owner(
    accounts: tuple[IdentityStore, dict[str, str]],
) -> None:
    store, _ = accounts
    _, first = sign_in(store, "customer")
    _, second = sign_in(store, "supervisor")
    assert store.bind_session(principal=first, session_id="console-1") == first.user_id
    with pytest.raises(ScopeError):
        store.bind_session(principal=second, session_id="console-1")
    with pytest.raises(ScopeError):
        store.session_owner(principal=second, session_id="console-1")
    support_token, support = sign_in(store, "support")
    assert support_token
    with pytest.raises(ScopeError):
        store.bind_session(principal=support, session_id="console-2")


def test_audit_trail_records_decisions_without_secrets(
    accounts: tuple[IdentityStore, dict[str, str]],
) -> None:
    store, users = accounts
    _, admin = sign_in(store, "admin")
    customer_token, customer = sign_in(store, "customer")
    store.bind_session(principal=customer, session_id="console-1")
    store.disable_user(actor=admin, user_id=users["customer"])
    entries = store.audit_tail(limit=50)
    actions = {str(entry["action"]) for entry in entries}
    assert {"login", "session.bind", "member.disable"} <= actions
    assert all(str(entry["detail"] or "").find(PASSWORD) == -1 for entry in entries)
    with pytest.raises(AuthenticationError):
        store.authenticate(customer_token)


# -- HTTP console ------------------------------------------------------------


def test_login_cookie_is_hardened(console: Console) -> None:
    app = FastAPI()
    app.include_router(identity_router(console.store, secure_cookies=True))
    client = TestClient(app)
    response = client.post(
        "/identity/login",
        json={"email": email_for("customer"), "password": PASSWORD, "workspace_id": WORKSPACE},
    )
    assert response.status_code == 200, response.text
    cookie = response.headers["set-cookie"].lower()
    assert cookie.startswith(COOKIE_NAME)
    assert "httponly" in cookie
    assert "secure" in cookie
    assert "samesite=lax" in cookie
    assert "path=/" in cookie
    assert PASSWORD not in response.text
    assert "password" not in response.text.lower()


def test_identity_endpoint_rejects_asserted_identity(console: Console) -> None:
    client = TestClient(console.app)
    response = client.post(
        "/identity/login",
        json={
            "email": email_for("customer"),
            "password": PASSWORD,
            "workspace_id": WORKSPACE,
            "roles": ["admin"],
        },
    )
    assert response.status_code == 422


def test_consult_requires_csrf_and_a_bound_session(console: Console) -> None:
    client = console.login("customer")
    payload = {"request_id": "r-1", "query": "price", "skus": [SKU]}
    client.headers[CSRF_HEADER] = "forged-token"
    denied = client.post("/commerce/consult", json=payload)
    assert denied.status_code == 403
    signed_in = TestClient(console.app)
    login = signed_in.post(
        "/identity/login",
        json={"email": email_for("customer"), "password": PASSWORD, "workspace_id": WORKSPACE},
    )
    signed_in.headers[SESSION_HEADER] = "console-1"
    without_csrf = signed_in.post("/commerce/consult", json=payload)
    assert login.status_code == 200
    assert without_csrf.status_code == 403


def test_a_second_customer_cannot_read_or_write_the_first_session(console: Console) -> None:
    console.store.add_user(
        workspace_id=WORKSPACE,
        email=email_for("customer", ".second"),
        password=PASSWORD,
        display_name="Second Customer",
        role="customer",
    )
    owner = console.login("customer", session_id="console-owner")
    response = owner.post(
        "/commerce/consult",
        json={"request_id": "r-1", "query": "price", "skus": [SKU]},
    )
    assert response.status_code == 200, response.text
    intruder = TestClient(console.app)
    login = intruder.post(
        "/identity/login",
        json={
            "email": email_for("customer", ".second"),
            "password": PASSWORD,
            "workspace_id": WORKSPACE,
        },
    )
    assert login.status_code == 200
    intruder.headers[CSRF_HEADER] = login.json()["csrf"]
    intruder.headers[SESSION_HEADER] = "console-owner"
    stolen_read = intruder.get("/harness/sessions/console-owner")
    stolen_write = intruder.post(
        "/commerce/consult",
        json={"request_id": "r-2", "query": "price", "skus": [SKU]},
    )
    assert stolen_read.status_code == 403
    assert stolen_write.status_code == 403
    assert owner.get("/harness/sessions/console-owner").status_code == 200


def test_handoff_needs_a_separate_approver_credential(console: Console) -> None:
    owner = console.login("customer")
    pending = owner.post(
        "/commerce/consult",
        json={
            "request_id": "handoff-1",
            "query": "handoff",
            "skus": [SKU],
            "handoff": True,
        },
    )
    assert pending.status_code == 200, pending.text
    turn = pending.json()
    assert turn["status"] == "awaiting_approval"
    self_serve = owner.post(
        "/identity/approval",
        json={
            "email": email_for("customer"),
            "password": PASSWORD,
            "workspace_id": WORKSPACE,
        },
    )
    assert self_serve.status_code == 403
    no_credential = owner.post(f"/harness/sessions/console-1/turns/{turn['id']}/approve")
    assert no_credential.status_code == 403
    assert console.commerce.list_tickets(WORKSPACE) == ()
    approval = owner.post(
        "/identity/approval",
        json={
            "email": email_for("supervisor"),
            "password": PASSWORD,
            "workspace_id": WORKSPACE,
        },
    )
    assert approval.status_code == 200, approval.text
    owner.headers[APPROVAL_HEADER] = approval.json()["approval_token"]
    grant = owner.post(f"/harness/sessions/console-1/turns/{turn['id']}/approve")
    assert grant.status_code == 200, grant.text
    resumed = owner.post(
        f"/harness/sessions/console-1/turns/{turn['id']}/resume",
        json={"grant": grant.json()},
    )
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["status"] == "succeeded"
    assert len(console.commerce.list_tickets(WORKSPACE)) == 1


def test_dual_role_user_still_cannot_approve_its_own_handoff(console: Console) -> None:
    _, admin = sign_in(console.store, "admin")
    dual = console.store.add_user(
        workspace_id=WORKSPACE,
        email="dual@example.test",
        password=PASSWORD,
        display_name="Dual Role",
        role="customer",
    )
    console.store.grant_role(actor=admin, user_id=dual, role="supervisor")
    client = TestClient(console.app)
    login = client.post(
        "/identity/login",
        json={"email": "dual@example.test", "password": PASSWORD, "workspace_id": WORKSPACE},
    )
    assert login.status_code == 200, login.text
    client.headers[CSRF_HEADER] = login.json()["csrf"]
    client.headers[SESSION_HEADER] = "console-dual"
    pending = client.post(
        "/commerce/consult",
        json={"request_id": "handoff-2", "query": "handoff", "skus": [SKU], "handoff": True},
    )
    assert pending.status_code == 200, pending.text
    turn = pending.json()
    approval = client.post(
        "/identity/approval",
        json={"email": "dual@example.test", "password": PASSWORD, "workspace_id": WORKSPACE},
    )
    assert approval.status_code == 200, approval.text
    client.headers[APPROVAL_HEADER] = approval.json()["approval_token"]
    refused = client.post(f"/harness/sessions/console-dual/turns/{turn['id']}/approve")
    assert refused.status_code == 403
    assert console.commerce.list_tickets(WORKSPACE) == ()


def test_expired_approval_credential_is_refused(console: Console) -> None:
    owner = console.login("customer")
    pending = owner.post(
        "/commerce/consult",
        json={"request_id": "handoff-3", "query": "handoff", "skus": [SKU], "handoff": True},
    )
    turn = pending.json()
    approval = owner.post(
        "/identity/approval",
        json={"email": email_for("supervisor"), "password": PASSWORD, "workspace_id": WORKSPACE},
    )
    owner.headers[APPROVAL_HEADER] = approval.json()["approval_token"]
    console.clock.advance(timedelta(minutes=10, seconds=1))
    refused = owner.post(f"/harness/sessions/console-1/turns/{turn['id']}/approve")
    assert refused.status_code == 401
    assert console.commerce.list_tickets(WORKSPACE) == ()


def test_support_scope_and_customer_denial(console: Console) -> None:
    console.commerce.general_ticket(
        key="seed-1", tenant=WORKSPACE, request_id="seed-1", reason="handoff"
    )
    console.commerce.general_ticket(
        key="seed-2", tenant="shop-b", request_id="seed-2", reason="other tenant"
    )
    support = console.login("support", session_id="support-1")
    listed = support.get("/commerce/tickets")
    assert listed.status_code == 200, listed.text
    assert len(listed.json()) == 1
    customer = console.login("customer")
    assert customer.get("/commerce/tickets").status_code == 403
    admin = console.login("admin", session_id="admin-1")
    assert admin.get("/commerce/tickets").status_code == 403


def test_disabling_a_user_stops_an_active_browser(console: Console) -> None:
    _, admin = sign_in(console.store, "admin")
    customer = console.login("customer")
    session = customer.post(
        "/commerce/consult",
        json={"request_id": "r-1", "query": "price", "skus": [SKU]},
    )
    assert session.status_code == 200, session.text
    console.store.disable_user(actor=admin, user_id=console.users["customer"])
    rejected = customer.post(
        "/commerce/consult",
        json={"request_id": "r-2", "query": "price", "skus": [SKU]},
    )
    assert rejected.status_code == 401


@pytest.mark.parametrize("operation", ["invite", "grant", "revoke", "disable"])
@pytest.mark.parametrize("actor_state", ["revoked", "asserted_admin"])
def test_member_mutations_revalidate_actor_inside_the_transaction(
    accounts: tuple[IdentityStore, dict[str, str]], operation: str, actor_state: str
) -> None:
    store, users = accounts
    _, actor = sign_in(store, "admin")
    if actor_state == "revoked":
        store.add_user(
            workspace_id=WORKSPACE,
            email=email_for("admin", ".second"),
            password=PASSWORD,
            display_name="Second Administrator",
            role="admin",
        )
        _, current_admin = sign_in(store, "admin", suffix=".second")
        store.disable_user(actor=current_admin, user_id=actor.user_id)
    else:
        _, customer = sign_in(store, "customer")
        actor = replace(customer, roles=("admin",))

    with pytest.raises(ScopeError):
        if operation == "invite":
            store.invite(actor=actor, email="denied@example.test", role="supervisor")
        elif operation == "grant":
            store.grant_role(actor=actor, user_id=users["support"], role="supervisor")
        elif operation == "revoke":
            store.revoke_membership(actor=actor, user_id=users["support"], role="support")
        else:
            store.disable_user(actor=actor, user_id=users["support"])

    assert store.current_principal(user_id=users["support"], workspace_id=WORKSPACE).roles == (
        "support",
    )
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM invitations").fetchone()[0] == 0


@pytest.mark.parametrize("operation", ["disable", "revoke"])
def test_member_management_never_revokes_another_workspaces_credentials(
    accounts: tuple[IdentityStore, dict[str, str]], operation: str
) -> None:
    store, users = accounts
    other = store.create_workspace("Other Workspace")
    # Valid shared-account fixture: platform users are global and memberships
    # carry the workspace. Tenant management must not mutate a different row.
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "INSERT INTO memberships(workspace_id,user_id,role,active,created_at) "
            "VALUES(?,?,?,1,?)",
            (other, users["supervisor"], "supervisor", "2026-10-28T00:00:00+00:00"),
        )
    _, admin = sign_in(store, "admin")
    here, _ = sign_in(store, "supervisor")
    elsewhere = store.login(email=email_for("supervisor"), password=PASSWORD, workspace_id=other)
    here_approval = store.approve_session(
        email=email_for("supervisor"), password=PASSWORD, workspace_id=WORKSPACE
    )
    elsewhere_approval = store.approve_session(
        email=email_for("supervisor"), password=PASSWORD, workspace_id=other
    )

    if operation == "disable":
        store.disable_user(actor=admin, user_id=users["supervisor"])
    else:
        store.revoke_membership(actor=admin, user_id=users["supervisor"], role="supervisor")

    with pytest.raises(AuthenticationError):
        store.authenticate(here)
    with pytest.raises(AuthenticationError):
        store.authenticate_approver(here_approval.token)
    assert store.authenticate(elsewhere.token).workspace_id == other
    assert store.authenticate_approver(elsewhere_approval.token).workspace_id == other
    assert (
        store.login(
            email=email_for("supervisor"), password=PASSWORD, workspace_id=other
        ).principal.user_id
        == users["supervisor"]
    )


def test_approval_and_login_share_password_failure_throttling(console: Console) -> None:
    client = TestClient(console.app)
    credentials = {
        "email": email_for("supervisor"),
        "password": "wrong-password",
        "workspace_id": WORKSPACE,
    }
    paths = ["/identity/login"] * 3 + ["/identity/approval"] * 2
    assert [client.post(path, json=credentials).status_code for path in paths] == [401] * 5
    credentials["password"] = PASSWORD
    assert client.post("/identity/approval", json=credentials).status_code == 429
    assert client.post("/identity/login", json=credentials).status_code == 429
    audit = console.store.audit_tail()
    assert (
        sum(row["action"] == "approval.session" and row["outcome"] == "denied" for row in audit)
        == 2
    )
    assert any(
        row["action"] == "approval.session" and row["outcome"] == "throttled" for row in audit
    )
    console.clock.advance(timedelta(minutes=15, seconds=1))
    assert client.post("/identity/approval", json=credentials).status_code == 200


def test_concurrent_login_and_approval_cannot_read_a_stale_failure_counter(
    accounts: tuple[IdentityStore, dict[str, str]],
) -> None:
    store, _ = accounts

    def attempt(index: int) -> type[ScopeError]:
        authenticate = store.login if index % 2 else store.approve_session
        try:
            authenticate(
                email=email_for("supervisor"),
                password="incorrect password",
                workspace_id=WORKSPACE,
            )
        except ScopeError as exc:
            return type(exc)
        raise AssertionError("incorrect password was accepted")

    with ThreadPoolExecutor(max_workers=8) as executor:
        outcomes = list(executor.map(attempt, range(8)))
    assert outcomes.count(AuthenticationError) == 5
    assert outcomes.count(LoginThrottled) == 3


@pytest.mark.parametrize("path", ["/identity/logout", "/commerce/consult"])
def test_revocation_during_csrf_validation_returns_401_instead_of_500(
    console: Console, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    client = console.login("customer")

    def revoked(*, token: str, csrf: str) -> bool:
        del token, csrf
        raise AuthenticationError("credential is not valid")

    monkeypatch.setattr(console.store, "verify_csrf", revoked)
    denied = client.post(path, json={"request_id": "denied", "query": "price"})
    assert denied.status_code == 401
    assert denied.json()["detail"] == "authentication required"


def test_logout_clears_browser_cookie_and_revokes_its_server_session(console: Console) -> None:
    client = console.login("customer")
    token = client.cookies[COOKIE_NAME]
    response = client.post("/identity/logout")
    assert response.status_code == 204
    assert "max-age=0" in response.headers["set-cookie"].lower()
    assert COOKIE_NAME not in client.cookies
    with pytest.raises(AuthenticationError):
        console.store.authenticate(token)


def test_nested_identity_dependencies_and_openapi_are_resolved(console: Console) -> None:
    client = console.login("admin")
    schema = client.get("/openapi.json")
    assert schema.status_code == 200, schema.text
    me = client.get("/identity/me")
    assert me.status_code == 200, me.text
    assert me.json()["user_id"] == console.users["admin"]
    invitation = client.post(
        "/identity/invitations", json={"email": "new.reader@example.test", "role": "customer"}
    )
    assert invitation.status_code == 201, invitation.text
    accepted = TestClient(console.app).post(
        f"/identity/invitations/{invitation.json()['invitation_token']}/accept",
        json={"password": PASSWORD, "display_name": "Invited Reader"},
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["roles"] == ["customer"]


def test_me_reports_a_concurrent_logout_as_401(
    console: Console, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = console.login("customer")

    def logged_out(token: str) -> str:
        console.store.logout(token=token)
        raise AuthenticationError("credential is not valid")

    monkeypatch.setattr(console.store, "session_csrf", logged_out)
    response = client.get("/identity/me")
    assert response.status_code == 401
