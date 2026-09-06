"""Live PostgreSQL/Redis/Qdrant/Nginx platform verification.

Prerequisites: OMLX and the four services in deploy/docker-compose.yml are running.
The script owns temporary API/worker processes, exercises recovery and failover,
then writes a revision-linked JSON report.
"""

from __future__ import annotations

import asyncio
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx

PROJECT_ROOT = Path(__file__).parents[1]
OUTPUT = PROJECT_ROOT / "reports" / "platform-verification.json"
PRIMARY = "http://127.0.0.1:8001"
SECONDARY = "http://127.0.0.1:8002"
PROXY = "http://127.0.0.1:8080"


async def main() -> None:
    token = uuid4().hex
    with tempfile.TemporaryDirectory(prefix="forgeharness-platform-") as temporary:
        run_root = Path(temporary)
        environment = _environment(run_root)
        primary = _start("uvicorn", "8001", cwd=run_root, environment=environment)
        secondary = _start("uvicorn", "8002", cwd=run_root, environment=environment)
        worker = _start("arq", cwd=run_root, environment=environment)
        try:
            async with httpx.AsyncClient(trust_env=False, timeout=30) as client:
                await _wait_ready(client, PRIMARY, log_path=run_root / "uvicorn-8001.log")
                await _wait_ready(client, SECONDARY, log_path=run_root / "uvicorn-8002.log")
                await _wait_ready(client, PROXY)
                session_shared = await _session_check(client)
                readiness = await _readiness_latencies(client, 100)
                upload = await _upload_workload(client, token, 30)
                vector_hits = await _cross_process_search(client, token)
                worker.terminate()
                await asyncio.to_thread(worker.wait, 10)
                recovery_job = await _submit(client, f"{token}-recovery", "worker recovery")
                queued_during_outage = (
                    await client.get(f"{SECONDARY}/v1/jobs/{recovery_job}")
                ).json()["status"] == "queued"
                worker = _start("arq", cwd=run_root, environment=environment)
                worker_recovered = await _wait_jobs(client, [recovery_job], timeout_seconds=60)
                primary.terminate()
                await asyncio.to_thread(primary.wait, 10)
                await asyncio.sleep(6)
                failover_successes = await _failover_requests(client, 20)
            report = {
                "schema_version": 1,
                "created_at": datetime.now(UTC).isoformat(),
                "revision": _revision(),
                "topology": {
                    "api_processes": 2,
                    "api_runtime": "macOS host",
                    "dependencies": ["PostgreSQL", "Redis", "Qdrant", "Nginx"],
                    "model_server": "OMLX localhost",
                },
                "checks": {
                    "both_apis_ready": True,
                    "postgres_session_cross_process": session_shared,
                    "content_deduplicated_across_keys": upload["deduplicated"],
                    "cross_process_vector_hits": vector_hits,
                    "queued_while_worker_stopped": queued_during_outage,
                    "worker_recovered": worker_recovered,
                    "worker_jobs_succeeded": upload["succeeded"],
                    "worker_jobs_total": upload["total"],
                    "failover_successes": failover_successes,
                    "failover_requests": 20,
                },
                "latency_ms": {
                    "readiness_p50": round(statistics.median(readiness), 3),
                    "readiness_p95": round(_percentile(readiness, 0.95), 3),
                    "upload_accept_p50": round(statistics.median(upload["latencies"]), 3),
                    "upload_accept_p95": round(_percentile(upload["latencies"], 0.95), 3),
                },
                "thresholds": {
                    "readiness_p95_max_ms": 100,
                    "upload_accept_p95_max_ms": 300,
                    "failover_success_required": 20,
                },
                "qualified": (
                    session_shared
                    and upload["deduplicated"]
                    and upload["succeeded"] == upload["total"]
                    and vector_hits > 0
                    and queued_during_outage
                    and worker_recovered
                    and failover_successes == 20
                    and _percentile(readiness, 0.95) < 100
                    and _percentile(upload["latencies"], 0.95) < 300
                ),
                "scope_limit": (
                    "Local process failover only; the laptop, disk, Docker runtime, and OMLX "
                    "remain shared failure domains."
                ),
            }
            _atomic_write(OUTPUT, report)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            if not report["qualified"]:
                raise SystemExit(2)
        finally:
            for process in (primary, secondary, worker):
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()


def _environment(run_root: Path) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "FORGE_ENABLE_OMLX": "true",
            "FORGE_OMLX_BASE_URL": "http://127.0.0.1:8000/v1",
            "FORGE_POSTGRES_DSN": (
                "postgresql://forge:forge-local-only@127.0.0.1:5432/forgeharness"
            ),
            "FORGE_REDIS_URL": "redis://127.0.0.1:6379/0",
            "FORGE_QDRANT_URL": "http://127.0.0.1:6333",
            "FORGE_DATA_DIR": str(run_root / ".forgeharness"),
        }
    )
    return environment


