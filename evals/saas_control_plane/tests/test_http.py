"""Real TCP HTTP -> identity PG -> governed runtime -> independent oracle."""

from __future__ import annotations

import asyncio
import socket
from contextlib import asynccontextmanager

import httpx
import pytest
import uvicorn

from forgeharness.cloud.control_api import COOKIE_NAME, create_control_app
from forgeharness.cloud.runtime import HostedRuntime

ORG = "/v1/organizations/org-a"
SPACE = ORG + "/workspaces/work-a"


@pytest.fixture
async def httpcase(controlcase):
    case = controlcase
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.setblocking(False)
    origin = "http://127.0.0.1:" + str(listener.getsockname()[1])
    app = create_control_app(
        case.identity,
        case.runtime,
        allowed_hosts=("127.0.0.1",),
        allowed_origins=(origin,),
        secure_cookies=False,
    )
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            log_level="critical",
            access_log=False,
            lifespan="off",
        )
    )
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(5):
            while not server.started:
                if task.done():
                    await task
                    raise RuntimeError("loopback server did not start")
                await asyncio.sleep(0.01)
        yield case, origin
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=5)
        listener.close()


@asynccontextmanager
async def session(origin, name):
    password = "Owner-passphrase-1" if name == "owner" else "Strong-passphrase-1"
    async with httpx.AsyncClient(
        base_url=origin, headers={"Origin": origin}, timeout=10, trust_env=False
    ) as client:
        response = await client.post(
            "/v1/login",
            json={
                "email": name + "@example.test",
                "password": password,
            },
        )
        assert response.status_code == 200
        client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
        yield client


async def submit(client, case, *, key="http-request-1", task="Update the synthetic record"):
    return await client.post(
        SPACE + "/runs",
        headers={"Idempotency-Key": key},
        json={
            "deployment_id": case.registration.deployment_id,
            "task": task,
        },
    )


def worker(case):
    return HostedRuntime(case.pg.store, {case.registration.deployment_id: case.registration})


async def test_real_http_authentication_csrf_origin_cookie_logout_and_secret_storage(httpcase):
    case, origin = httpcase
    async with httpx.AsyncClient(
        base_url=origin, headers={"Origin": origin}, trust_env=False
    ) as http:
        for email in ("unknown@example.test", "user@example.test"):
            response = await http.post("/v1/login", json={"email": email, "password": "wrong"})
            assert response.status_code == 401
            assert response.json() == {"error": "authentication_required"}
        response = await http.post("/v1/login", headers={"Origin": "https://foreign.test"}, json={})
        assert response.status_code == 403
        response = await http.post(
            "/v1/login",
            json={
                "email": "user@example.test",
                "password": "Strong-passphrase-1",
            },
        )
        assert response.status_code == 200
        cookie = response.headers["set-cookie"]
        assert "HttpOnly" in cookie and "SameSite=strict" in cookie
        token = http.cookies.get(COOKIE_NAME)
        assert token and token not in response.text
        csrf = response.json()["csrf_token"]
        assert (await http.get("/v1/me")).json()["csrf_token"] == csrf
        response = await submit(http, case)
        assert response.status_code == 403
        http.headers["X-CSRF-Token"] = csrf
        assert (await submit(http, case)).status_code == 202
        for table in ("accounts", "sessions", "invitations", "identity_audit", "outbox"):
            rows = await case.pg.admin.fetch(f"SELECT row_to_json(t)::text FROM fh_cloud.{table} t")
            text = " ".join(row[0] for row in rows)
            assert token not in text and "Strong-passphrase-1" not in text and csrf not in text
        assert (await http.post("/v1/logout")).status_code == 204
        assert (await http.get("/v1/me")).status_code == 401
        http.cookies.set(COOKIE_NAME, token)
        assert (await http.get("/v1/me")).status_code == 401


