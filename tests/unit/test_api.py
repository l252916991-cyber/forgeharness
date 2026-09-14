"""Tests for the local run and inspection API."""

from pathlib import Path

import httpx
import pytest

from forgeharness.api import create_app
from forgeharness.knowledge.application import KnowledgeSettings


async def test_api_runs_and_inspects_keyless_demo(tmp_path: Path) -> None:
    transport = httpx.ASGITransport(app=create_app(tmp_path / "data"))
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
        health = await client.get("/health")
        assert health.status_code == 200
        assert health.json()["service"] == "forgeharness"

        created = await client.post("/runs/keyless-demo", json={"text": "controlled"})
        assert created.status_code == 200
        result = created.json()
        assert result["status"] == "succeeded"
        assert result["final_output"] == "Echo verified: controlled"

        loaded = await client.get(f"/runs/{result['task_id']}")
        assert loaded.status_code == 200
        assert loaded.json() == result

        verified = await client.get(f"/traces/{result['task_id']}/verify")
        assert verified.status_code == 200
        assert verified.json()["valid"] is True
        assert verified.json()["events"] > 0


async def test_api_rejects_unknown_and_invalid_ids(tmp_path: Path) -> None:
    transport = httpx.ASGITransport(app=create_app(tmp_path / "data"))
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
        assert (await client.get("/runs/missing")).status_code == 404
        assert (await client.get("/traces/missing/verify")).status_code == 404
        assert (await client.get("/runs/bad!id")).status_code == 422
        assert (await client.get("/runs/..%2Fescape")).status_code == 404
        assert (await client.post("/runs/keyless-demo", json={"text": ""})).status_code == 422


async def test_api_key_protects_service_routes_but_keeps_health_public(tmp_path: Path) -> None:
    settings = KnowledgeSettings(service_api_key="s" * 16)
    transport = httpx.ASGITransport(app=create_app(tmp_path / "data", settings=settings))
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
        assert (await client.get("/health")).status_code == 200
        assert (await client.get("/runs/missing")).status_code == 401
        assert (
            await client.get("/runs/missing", headers={"X-API-Key": "wrong"})
        ).status_code == 401
        authorized = await client.get(
            "/runs/missing", headers={"Authorization": "Bearer " + "s" * 16}
        )
        assert authorized.status_code == 404


@pytest.mark.parametrize(
    "path", ["/traces/missing/verify", "/v1/traces/missing/verify", "/metrics", "/health/ready"]
)
async def test_service_key_protects_all_inspection_routes(tmp_path: Path, path: str) -> None:
    settings = KnowledgeSettings(service_api_key="s" * 16)
    transport = httpx.ASGITransport(app=create_app(tmp_path, settings=settings))
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
        assert (await client.get(path)).status_code == 401
        authorized = await client.get(path, headers={"X-API-Key": "s" * 16})
        assert authorized.status_code != 401


async def test_non_ascii_credentials_fail_closed(tmp_path: Path) -> None:
    settings = KnowledgeSettings(service_api_key="s" * 16)
    transport = httpx.ASGITransport(app=create_app(tmp_path, settings=settings))
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
        response = await client.get("/runs/missing", headers={b"x-api-key": b"\xff"})
        assert response.status_code == 401
