"""HTTP session contracts use real durable services and host authentication."""

from collections.abc import Awaitable, Callable
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI, HTTPException, Request
from test_sdk import Echo, Policy, app, context

from forgeharness.sdk import (
    Actor,
    FinalAction,
    ModelRequest,
    ModelResult,
    ResourceScope,
    RunContext,
    ScriptedModel,
    ToolAction,
    ToolCall,
)
from forgeharness.sdk.session_control import session_router
from forgeharness.sdk.sessions import SessionOperation, SessionService


class Host:
    def __init__(self, identity: RunContext | None = None) -> None:
        self.identity = identity or context()
        self.calls = 0

    async def authenticate(self, request: Request) -> RunContext:
        self.calls += 1
        if request.headers.get("authorization") != "trusted-test":
            raise HTTPException(401, "authentication required")
        return self.identity


def client_for(
    service: SessionService,
    host: Host,
    *,
    authenticate_approver: Callable[[Request], Awaitable[RunContext]] | None = None,
) -> httpx.AsyncClient:
    api = FastAPI()
    api.include_router(
        session_router(
            service, authenticate=host.authenticate, authenticate_approver=authenticate_approver
        )
    )
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api),
        base_url="http://local",
        headers={"authorization": "trusted-test"},
    )


def reading_service(tmp_path: Path, **options: object) -> SessionService:
    return SessionService(
        app(Echo(), [ModelResult(action=FinalAction(content="answer"))]),
        state_dir=tmp_path,
        authorize=lambda actor, operation, session: True,
        **options,
    )


async def test_http_session_lifecycle_restarts_and_retains_history(tmp_path: Path) -> None:
    requests: list[ModelRequest] = []

    class Model:
        async def decide(self, request: ModelRequest) -> ModelResult:
            requests.append(request)
            return ModelResult(action=FinalAction(content="answer"))

    specification = app(Echo(), [], model_factory=Model)
    service = SessionService(
        specification, state_dir=tmp_path, authorize=lambda actor, operation, session: True
    )
    host = Host()
    async with client_for(service, host) as client:
        assert (
            await client.post("/harness/sessions", headers={"authorization": "bad"})
        ).status_code == 401
        created = await client.post("/harness/sessions")
        assert created.status_code == 201, created.text
        assert created.json()["id"] == "s1"
        assert (await client.post("/harness/sessions")).status_code == 409
        assert (await client.get("/harness/sessions/s1")).status_code == 200
        submitted = await client.post(
            "/harness/sessions/s1/messages", json={"request_id": "one", "text": "first question"}
        )
        assert submitted.status_code == 200, submitted.text
        turn = submitted.json()
        assert turn["status"] == "succeeded"
        repeated = await client.post(
            "/harness/sessions/s1/messages", json={"request_id": "one", "text": "first question"}
        )
        assert repeated.json()["id"] == turn["id"]
        assert len(requests) == 1
        assert (
            await client.post(
                "/harness/sessions/s1/messages",
                json={"request_id": "one", "text": "changed question"},
            )
        ).status_code == 409
        base = f"/harness/sessions/s1/turns/{turn['id']}"
        assert (await client.get(base)).json()["id"] == turn["id"]
        assert (await client.get(base + "/inspect")).json()["status"] == "succeeded"
        events = await client.get(base + "/events", params={"limit": 1})
        assert events.status_code == 200, events.text
        assert len(events.json()) == 1

    reconstructed = SessionService(
        specification, state_dir=tmp_path, authorize=lambda actor, operation, session: True
    )
    async with client_for(reconstructed, host) as client:
        assert len((await client.get("/harness/sessions/s1/history")).json()) == 1
        second = await client.post(
            "/harness/sessions/s1/messages", json={"request_id": "two", "text": "follow up"}
        )
        assert second.status_code == 200, second.text
        assert "first question" in " ".join(message.content for message in requests[-1].messages)
        assert (await client.post("/harness/sessions/s1/close")).json()["status"] == "closed"
        assert (
            await client.post(
                "/harness/sessions/s1/messages", json={"request_id": "three", "text": "more"}
            )
        ).status_code == 409
        assert (await client.delete(base)).status_code == 204
        assert (await client.get(base + "/inspect")).status_code == 403
        assert (await client.delete("/harness/sessions/s1")).status_code == 204
        assert (await client.get("/harness/sessions/s1")).status_code == 403
        assert (await client.post("/harness/sessions")).status_code == 403