async def test_real_http_atomic_async_acceptance_owner_isolation_and_path_validation(httpcase):
    case, origin = httpcase
    async with session(origin, "user") as user, session(origin, "peer") as peer:
        responses = await asyncio.gather(submit(user, case), submit(user, case))
        assert all(response.status_code == 202 for response in responses)
        run_id = responses[0].json()["run_id"]
        assert responses[1].json()["run_id"] == run_id
        assert not case.fixture.ledger.all_receipts()
        assert await case.pg.admin.fetchval("SELECT count(*) FROM fh_cloud.execution_calls") == 0
        assert await case.pg.admin.fetchval("SELECT count(*) FROM fh_cloud.run_bindings") == 1
        assert (await submit(user, case, task="changed task")).status_code == 409
        response = await user.get(SPACE + "/runs/" + run_id)
        assert response.status_code == 200
        assert response.json()["status"] == "queued"
        assert "checkpoint" not in response.json() and "task" not in response.json()
        assert (await peer.get(SPACE + "/runs/" + run_id)).status_code == 404
        assert (await user.get(ORG + "/workspaces/foreign/runs/" + run_id)).status_code == 404
        assert (
            await user.get("/v1/organizations/org-b/workspaces/work-a/runs/" + run_id)
        ).status_code == 404
        assert (await user.get(SPACE + "/runs")).json()["items"][0]["run_id"] == run_id
        assert (await peer.get(SPACE + "/runs")).json()["items"] == []
        assert (await user.post(SPACE + "/runs/" + run_id + "/advance")).status_code == 404
        for field, value in (
            ("actor_id", "service-worker"),
            ("max_units", 10**9),
            ("scope", {"tenant_id": "org-b"}),
            ("import_path", "evil:app"),
        ):
            response = await user.post(
                SPACE + "/runs",
                headers={"Idempotency-Key": "forged"},
                json={
                    "deployment_id": case.registration.deployment_id,
                    "task": "task",
                    field: value,
                },
            )
            assert response.status_code == 422
        assert (await user.get(SPACE + "/runs?limit=101")).status_code == 422
        assert (await user.get(SPACE + "/runs?cursor=invalid")).status_code == 422


async def test_real_http_independent_review_exact_fingerprint_and_competing_decisions(httpcase):
    case, origin = httpcase
    async with (
        session(origin, "user") as user,
        session(origin, "reviewer") as reviewer,
        session(origin, "owner") as admin,
    ):
        receipt = await submit(user, case)
        run_id = receipt.json()["run_id"]
        path = SPACE + "/runs/" + run_id
        pending = await worker(case).advance(case.worker, run_id)
        assert pending.run.status == "awaiting_approval"
        assert (await admin.get(path + "/approval")).status_code == 404
        assert (await user.get(path + "/approval")).status_code == 404
        queue = (await reviewer.get(SPACE + "/approvals")).json()
        detail = (await reviewer.get(path + "/approval")).json()
        assert queue["items"][0]["fingerprint"] == detail["fingerprint"]
        assert detail["proposal"] == pending.checkpoint.pending_approval.call.model_dump(
            mode="json"
        )
        wrong = "f" * 64 if detail["fingerprint"] != "f" * 64 else "e" * 64
        assert (
            await reviewer.post(path + "/approve", json={"fingerprint": wrong})
        ).status_code == 409
        responses = await asyncio.gather(
            *(
                reviewer.post(path + "/" + decision, json={"fingerprint": detail["fingerprint"]})
                for decision in ("approve", "reject")
            )
        )
        assert sorted(response.status_code for response in responses) == [200, 409]
        decision = next(
            response.json()["decision"] for response in responses if response.status_code == 200
        )
        assert (
            await reviewer.post(path + "/" + decision, json={"fingerprint": detail["fingerprint"]})
        ).status_code == 200
        result = await worker(case).advance(case.worker, run_id)
        assert result.run.status == ("succeeded" if decision == "approve" else "cancelled")
        assert case.fixture.ledger.effect_count("orders") == (1 if decision == "approve" else 0)
        assert (await user.get(path)).json()["reserved_units"] == 0
        assert (await reviewer.get(SPACE + "/approvals")).json()["items"] == []


async def test_real_http_member_revocation_blocks_live_session_and_queued_send(httpcase):
    case, origin = httpcase
    async with session(origin, "user") as user, session(origin, "owner") as owner:
        receipt = await submit(user, case)
        run_id = receipt.json()["run_id"]
        response = await owner.put(
            ORG + "/members/" + case.user.principal.account_id,
            json={"role": "member", "active": False},
        )
        assert response.status_code == 204
        assert (await user.get("/v1/me")).status_code == 401
        assert (await submit(user, case, key="revoked")).status_code == 401
        result = await worker(case).advance(case.worker, run_id)
        assert result.run.status == "failed"
        assert not case.fixture.ledger.all_receipts()
        assert not result.run.used_units and not result.run.reserved_units


