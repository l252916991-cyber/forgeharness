"""Tests for the local run and inspection API."""

from pathlib import Path

import httpx

from forgeharness.api import create_app


async def test_api_runs_and_inspects_keyless_demo(tmp_path: Path) -> None:
    transport = httpx.ASGITransport(app=create_app(tmp_path / "data"))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
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
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.get("/runs/missing")).status_code == 404
        assert (await client.get("/traces/missing/verify")).status_code == 404
        assert (await client.get("/runs/bad!id")).status_code == 422
        assert (await client.get("/runs/..%2Fescape")).status_code == 404
        assert (await client.post("/runs/keyless-demo", json={"text": ""})).status_code == 422