@pytest.mark.parametrize(
    "changed",
    [
        {"scope": ResourceScope(tenant_id="shop-b", resources=("product:a",))},
        {"scope": ResourceScope(tenant_id="shop-a", resources=("product:b",))},
        {"actor": Actor(id="intruder", roles=("use", "approve"))},
        {"session_id": "another-session"},
        {"application_id": "other"},
    ],
)
async def test_http_session_scope_checks_cover_every_operation(
    tmp_path: Path, changed: dict[str, object]
) -> None:
    service = reading_service(tmp_path)
    host = Host()
    async with client_for(service, host) as client:
        assert (await client.post("/harness/sessions")).status_code == 201
        turn = (
            await client.post(
                "/harness/sessions/s1/messages", json={"request_id": "one", "text": "private"}
            )
        ).json()
        base = f"/harness/sessions/s1/turns/{turn['id']}"
        host.identity = context(**changed)
        operations = [
            ("GET", "/harness/sessions/s1", None),
            ("GET", "/harness/sessions/s1/history", None),
            ("GET", base, None),
            ("GET", base + "/inspect", None),
            ("GET", base + "/events", None),
            ("POST", "/harness/sessions/s1/messages", {"request_id": "two", "text": "read"}),
            ("POST", base + "/approve", None),
            ("POST", base + "/resume", None),
            ("POST", "/harness/sessions/s1/close", None),
            ("DELETE", base, None),
            ("DELETE", "/harness/sessions/s1", None),
        ]
        for method, path, body in operations:
            response = await client.request(method, path, json=body)
            assert response.status_code == 403, (method, path, response.text)
            assert response.json() == {"detail": "session operation unavailable or unauthorized"}
        assert host.calls == len(operations) + 2


async def test_http_service_revocation_is_checked_again_on_reads_and_writes(tmp_path: Path) -> None:
    revoked = False
    seen: list[SessionOperation] = []

    def authorize(actor, operation, session):
        seen.append(operation)
        return not revoked

    service = SessionService(
        app(Echo(), [ModelResult(action=FinalAction(content="ok"))]),
        state_dir=tmp_path,
        authorize=authorize,
    )
    async with client_for(service, Host()) as client:
        assert (await client.post("/harness/sessions")).status_code == 201
        revoked = True
        for method, path, body in [
            ("GET", "/harness/sessions/s1", None),
            ("GET", "/harness/sessions/s1/history", None),
            ("POST", "/harness/sessions/s1/messages", {"request_id": "one", "text": "read"}),
            ("POST", "/harness/sessions/s1/close", None),
            ("DELETE", "/harness/sessions/s1", None),
        ]:
            assert (await client.request(method, path, json=body)).status_code == 403
    assert {SessionOperation.READ, SessionOperation.SUBMIT, SessionOperation.DELETE} <= set(seen)


@pytest.mark.parametrize("field", ["actor", "scope", "tenant_id", "roles", "session_id"])
async def test_http_identity_fields_are_rejected_in_all_mutating_bodies(
    tmp_path: Path, field: str
) -> None:
    async with client_for(reading_service(tmp_path), Host()) as client:
        assert (await client.post("/harness/sessions", json={field: "admin"})).status_code == 422
        assert (await client.post("/harness/sessions")).status_code == 201
        turn = (
            await client.post(
                "/harness/sessions/s1/messages", json={"request_id": "one", "text": "read"}
            )
        ).json()
        base = f"/harness/sessions/s1/turns/{turn['id']}"
        for method, path, body in [
            ("POST", "/harness/sessions/s1/messages", {"request_id": "two", "text": "read"}),
            ("POST", base + "/approve", {}),
            ("POST", base + "/resume", {}),
            ("POST", "/harness/sessions/s1/close", {}),
            ("DELETE", base, {}),
            ("DELETE", "/harness/sessions/s1", {}),
        ]:
            body[field] = "admin"
            response = await client.request(method, path, json=body)
            assert response.status_code == 422, (method, path, response.text)
        assert len((await client.get("/harness/sessions/s1/history")).json()) == 1