def _start(
    kind: str,
    port: str | None = None,
    *,
    cwd: Path,
    environment: dict[str, str],
) -> subprocess.Popen[bytes]:
    executable = Path(sys.executable).with_name(kind)
    command = (
        [
            str(executable),
            "forgeharness.api:create_app",
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            port or "",
        ]
        if kind == "uvicorn"
        else [str(executable), "forgeharness.worker.WorkerSettings"]
    )
    suffix = f"-{port}" if port else ""
    log_path = cwd / f"{kind}{suffix}.log"
    with log_path.open("ab") as log:
        return subprocess.Popen(command, cwd=cwd, env=environment, stdout=log, stderr=log)


async def _wait_ready(
    client: httpx.AsyncClient, base_url: str, *, log_path: Path | None = None
) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            response = await client.get(f"{base_url}/health/ready")
            if response.status_code == 200 and response.json()["status"] == "ready":
                return
        except httpx.HTTPError:
            pass
        await asyncio.sleep(0.2)
    details = ""
    if log_path is not None and await asyncio.to_thread(log_path.is_file):
        log_text = await asyncio.to_thread(log_path.read_text, encoding="utf-8", errors="replace")
        details = "\n" + log_text[-4_000:]
    raise RuntimeError(f"service did not become ready: {base_url}{details}")


async def _session_check(client: httpx.AsyncClient) -> bool:
    created = await client.post(f"{PRIMARY}/v1/sessions", json={})
    created.raise_for_status()
    session_id = created.json()["id"]
    loaded = await client.get(f"{SECONDARY}/v1/sessions/{session_id}")
    return loaded.status_code == 200 and loaded.json()["session"]["id"] == session_id


async def _readiness_latencies(client: httpx.AsyncClient, count: int) -> list[float]:
    latencies = []
    for _ in range(count):
        started = time.perf_counter()
        response = await client.get(f"{PROXY}/health/ready")
        latencies.append((time.perf_counter() - started) * 1_000)
        response.raise_for_status()
        if response.json()["status"] != "ready":
            raise RuntimeError("proxy readiness became false")
    return latencies


async def _submit(client: httpx.AsyncClient, key: str, content: str) -> str:
    response = await client.post(
        f"{PROXY}/v1/documents",
        headers={"Idempotency-Key": key},
        files={"file": (f"{key}.md", content.encode(), "text/markdown")},
    )
    response.raise_for_status()
    return str(response.json()["id"])


async def _upload_workload(client: httpx.AsyncClient, token: str, count: int) -> dict[str, object]:
    jobs = []
    latencies = []
    first_content = f"# Platform {token} 0\nunique_{token}_0"
    for index in range(count):
        content = f"# Platform {token} {index}\nunique_{token}_{index}"
        started = time.perf_counter()
        jobs.append(await _submit(client, f"{token}-{index}", content))
        latencies.append((time.perf_counter() - started) * 1_000)
    duplicate = await _submit(client, f"{token}-duplicate", first_content)
    succeeded = await _wait_jobs(client, jobs, timeout_seconds=120)
    return {
        "total": len(jobs),
        "succeeded": len(jobs) if succeeded else 0,
        "latencies": latencies,
        "deduplicated": duplicate == jobs[0],
    }


async def _wait_jobs(client: httpx.AsyncClient, jobs: list[str], *, timeout_seconds: float) -> bool:
    pending = set(jobs)
    deadline = time.monotonic() + timeout_seconds
    while pending and time.monotonic() < deadline:
        for job_id in tuple(pending):
            response = await client.get(f"{SECONDARY}/v1/jobs/{job_id}")
            response.raise_for_status()
            state = response.json()["status"]
            if state == "failed":
                return False
            if state == "succeeded":
                pending.remove(job_id)
        if pending:
            await asyncio.sleep(0.2)
    return not pending


async def _cross_process_search(client: httpx.AsyncClient, token: str) -> int:
    response = await client.post(
        f"{SECONDARY}/v1/search", json={"query": f"unique_{token}_0", "limit": 3}
    )
    response.raise_for_status()
    return len(response.json()["vector"])


async def _failover_requests(client: httpx.AsyncClient, count: int) -> int:
    successes = 0
    for _ in range(count):
        try:
            response = await client.get(f"{PROXY}/health/ready")
            if response.status_code == 200 and response.json()["status"] == "ready":
                successes += 1
        except httpx.HTTPError:
            pass
    return successes


def _percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, min(len(ordered) - 1, int(len(ordered) * quantile) - 1))]


def _revision() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _atomic_write(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


if __name__ == "__main__":
    asyncio.run(main())
