"""Host-authenticated optional HTTP control."""

from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from test_sdk import Echo, app, context

from forgeharness.sdk import FinalAction, Harness, ModelResult
from forgeharness.sdk.control import control_router


async def test_http_auth_and_body_do_not_allow_identity_override(tmp_path: Path) -> None:
    harness = Harness(
        app(Echo(), [ModelResult(action=FinalAction(content="ok"))]), state_dir=tmp_path
    )

    async def authenticate(request: Request):
        if request.headers.get("authorization") != "trusted-test":
            raise HTTPException(401, "authentication required")
        return context()

    api = FastAPI()
    api.include_router(control_router(harness, authenticate=authenticate))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api), base_url="http://local"
    ) as client:
        assert (await client.get("/harness/runs/current")).status_code == 401
        headers = {"authorization": "trusted-test"}
        assert (await client.get("/harness/runs/current", headers=headers)).status_code == 403
        assert (
            await client.post(
                "/harness/runs", headers=headers, json={"task": "hello", "actor": "admin"}
            )
        ).status_code == 422
        created = await client.post("/harness/runs", headers=headers, json={"task": "hello"})
        assert created.status_code == 200, created.text
        assert (await client.get("/harness/runs/current", headers=headers)).json()[
            "status"
        ] == "succeeded"
        assert (
            await client.post("/harness/runs", headers=headers, json={"task": "hello"})
        ).status_code == 409
        assert (
            await client.post("/harness/runs/current/resume", headers=headers)
        ).status_code == 409
        assert (
            await client.post("/harness/runs/current/approve", headers=headers)
        ).status_code == 403


async def test_http_approved_operation_runs_through_durable_control(tmp_path: Path) -> None:
    from forgeharness.sdk import ModelRequest, ToolAction, ToolCall

    class Model:
        async def decide(self, request: ModelRequest):
            if request.messages[-1].role.value == "tool":
                return ModelResult(action=FinalAction(content="ok"))
            return ModelResult(action=ToolAction(call=ToolCall(id="c", name="echo", arguments={})))

    harness = Harness(app(Echo(write=True), [], model_factory=Model), state_dir=tmp_path)

    async def authenticate(request: Request):
        return context()

    api = FastAPI()
    api.include_router(control_router(harness, authenticate=authenticate))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api), base_url="http://local"
    ) as client:
        assert (await client.post("/harness/runs", json={"task": "write"})).json()[
            "status"
        ] == "awaiting_approval"
        response = await client.post("/harness/runs/current/approve")
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "succeeded"