async def test_real_http_invitation_creation_paging_revocation_and_replay(httpcase):
    _case, origin = httpcase
    async with session(origin, "owner") as owner, session(origin, "user") as user:
        response = await owner.post(ORG + "/invitations", json={"email": "new@example.test"})
        assert response.status_code == 201
        invitation = response.json()["invitation_token"]
        response = await owner.get(ORG + "/invitations?limit=1")
        assert response.status_code == 200
        listing = response.json()
        pages = listing["items"]
        while listing["next_cursor"]:
            listing = (
                await owner.get(
                    ORG + "/invitations",
                    params={
                        "limit": 1,
                        "cursor": listing["next_cursor"],
                    },
                )
            ).json()
            pages.extend(listing["items"])
        assert len({item["invitation_id"] for item in pages}) == len(pages)
        assert invitation not in str(pages) and "token_digest" not in str(pages)
        item = next(item for item in pages if item["email"] == "new@example.test")
        path = ORG + "/invitations/" + item["invitation_id"] + "/revoke"
        assert (await user.get(ORG + "/invitations")).status_code == 404
        assert (await user.post(path)).status_code == 404
        assert (await owner.post(path)).status_code == 204
        async with httpx.AsyncClient(
            base_url=origin, headers={"Origin": origin}, trust_env=False
        ) as anonymous:
            response = await anonymous.post(
                "/v1/invitations/accept",
                json={
                    "token": invitation,
                    "password": "New-password-strong",
                },
            )
            assert response.status_code == 401
        response = await owner.post(ORG + "/invitations", json={"email": "accepted@example.test"})
        invitation = response.json()["invitation_token"]
        async with httpx.AsyncClient(
            base_url=origin, headers={"Origin": origin}, trust_env=False
        ) as anonymous:
            body = {"token": invitation, "password": "New-password-strong"}
            anonymous.cookies.set(COOKIE_NAME, "idle-expired-cookie")
            response = await anonymous.post("/v1/invitations/accept", json=body)
            assert response.status_code == 200
            anonymous.headers["X-CSRF-Token"] = response.json()["csrf_token"]
            assert (await anonymous.post("/v1/invitations/accept", json=body)).status_code == 401


async def test_real_http_existing_account_accepts_cross_org_without_password_reset(httpcase):
    case, origin = httpcase
    other_owner = await case.identity.bootstrap(
        "org-b", "second-owner@example.test", "Second-owner-password"
    )
    invitation = await case.identity.invite(other_owner.principal, "org-b", "user@example.test")
    async with session(origin, "user") as user:
        before = (await user.get("/v1/me")).json()
        old_cookie = user.cookies.get(COOKIE_NAME)
        body = {"token": invitation}
        assert (
            await user.post("/v1/invitations/accept", headers={"X-CSRF-Token": "wrong"}, json=body)
        ).status_code == 403
        assert (
            await user.post(
                "/v1/invitations/accept",
                json={
                    **body,
                    "password": "Forbidden-new-password",
                },
            )
        ).status_code == 401
        response = await user.post("/v1/invitations/accept", json=body)
        assert response.status_code == 200
        assert response.json()["account_id"] == before["account_id"]
        assert response.json()["org_id"] == "org-b"
        user.headers["X-CSRF-Token"] = response.json()["csrf_token"]
        organizations = (await user.get("/v1/organizations")).json()["items"]
        assert {item["org_id"] for item in organizations} == {"org-a", "org-b"}
        # The pre-existing org-a session remains valid: joining a different org
        # must not revoke unrelated organizational access or reset credentials.
        async with httpx.AsyncClient(
            base_url=origin, trust_env=False, cookies={COOKIE_NAME: old_cookie}
        ) as previous:
            assert (await previous.get("/v1/me")).json()["org_id"] == "org-a"
    async with session(origin, "user") as user:
        assert (await user.get("/v1/me")).json()["account_id"] == before["account_id"]


async def test_real_http_owner_protection_organization_disable_and_body_limit(httpcase):
    case, origin = httpcase
    async with session(origin, "owner") as owner, session(origin, "user") as user:
        response = await owner.put(
            ORG + "/members/" + case.owner.principal.account_id,
            json={"role": "admin", "active": True},
        )
        assert response.status_code == 409
        assert (await user.put(ORG + "/active", json={"active": False})).status_code == 404
        response = await owner.post("/v1/login", content=b"secret-too-large-" * 10_000)
        assert response.status_code == 413
        assert "secret-too-large" not in response.text
        assert (await owner.put(ORG + "/active", json={"active": False})).status_code == 204
        assert (await user.get("/v1/me")).status_code == 401
        assert (await owner.get("/v1/me")).status_code == 401