async def test_http_session_limits_are_validation_errors(tmp_path: Path) -> None:
    async with client_for(reading_service(tmp_path, max_message_chars=10), Host()) as client:
        assert (await client.post("/harness/sessions")).status_code == 201
        for text in ["", "  ", "x" * 11, "x" * 100_001]:
            response = await client.post(
                "/harness/sessions/s1/messages", json={"request_id": "one", "text": text}
            )
            assert response.status_code == 422, response.text
        for request_id in ["", "x" * 201]:
            assert (
                await client.post(
                    "/harness/sessions/s1/messages", json={"request_id": request_id, "text": "ok"}
                )
            ).status_code == 422
        for path, params in [
            ("/history", {"limit": 0}),
            ("/history", {"limit": 101}),
            ("/history", {"before_sequence": 0}),
            ("/turns/missing/events", {"limit": 0}),
            ("/turns/missing/events", {"limit": 1001}),
            ("/turns/missing/events", {"after": -1}),
        ]:
            assert (
                await client.get("/harness/sessions/s1" + path, params=params)
            ).status_code == 422


async def test_http_delegated_approval_reauthenticates_and_restarts(tmp_path: Path) -> None:
    tool = Echo(write=True)
    policy = Policy()
    owner = context(actor=Actor(id="owner", roles=("use",)))
    reviewer = context(actor=Actor(id="reviewer", roles=("approve",)))
    approver_calls = 0
    reviewer_revoked = False
    checked: list[tuple[str, SessionOperation]] = []

    async def authenticate_approver(request: Request) -> RunContext:
        nonlocal approver_calls
        approver_calls += 1
        assert request.headers["authorization"] == "trusted-test"
        return reviewer

    def authorize(actor, operation, session):
        checked.append((actor.actor.id, operation))
        return not (actor.actor.id == "reviewer" and reviewer_revoked)

    specification = app(
        tool,
        [],
        policy=policy,
        model_factory=lambda: ScriptedModel(
            [ModelResult(action=ToolAction(call=ToolCall(id="write", name="echo", arguments={})))],
        ),
    )
    service = SessionService(specification, state_dir=tmp_path, authorize=authorize)
    host = Host(owner)
    async with client_for(service, host, authenticate_approver=authenticate_approver) as client:
        assert (await client.post("/harness/sessions")).status_code == 201
        submitted = await client.post(
            "/harness/sessions/s1/messages", json={"request_id": "one", "text": "write"}
        )
        assert submitted.status_code == 200, submitted.text
        assert submitted.json()["status"] == "awaiting_approval"
        base = f"/harness/sessions/s1/turns/{submitted.json()['id']}"
        approved = await client.post(base + "/approve")
        assert approved.status_code == 200, approved.text
        grant = approved.json()
        assert grant["granted_by"] == "reviewer"
        assert {("owner", SessionOperation.APPROVE), ("reviewer", SessionOperation.APPROVE)} <= set(
            checked
        )

    from dataclasses import replace

    reconstructed = SessionService(
        replace(
            specification,
            model_factory=lambda: ScriptedModel([ModelResult(action=FinalAction(content="saved"))]),
        ),
        state_dir=tmp_path,
        authorize=authorize,
    )
    async with client_for(
        reconstructed, host, authenticate_approver=authenticate_approver
    ) as client:
        reviewer_revoked = True
        denied = await client.post(base + "/resume", json={"grant": grant})
        assert denied.status_code == 403, denied.text
        assert not tool.calls
        reviewer_revoked = False
        policy.revoked = True
        assert (await client.post(base + "/resume", json={"grant": grant})).status_code == 403
        assert not tool.calls
        policy.revoked = False
        forged = {**grant, "id": "not-issued"}
        assert (await client.post(base + "/resume", json={"grant": forged})).status_code == 403
        assert not tool.calls
        resumed = await client.post(base + "/resume", json={"grant": grant})
        assert resumed.status_code == 200, resumed.text
        assert resumed.json()["status"] == "succeeded"
        assert len(tool.calls) == 1
        replay = await client.post(base + "/resume", json={"grant": grant})
        assert replay.status_code in (403, 409), replay.text
        assert len(tool.calls) == 1
        assert (await client.get(base + "/inspect")).json()["status"] == "succeeded"
    assert approver_calls == 6


async def test_http_self_approval_requires_current_approver_role(tmp_path: Path) -> None:
    service = SessionService(
        app(
            Echo(write=True),
            [ModelResult(action=ToolAction(call=ToolCall(id="write", name="echo", arguments={})))],
        ),
        state_dir=tmp_path,
        authorize=lambda actor, operation, session: True,
    )
    async with client_for(
        service, Host(context(actor=Actor(id="owner", roles=("use",))))
    ) as client:
        await client.post("/harness/sessions")
        submitted = await client.post(
            "/harness/sessions/s1/messages", json={"request_id": "one", "text": "write"}
        )
        base = f"/harness/sessions/s1/turns/{submitted.json()['id']}"
        assert (await client.post(base + "/approve")).status_code == 403
