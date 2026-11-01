"""HTTP adapter contracts; real PG/wheel/browser coverage is a separate gate."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from typing import Any, cast

import httpx
import pytest

from forgeharness.cloud.control import PlatformRuntime
from forgeharness.cloud.control_api import COOKIE_NAME, create_control_app
from forgeharness.cloud.identity import PostgresIdentityStore
from forgeharness.cloud.identity_models import Principal, SessionCredentials
from forgeharness.cloud.models import CloudConflict, CloudDenied
from forgeharness.cloud.workbench import WORKBENCH_HTML

ORIGIN = "https://alpha.test"
ORG = "/v1/organizations/org-a"
SPACE = ORG + "/workspaces/space-a"
FINGERPRINT = "a" * 64


class Identity:
    def __init__(self) -> None:
        self.principal = Principal("acct_a", "alpha@example.test", "org-a", "session-a")
        self.token = "session-secret-a"
        self.csrf = "csrf-secret-a"
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.error: Exception | None = None

    def credentials(self) -> SessionCredentials:
        return SessionCredentials(
            token=self.token,
            csrf_token=self.csrf,
            principal=self.principal,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )

    async def authenticate(self, token: str) -> Principal:
        if token != self.token:
            raise CloudDenied("private session failure")
        return self.principal

    async def check_csrf(self, token: str, csrf: str) -> Principal:
        principal = await self.authenticate(token)
        if csrf != self.csrf:
            raise CloudDenied("private csrf failure")
        return principal

    async def session_csrf(self, token: str) -> str:
        await self.authenticate(token)
        return self.csrf

    async def login(self, email: str, password: str) -> SessionCredentials:
        if password != "valid-test-password":
            raise CloudDenied("private credential failure " + email)
        return self.credentials()

    async def accept_invitation(
        self, token: str, password: str | None, existing_session: str | None
    ) -> SessionCredentials:
        self.calls.append(("accept", (token, password, existing_session)))
        if token != "valid-invitation-token":
            raise CloudDenied("raw-invitation-secret " + token)
        return self.credentials()

    async def logout(self, token: str) -> None:
        self.calls.append(("logout", (token,)))
        self.token = "rotated-after-logout"

    async def switch_organization(self, token: str, org_id: str) -> SessionCredentials:
        await self.authenticate(token)
        self.calls.append(("select", (org_id,)))
        return self.credentials()

    async def list_organizations(self, principal: Principal) -> list[dict[str, str]]:
        return [{"org_id": "org-a", "role": "owner"}]

    async def list_workspaces(self, *args: Any) -> list[dict[str, str]]:
        self.calls.append(("workspaces", args))
        return [{"workspace_id": "space-a", "name": "Space A"}]

    async def list_deployments(self, *args: Any) -> list[dict[str, Any]]:
        self.calls.append(("deployments", args))
        return [{"deployment_id": "deployed-a", "can_use": True, "can_approve": False}]

    async def list_members(self, *args: Any) -> list[dict[str, str]]:
        self.calls.append(("members", args))
        if self.error:
            raise self.error
        return [{"account_id": "acct_a", "email": "alpha@example.test", "role": "owner"}]

    async def invite(self, *args: Any) -> str:
        self.calls.append(("invite", args))
        return "new-one-time-secret"

    async def list_invitations(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("invitations", args))
        assert kwargs == {"limit": 20, "cursor": None}
        return {"items": [{"invitation_id": "invitation-a"}], "next_cursor": None}

    async def revoke_invitation(self, *args: Any) -> None:
        self.calls.append(("revoke_invitation", args))

    async def set_organization(self, *args: Any) -> None:
        self.calls.append(("set_organization", args))

    async def set_member(self, *args: Any) -> None:
        self.calls.append(("set_member", args))

    async def put_workspace(self, *args: Any) -> None:
        self.calls.append(("put_workspace", args))

    async def set_workspace_member(self, *args: Any) -> None:
        self.calls.append(("workspace_member", args))

    async def put_deployment(self, *args: Any) -> None:
        self.calls.append(("put_deployment", args))

    async def set_deployment_grant(self, *args: Any) -> None:
        self.calls.append(("grant", args))


class Runtime:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.run = {
            "run_id": "run-a",
            "workspace_id": "space-a",
            "deployment_id": "deployed-a",
            "status": "queued",
            "cancel_requested": False,
            "used_units": 0,
            "reserved_units": 0,
            "result": None,
            "error": None,
            "checkpoint": {"input": "not-public"},
            "trace": "not-public",
        }
        self.proposal = {
            "run_id": "run-a",
            "fingerprint": FINGERPRINT,
            "proposal": {"tool": "write", "arguments": {"value": "precise-action"}},
        }

    def registered_deployment(self, deployment_id: str) -> dict[str, str]:
        assert deployment_id == "deployed-a"
        return {"application_id": "application-a", "binding_digest": "b" * 64}

    async def accept(self, *args: Any) -> dict[str, Any]:
        self.calls.append(("accept", args, {}))
        return self.run

    async def inspect(self, *args: Any) -> dict[str, Any]:
        self.calls.append(("inspect", args, {}))
        return self.run

    async def list_runs(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("list", args, kwargs))
        return {"items": [self.run], "next_cursor": "run-next"}

    async def cancel(self, *args: Any) -> dict[str, Any]:
        self.calls.append(("cancel", args, {}))
        return {**self.run, "cancel_requested": True}

    async def pending_approvals(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("pending", args, kwargs))
        return {"items": [self.proposal], "next_cursor": None}

    async def approval_detail(self, *args: Any) -> dict[str, Any]:
        self.calls.append(("proposal", args, {}))
        return self.proposal

    async def decide(self, *args: Any) -> dict[str, Any]:
        self.calls.append(("decide", args, {}))
        return {"run_id": args[3], "decision": args[4], "fingerprint": args[5]}


def app(identity: Identity, runtime: Runtime, *, secure_cookies: bool = True):
    return create_control_app(
        cast(PostgresIdentityStore, identity),
        cast(PlatformRuntime, runtime),
        allowed_origins=(ORIGIN,),
        allowed_hosts=("alpha.test",),
        secure_cookies=secure_cookies,
    )


def client(
    identity: Identity, runtime: Runtime, *, authenticated: bool = True
) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(identity, runtime)),
        base_url=ORIGIN,
        headers={"Origin": ORIGIN, "X-CSRF-Token": identity.csrf},
        cookies={COOKIE_NAME: identity.token} if authenticated else {},
    )


async def test_cookie_login_logout_invitation_and_organization_rotation() -> None:
    identity, runtime = Identity(), Runtime()
    async with client(identity, runtime, authenticated=False) as http:
        response = await http.post(
            "/v1/login",
            json={
                "email": "alpha@example.test",
                "password": "valid-test-password",
            },
        )
        assert response.status_code == 200
        cookie = response.headers["set-cookie"]
        for attribute in ("HttpOnly", "Secure", "SameSite=strict", "Path=/", "Max-Age="):
            assert attribute in cookie
        assert response.json() == {
            "account_id": "acct_a",
            "email": "alpha@example.test",
            "org_id": "org-a",
            "csrf_token": identity.csrf,
        }
        assert identity.token not in response.text
        assert (await http.get("/v1/me")).json()["csrf_token"] == identity.csrf
        assert (await http.get("/v1/organizations")).json()["items"][0]["org_id"] == "org-a"
        response = await http.post(ORG + "/select")
        assert response.status_code == 200
        assert ("select", ("org-a",)) in identity.calls
        response = await http.post(
            "/v1/invitations/accept", json={"token": "valid-invitation-token"}
        )
        assert response.status_code == 200
        assert ("accept", ("valid-invitation-token", None, identity.token)) in identity.calls
        response = await http.post("/v1/logout")
        assert response.status_code == 204
        assert "Max-Age=0" in response.headers["set-cookie"]
        assert (await http.get("/v1/me")).status_code == 401
    async with client(identity, runtime, authenticated=False) as http:
        http.cookies.set(COOKIE_NAME, "expired-stale-session")
        response = await http.post(
            "/v1/invitations/accept",
            json={
                "token": "valid-invitation-token",
                "password": "new-test-password",
            },
        )
        assert response.status_code == 200
        assert identity.calls[-1] == (
            "accept",
            ("valid-invitation-token", "new-test-password", None),
        )


async def test_management_delegates_current_principal_and_server_only_binding() -> None:
    identity, runtime = Identity(), Runtime()
    async with client(identity, runtime) as http:
        for path in ("/members", "/workspaces", "/workspaces/space-a/deployments"):
            assert (await http.get(ORG + path)).status_code == 200
        response = await http.post(ORG + "/invitations", json={"email": "new@example.test"})
        assert response.status_code == 201
        assert response.json() == {"invitation_token": "new-one-time-secret"}
        assert (await http.get(ORG + "/invitations")).json()["items"] == [
            {"invitation_id": "invitation-a"}
        ]
        assert (await http.post(ORG + "/invitations/invitation-a/revoke")).status_code == 204
        assert identity.calls[-1] == (
            "revoke_invitation",
            (identity.principal, "org-a", "invitation-a"),
        )
        assert (await http.put(ORG + "/active", json={"active": False})).status_code == 204
        assert identity.calls[-1] == ("set_organization", (identity.principal, "org-a", False))
        cases = [
            ("/members/acct_b", {"role": "member", "active": False}, "set_member"),
            ("/workspaces/space-a", {"name": "Space A"}, "put_workspace"),
            ("/workspaces/space-a/members/acct_b", {"active": True}, "workspace_member"),
            ("/workspaces/space-a/deployments/deployed-a", {"active": True}, "put_deployment"),
            ("/deployments/deployed-a/grants/acct_b", {"can_approve": True}, "grant"),
        ]
        for path, body, name in cases:
            assert (await http.put(ORG + path, json=body)).status_code == 204
            assert identity.calls[-1][0] == name
            assert identity.calls[-1][1][0] is identity.principal
        deployment = next(args for name, args in identity.calls if name == "put_deployment")
        assert deployment[2:] == (
            "deployed-a",
            "space-a",
            "application-a",
            "b" * 64,
            True,
        )
        response = await http.put(
            SPACE + "/deployments/deployed-a",
            json={
                "active": True,
                "binding_digest": "attacker-binding",
                "import_path": "evil:code",
            },
        )
        assert response.status_code == 422
        assert "attacker-binding" not in response.text


async def test_run_and_approval_routes_are_async_minimal_and_fingerprint_bound() -> None:
    identity, runtime = Identity(), Runtime()
    async with client(identity, runtime) as http:
        response = await http.post(
            SPACE + "/runs",
            headers={"Idempotency-Key": "request-a"},
            json={"deployment_id": "deployed-a", "task": "task text"},
        )
        assert response.status_code == 202
        assert runtime.calls[-1] == (
            "accept",
            (identity.principal, "org-a", "space-a", "deployed-a", "task text", "request-a"),
            {},
        )
        assert "checkpoint" not in response.json() and "not-public" not in response.text
        response = await http.get(SPACE + "/runs?limit=2&cursor=run-previous")
        assert response.status_code == 200
        assert response.json()["next_cursor"] == "run-next"
        assert "not-public" not in response.text
        assert runtime.calls[-1][2] == {"limit": 2, "cursor": "run-previous"}
        assert (await http.get(SPACE + "/runs/run-a")).status_code == 200
        assert (await http.post(SPACE + "/runs/run-a/cancel")).json()["cancel_requested"]
        assert (await http.get(SPACE + "/approvals?limit=1")).status_code == 200
        response = await http.get(SPACE + "/runs/run-a/approval")
        assert response.json()["proposal"]["arguments"]["value"] == "precise-action"
        for decision in ("approve", "reject"):
            response = await http.post(
                SPACE + "/runs/run-a/" + decision, json={"fingerprint": FINGERPRINT}
            )
            assert response.json()["decision"] == decision
            assert runtime.calls[-1][1][-2:] == (decision, FINGERPRINT)
        assert (await http.post(SPACE + "/runs/run-a/advance")).status_code == 404
        assert not any(name == "advance" for name, _, _ in runtime.calls)


async def test_origin_csrf_host_input_and_authentication_boundaries() -> None:
    identity, runtime = Identity(), Runtime()
    async with client(identity, runtime, authenticated=False) as http:
        assert (await http.get("/v1/me")).status_code == 401
        assert (
            await http.post("/v1/login", json={"email": "ghost@example.test", "password": "bad"})
        ).json() == {
            "error": "authentication_required",
        }
        assert (
            await http.post("/v1/invitations/accept", json={"token": "invalid-token-secret"})
        ).status_code == 401
    async with client(identity, runtime) as http:
        response = await http.post(
            SPACE + "/runs", headers={"Origin": "https://evil.test"}, json={}
        )
        assert response.status_code == 403
        assert (
            await http.post(SPACE + "/runs", headers={"X-CSRF-Token": "wrong"}, json={})
        ).status_code == 403
        assert (
            await http.post(
                "/v1/invitations/accept",
                headers={"X-CSRF-Token": "wrong"},
                json={"token": "valid-invitation-token"},
            )
        ).status_code == 403
        assert (await http.get("/v1/me", headers={"Host": "evil.test"})).status_code == 400
        for body in (
            {"deployment_id": "deployed-a", "task": "ok", "actor_id": "owner"},
            {"deployment_id": "deployed-a", "task": "   "},
        ):
            response = await http.post(
                SPACE + "/runs", headers={"Idempotency-Key": "request-a"}, json=body
            )
            assert response.status_code == 422
            assert "owner" not in response.text
        assert (
            await http.post(SPACE + "/runs", json={"deployment_id": "deployed-a", "task": "ok"})
        ).status_code == 422
        for query in ("limit=0", "limit=101", "cursor=" + "x" * 129):
            assert (await http.get(SPACE + "/runs?" + query)).status_code == 422
        assert (await http.get("/v1/organizations/org-b/workspaces")).status_code == 404
        assert not runtime.calls
        assert (
            await http.post(SPACE + "/runs/run-a/approve", json={"fingerprint": "wrong"})
        ).status_code == 422


@pytest.mark.parametrize(
    "error,status",
    [
        (CloudDenied("SQL password=raw-secret"), 404),
        (CloudConflict("DSN=raw-secret"), 409),
        (ValueError("raw-secret"), 422),
        (RuntimeError("postgresql://raw-secret"), 500),
    ],
)
async def test_private_errors_are_normalized_and_never_cached(
    error: Exception, status: int
) -> None:
    identity, runtime = Identity(), Runtime()
    identity.error = error
    async with client(identity, runtime) as http:
        response = await http.get(ORG + "/members")
        assert response.status_code == status
        assert "raw-secret" not in response.text
        assert response.headers["cache-control"] == "no-store"


async def test_request_bodies_are_bounded_before_json_validation_including_chunked() -> None:
    identity, runtime = Identity(), Runtime()

    async def chunks():
        for _ in range(4):
            yield b"private-untrusted-input-" * 4000

    async with client(identity, runtime) as http:
        for content in (b"private-input-" * 20_000, chunks()):
            response = await http.post("/v1/login", content=content)
            assert response.status_code == 413
            assert response.json() == {"error": "request_too_large"}
            assert response.headers["cache-control"] == "no-store"
        http.headers.pop("Origin")
        assert (await http.post("/v1/login", json={})).status_code == 403


def test_sensitive_forms_submit_secrets_in_body_without_javascript() -> None:
    class FormContract(HTMLParser):
        def __init__(self):
            super().__init__()
            self.forms = {}
            self.controls = []
            self.current_form = None

        def handle_starttag(self, tag, attrs):
            attributes = dict(attrs)
            if tag == "form":
                self.current_form = attributes["id"]
                self.forms[self.current_form] = attributes
            elif tag in {"input", "button", "textarea"}:
                self.controls.append((attributes.get("form", self.current_form), attributes))

        def handle_endtag(self, tag):
            if tag == "form":
                self.current_form = None

    parsed = FormContract()
    parsed.feed(WORKBENCH_HTML)
    sensitive_forms = {
        form for form, attrs in parsed.controls if attrs.get("name") in {"password", "token"}
    }
    targets = {"login-form": "/v1/login", "accept-form": "/v1/invitations/accept"}
    assert sensitive_forms == targets.keys()
    for form_id, action in targets.items():
        form = parsed.forms[form_id]
        assert form["method"].casefold() == "post"
        assert form["action"] == action
        for associated_form, attrs in parsed.controls:
            if associated_form == form_id:
                assert attrs.get("formmethod", "post").casefold() == "post"
                assert attrs.get("formaction", action) == action


async def test_page_assets_csp_and_explicit_cors() -> None:
    identity, runtime = Identity(), Runtime()
    async with client(identity, runtime) as http:
        response = await http.get("/")
        assert response.status_code == 200
        assert '<script src="/assets/workbench.js" defer>' in response.text
        assert response.text.count('maxlength="1024"') == 2
        assert "5 分钟" in response.text and "请取消原运行并提交新运行" in response.text
        assert "需管理员离线恢复" in response.text
        assert "unsafe-inline" not in response.headers["content-security-policy"]
        assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
        script = await http.get("/assets/workbench.js")
        assert script.status_code == 200
        assert "textContent" in script.text and "innerHTML" not in script.text
        assert "localStorage" not in script.text
        assert (await http.get("/assets/workbench.css")).status_code == 200
        schema = (await http.get("/openapi.json")).json()
        assert "TenantContext" not in str(schema)
        assert "actor_id" not in str(schema)
        response = await http.options(
            "/v1/login",
            headers={
                "Origin": ORIGIN,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "Content-Type,X-CSRF-Token",
            },
        )
        assert response.headers["access-control-allow-origin"] == ORIGIN
        assert response.headers["access-control-allow-credentials"] == "true"
        response = await http.options(
            "/v1/login",
            headers={
                "Origin": "https://evil.test",
                "Access-Control-Request-Method": "POST",
            },
        )
        assert "access-control-allow-origin" not in response.headers


@pytest.mark.parametrize(
    "origins,hosts",
    [
        ((), ("alpha.test",)),
        ((ORIGIN,), ()),
        (("https://*.test",), ("alpha.test",)),
        ((ORIGIN + "/path",), ("alpha.test",)),
        (("ftp://alpha.test",), ("alpha.test",)),
        (("https://user:pass@alpha.test",), ("alpha.test",)),
        ((ORIGIN,), ("*.test",)),
        ((ORIGIN,), ("alpha.test:80",)),
        (("http://alpha.test:invalid",), ("alpha.test",)),
    ],
)
def test_trust_configuration_is_explicit(origins: tuple[str, ...], hosts: tuple[str, ...]) -> None:
    with pytest.raises(ValueError):
        create_control_app(
            cast(PostgresIdentityStore, Identity()),
            cast(PlatformRuntime, Runtime()),
            allowed_origins=origins,
            allowed_hosts=hosts,
        )
