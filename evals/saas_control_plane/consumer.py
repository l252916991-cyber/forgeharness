"""Independent installed-wheel HTTP, PostgreSQL, process and browser consumer.

The program owns a fresh socket-only PostgreSQL and synthetic business ledger.
No caller database, model endpoint, production credential or private data is
accepted. API and workers run in distinct installed-Python processes. Only the
test oracle uses SQLite; all platform state is PostgreSQL-authoritative.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib
import json
import os
import signal
import socket
import sys
import time
import traceback
from importlib.resources import files
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

from forgeharness.cloud.control import PlatformRuntime
from forgeharness.cloud.identity import PostgresIdentityStore
from forgeharness.cloud.models import TenantContext
from forgeharness.cloud.provision import (
    control_plane_inventory,
    grant_identity,
    grant_runtime,
    grant_web,
    upgrade_1_to_2,
    upgrade_2_to_3,
)
from forgeharness.cloud.store import PostgresRunStore
from forgeharness.cloud.worker import HostedWorker

# Import only the external oracle fixture directory, never the source package.
# Its governed production imports must resolve to the installed wheel below.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "saas_postgres/tests"))
from execution_fixtures import (
    SyntheticBusinessLedger,
    application_fixture,
    hosted_registration,
)

DOMAINS = ("forms", "research")
PASSWORD = "Synthetic-Only-Password-19!"
MODULES = (
    "forgeharness",
    "forgeharness.cloud.control",
    "forgeharness.cloud.control_api",
    "forgeharness.cloud.identity",
    "forgeharness.cloud.runtime",
    "forgeharness.cloud.store",
    "forgeharness.cloud.execution_calls",
    "forgeharness.cloud.provision",
    "forgeharness.cloud.worker",
    "forgeharness.cloud.workbench",
)


def loaded_from_installation() -> bool:
    prefix = Path(sys.prefix).resolve()
    return all(
        (source := Path(importlib.import_module(name).__file__).resolve()).is_relative_to(prefix)
        and "site-packages" in source.parts
        for name in MODULES
    )


def registry(ledger_path: Path, *, barriers: dict[str, Any] | None = None) -> dict[str, Any]:
    ledger = SyntheticBusinessLedger(ledger_path)
    fixtures = [application_fixture(ledger, domain) for domain in DOMAINS]
    if barriers is not None:
        barriers.update({fixture.domain: fixture.faults for fixture in fixtures})
    values = [hosted_registration(fixture) for fixture in fixtures]
    return {value.deployment_id: value for value in values}


def role_dsn(dsn: str, role: str) -> str:
    value = urlsplit(dsn)
    return urlunsplit(
        (
            value.scheme,
            value.netloc.rsplit("@", 1)[-1],
            value.path,
            urlencode(dict(parse_qsl(value.query)) | {"user": role}),
            "",
        )
    )


def child_environment() -> dict[str, str]:
    allowed = {"PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "NODE_PATH", "PLAYWRIGHT_BROWSERS_PATH"}
    return {key: value for key, value in os.environ.items() if key in allowed} | {
        "PYTHONNOUSERSITE": "1"
    }


async def serve(payload: dict[str, Any]) -> None:
    import uvicorn

    from forgeharness.cloud.control_api import create_control_app

    identity = await PostgresIdentityStore.connect(os.environ["FH_CONTROL_IDENTITY_DSN"])
    store = await PostgresRunStore.connect(os.environ["FH_CONTROL_WEB_DSN"])
    try:
        runtime = PlatformRuntime(store, registry(Path(payload["ledger_path"])))
        app = create_control_app(
            identity,
            runtime,
            allowed_origins=(payload["base_url"],),
            allowed_hosts=("127.0.0.1",),
            # Local-only loopback HTTP. Secure cookies are the default; TLS
            # termination belongs to the separately configured hosting layer.
            secure_cookies=False,
        )
        config = uvicorn.Config(
            app, host="127.0.0.1", port=payload["port"], access_log=False, log_level="critical"
        )
        await uvicorn.Server(config).serve()
    finally:
        await store.close()
        await identity.close()


async def worker(payload: dict[str, Any]) -> dict[str, Any]:
    store = await PostgresRunStore.connect(os.environ["FH_CONTROL_WORKER_DSN"])
    watcher = None
    try:
        barriers: dict[str, Any] = {}
        runtime = PlatformRuntime(store, registry(Path(payload["ledger_path"]), barriers=barriers))
        if payload.get("fault_point"):
            barrier = barriers[payload["fault_domain"]]
            barrier.arm(payload["fault_point"])

            async def signal_fault() -> None:
                await barrier.entered.wait()
                await asyncio.to_thread(Path(payload["fault_marker"]).write_text, "committed")
                # The release comes from another OS process, not an in-process Event.
                while not await asyncio.to_thread(  # noqa: ASYNC110
                    Path(payload["fault_release"]).exists
                ):
                    await asyncio.sleep(0.05)
                barrier.release.set()

            watcher = asyncio.create_task(signal_fault())
        loop = HostedWorker(
            runtime,
            TenantContext.model_validate(payload["worker"]),
            worker_id="consumer-" + uuid4().hex,
            lease_seconds=2,
        )
        progressed = await loop.tick()
        return {"pid": os.getpid(), "progressed": progressed, "failures": loop.metrics.failures}
    finally:
        if watcher is not None:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
        await store.close()


class ApiProcess:
    def __init__(self, payload: dict[str, Any], env: dict[str, str]) -> None:
        self.payload, self.env = payload, env
        self.process: asyncio.subprocess.Process | None = None

    async def start(self) -> None:
        import httpx

        self.process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(Path(__file__).resolve()),
            "--serve",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            env=self.env,
        )
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(self.payload).encode() + b"\n")
        await self.process.stdin.drain()
        self.process.stdin.close()
        deadline = time.monotonic() + 15
        async with httpx.AsyncClient() as client:
            while time.monotonic() < deadline:
                if self.process.returncode is not None:
                    raise RuntimeError("api process failed before readiness")
                try:
                    response = await client.get(self.payload["base_url"] + "/")
                    if response.status_code == 200:
                        return
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.05)
        raise TimeoutError("API readiness deadline")

    async def stop(self) -> None:
        if self.process is not None and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), 5)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()


class Client:
    def __init__(self, base_url: str) -> None:
        import httpx

        self.client = httpx.AsyncClient(base_url=base_url, headers={"Origin": base_url}, timeout=10)
        self.csrf = ""
        self.account_id = ""

    async def request(self, method: str, path: str, **kwargs: Any) -> Any:
        headers = kwargs.pop("headers", {}) | {"X-CSRF-Token": self.csrf}
        response = await self.client.request(method, path, headers=headers, **kwargs)
        assert response.headers.get("cache-control") == "no-store"
        return response

    async def login(self, email: str, org: str) -> None:
        response = await self.request(
            "POST", "/v1/login", json={"email": email, "password": PASSWORD}
        )
        assert response.status_code == 200, "login"
        value = response.json()
        self.csrf, self.account_id = value["csrf_token"], value["account_id"]
        if value["org_id"] != org:
            response = await self.request("POST", f"/v1/organizations/{org}/select")
            assert response.status_code == 200, "organization selection"
            self.csrf = response.json()["csrf_token"]

    async def invite(self, org: str, email: str) -> str:
        response = await self.request(
            "POST", f"/v1/organizations/{org}/invitations", json={"email": email, "role": "member"}
        )
        assert response.status_code == 201, "invite"
        return str(response.json()["invitation_token"])

    async def accept(self, token: str) -> None:
        response = await self.request(
            "POST", "/v1/invitations/accept", json={"token": token, "password": PASSWORD}
        )
        assert response.status_code == 200, "invitation acceptance"
        value = response.json()
        self.csrf, self.account_id = value["csrf_token"], value["account_id"]

    async def put(self, path: str, value: dict[str, Any]) -> None:
        response = await self.request("PUT", path, json=value)
        assert response.status_code == 204, "management write"


def run_path(org: str, workspace: str, run_id: str = "") -> str:
    base = f"/v1/organizations/{org}/workspaces/{workspace}/runs"
    return base + ("/" + run_id if run_id else "")


async def execute_worker(payload: dict[str, Any], env: dict[str, str]) -> dict[str, Any]:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(Path(__file__).resolve()),
        "--worker",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        env=env,
    )
    try:
        output, _ = await asyncio.wait_for(
            process.communicate(json.dumps(payload).encode() + b"\n"), 30
        )
    except BaseException:
        process.kill()
        await process.wait()
        raise
    assert process.returncode == 0, "independent worker failed"
    value = json.loads(output)
    assert value["failures"] == 0, "independent worker reported failure"
    return value


async def recover_interrupted_worker(
    payload: dict[str, Any],
    env: dict[str, str],
    admin: Any,
    run_id: str,
    domain: str,
    directory: Path,
    *,
    stop_and_resume: bool,
) -> dict[str, Any]:
    """Lose a worker after a committed effect; a new epoch resolves without redispatch."""
    marker, release = directory / (domain + ".committed"), directory / (domain + ".release")
    old_payload = payload | {
        "fault_domain": domain,
        "fault_point": "write.after_commit",
        "fault_marker": str(marker),
        "fault_release": str(release),
    }
    old = await asyncio.create_subprocess_exec(
        sys.executable,
        str(Path(__file__).resolve()),
        "--worker",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        env=env,
    )
    assert old.stdin is not None
    old.stdin.write(json.dumps(old_payload).encode() + b"\n")
    await old.stdin.drain()
    old.stdin.close()
    try:
        deadline = time.monotonic() + 15
        while not await asyncio.to_thread(marker.exists):
            assert old.returncode is None, "fault worker exited before business commit"
            if time.monotonic() >= deadline:
                raise TimeoutError("fault worker did not reach committed-effect barrier")
            await asyncio.sleep(0.05)
        original = await admin.fetchrow(
            "SELECT epoch,used_units,reserved_units,status FROM fh_cloud.runs "
            "WHERE org_id='org-a' AND run_id=$1",
            run_id,
        )
        assert original["status"] == "running"
        assert original["used_units"] == 12 and original["reserved_units"] == 10
        state = await admin.fetchval(
            "SELECT state FROM fh_cloud.invocations WHERE org_id='org-a' AND run_id=$1 "
            "AND tool_name='oracle.write'",
            run_id,
        )
        assert state == "sent", "unreceived effect must retain a sent reservation"
        if stop_and_resume:
            old.send_signal(signal.SIGSTOP)
        else:
            old.kill()
            await old.wait()
        await asyncio.sleep(2.1)
        recovered = await execute_worker(payload, env)
        assert recovered["progressed"] == 1
        current = await admin.fetchrow(
            "SELECT epoch,used_units,reserved_units,status FROM fh_cloud.runs "
            "WHERE org_id='org-a' AND run_id=$1",
            run_id,
        )
        assert current["epoch"] > original["epoch"]
        assert current["status"] == "succeeded"
        assert current["used_units"] == 20 and current["reserved_units"] == 0
        if stop_and_resume:
            old.send_signal(signal.SIGCONT)
            await asyncio.to_thread(release.write_text, "release old epoch")
            output, _ = await asyncio.wait_for(old.communicate(), 10)
            assert old.returncode == 0
            # A stale worker may receive the already-final view. Its report is
            # not evidence of a write; immutable PG facts and the oracle below
            # prove it cannot overwrite the takeover or redispatch the effect.
            assert json.loads(output)["failures"] == 0
            final = await admin.fetchrow(
                "SELECT epoch,used_units,reserved_units,status FROM fh_cloud.runs "
                "WHERE org_id='org-a' AND run_id=$1",
                run_id,
            )
            assert dict(final) == dict(current)
        return {
            "old_pid": old.pid,
            "new_pid": recovered["pid"],
            "old_epoch": original["epoch"],
            "new_epoch": current["epoch"],
            "mode": "SIGSTOP/SIGCONT" if stop_and_resume else "SIGKILL",
        }
    finally:
        if old.returncode is None:
            old.kill()
            await old.wait()


async def migrate_legacy(admin: Any) -> bool:
    """Consume shipped schema1/2/3 resources and preserve unknown historical facts."""
    await admin.execute(files("forgeharness.cloud").joinpath("schema.sql").read_text())
    await upgrade_1_to_2(admin)
    await admin.execute("INSERT INTO fh_cloud.organizations(org_id) VALUES('legacy')")
    await admin.execute(
        "INSERT INTO fh_cloud.members(org_id,actor_id,can_approve) VALUES "
        "('legacy','owner',false),('legacy','reviewer',true)"
    )
    await admin.execute(
        "INSERT INTO fh_cloud.runs(org_id,run_id,owner_id,request_key,definition,status,"
        "used_units,reserved_units) VALUES('legacy','legacy-run','owner','old-key',"
        "'{\"original\":true}','reconciling',4,9)"
    )
    await admin.execute(
        "INSERT INTO fh_cloud.approvals VALUES('legacy','old-approval','legacy-run',"
        "'old-fingerprint','reviewer',clock_timestamp()+interval '1 day',false)"
    )
    await admin.execute(
        "INSERT INTO fh_cloud.invocations(org_id,run_id,invocation_id,tool_name,effect_class,"
        "arguments_digest,action_digest,approval_id,reserve_units,state,admission_epoch) VALUES "
        "('legacy','legacy-run','old-unknown','old-tool','idempotent_write','a','b',"
        "'old-approval',9,'unknown',1),"
        "('legacy','legacy-run','old-settled','old-read','read','c','d',"
        "'old-approval',0,'settled',1)"
    )
    await admin.execute(
        "INSERT INTO fh_cloud.receipts VALUES('legacy','legacy-run','old-settled','old-receipt',"
        "4,'old-result','completed',clock_timestamp())"
    )
    await admin.execute(
        "INSERT INTO fh_cloud.outbox(org_id,event_id,run_id,kind,payload) VALUES"
        "('legacy','old-event','legacy-run','legacy.fact','{\"retained\":true}')"
    )
    tables = ("organizations", "members", "runs", "approvals", "invocations", "receipts", "outbox")

    async def facts() -> list[Any]:
        return [
            await admin.fetchval(
                "SELECT jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text)::text "
                f"FROM fh_cloud.{table} t"
            )
            for table in tables
        ]

    before = await facts()
    inventory = await control_plane_inventory(admin)
    assert inventory == {
        "active_runs": 1,
        "pending_approvals": 1,
        "unknown_calls": 1,
        "pending_outbox": 1,
    }
    try:
        async with admin.transaction():
            await upgrade_2_to_3(admin, writers_stopped=True)
            raise InterruptedError("injected offline migration rollback")
    except InterruptedError:
        pass
    assert await admin.fetchval("SELECT version FROM fh_cloud.schema_version") == 2
    assert await admin.fetchval("SELECT to_regclass('fh_cloud.accounts')") is None
    assert await facts() == before
    await upgrade_2_to_3(admin, writers_stopped=True)
    return await facts() == before


async def exercise(dsn: str, directory: Path, args: argparse.Namespace) -> dict[str, Any]:
    import asyncpg

    admin = await asyncpg.connect(dsn)
    identity = None
    api = None
    clients: list[Client] = []
    checks: dict[str, bool] = {}
    browser_result: dict[str, Any] | None = None
    stage = "migration"
    try:
        assert await admin.fetchval("SHOW listen_addresses") == ""
        checks["migration_preserves_history"] = await migrate_legacy(admin)
        role_names = {
            key: "fh_cp_" + key + "_" + uuid4().hex for key in ("identity", "web", "worker")
        }
        for role in role_names.values():
            await admin.execute(
                f"CREATE ROLE {role} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                "NOINHERIT NOREPLICATION NOBYPASSRLS"
            )
        await grant_identity(admin, role_names["identity"])
        await grant_web(admin, role_names["web"])
        await grant_runtime(admin, role_names["worker"])
        privilege_checks = (
            ("web", "fh_cloud.runs", "status", "UPDATE"),
            ("web", "fh_cloud.invocations", "state", "UPDATE"),
            ("identity", "fh_cloud.runs", "status", "UPDATE"),
            ("worker", "fh_cloud.accounts", "password_hash", "SELECT"),
            ("worker", "fh_cloud.members", "can_approve", "UPDATE"),
        )
        checks["least_privilege"] = all(
            [
                not await admin.fetchval(
                    "SELECT has_column_privilege($1,$2,$3,$4)",
                    role_names[role],
                    table,
                    column,
                    privilege,
                )
                for role, table, column, privilege in privilege_checks
            ]
        )
        identity = await PostgresIdentityStore.connect(role_dsn(dsn, role_names["identity"]))
        await identity.bootstrap("org-a", "admin-a@example.invalid", PASSWORD)
        await identity.bootstrap("org-b", "admin-b@example.invalid", PASSWORD)
        for org in ("org-a", "org-b"):
            await admin.execute(
                "INSERT INTO fh_cloud.members(org_id,actor_id,can_execute) "
                "VALUES($1,'service',true)",
                org,
            )
        ledger_path = directory / "external-oracle.sqlite"
        ledger = SyntheticBusinessLedger(ledger_path)
        for org in ("org-a", "org-b"):
            for domain in DOMAINS:
                ledger.seed(domain, org)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        base_url = f"http://127.0.0.1:{port}"
        env = child_environment() | {
            "FH_CONTROL_IDENTITY_DSN": role_dsn(dsn, role_names["identity"]),
            "FH_CONTROL_WEB_DSN": role_dsn(dsn, role_names["web"]),
            "FH_CONTROL_WORKER_DSN": role_dsn(dsn, role_names["worker"]),
        }
        payload = {"ledger_path": str(ledger_path), "base_url": base_url, "port": port}
        api = ApiProcess(payload, env)
        stage = "api-start"
        await api.start()

        def client() -> Client:
            value = Client(base_url)
            clients.append(value)
            return value

        admin_a, admin_b = client(), client()
        await admin_a.login("admin-a@example.invalid", "org-a")
        await admin_b.login("admin-b@example.invalid", "org-b")
        alice, bea, reviewer, foreign, reviewer_b = (client() for _ in range(5))
        stage = "invitation-and-management"
        for manager, org, email, target in (
            (admin_a, "org-a", "alice@example.invalid", alice),
            (admin_a, "org-a", "bea@example.invalid", bea),
            (admin_a, "org-a", "reviewer@example.invalid", reviewer),
            (admin_b, "org-b", "foreign@example.invalid", foreign),
            (admin_b, "org-b", "reviewer-b@example.invalid", reviewer_b),
        ):
            token = await manager.invite(org, email)
            await target.accept(token)
            replay = await client().request(
                "POST", "/v1/invitations/accept", json={"token": token, "password": PASSWORD}
            )
            assert replay.status_code == 401, "invitation replay"
        for manager, org, ws, domain, members in (
            (admin_a, "org-a", "workspace-a", "forms", (alice, bea, reviewer)),
            (admin_a, "org-a", "workspace-b", "research", (alice, bea, reviewer)),
            (admin_b, "org-b", "workspace-c", "forms", (foreign, reviewer_b)),
            (admin_b, "org-b", "workspace-d", "research", (foreign, reviewer_b)),
        ):
            prefix = f"/v1/organizations/{org}"
            await manager.put(prefix + "/workspaces/" + ws, {"name": ws, "active": True})
            await manager.put(
                prefix + f"/workspaces/{ws}/deployments/synthetic-{domain}", {"active": True}
            )
            for member in (manager, *members):
                await manager.put(
                    prefix + f"/workspaces/{ws}/members/{member.account_id}", {"active": True}
                )
                await manager.put(
                    prefix + f"/deployments/synthetic-{domain}/grants/{member.account_id}",
                    {
                        "active": True,
                        "can_use": member is not reviewer and member is not reviewer_b,
                        "can_approve": member is reviewer or member is reviewer_b,
                    },
                )
        checks["two_organizations"] = (await alice.request("GET", "/v1/organizations")).json()[
            "items"
        ][0]["org_id"] == "org-a"
        runs: list[tuple[str, str, str]] = []
        stage = "asynchronous-acceptance"
        for ws, domain in (("workspace-a", "forms"), ("workspace-b", "research")):
            body = {"deployment_id": "synthetic-" + domain, "task": "Review the synthetic record."}
            response = await alice.request(
                "POST", run_path("org-a", ws), json=body, headers={"Idempotency-Key": domain}
            )
            assert response.status_code == 202, "async acceptance"
            run_id = response.json()["run_id"]
            assert response.json()["status"] == "queued"
            repeat = await alice.request(
                "POST", run_path("org-a", ws), json=body, headers={"Idempotency-Key": domain}
            )
            assert repeat.status_code == 202 and repeat.json()["run_id"] == run_id
            conflict = await alice.request(
                "POST",
                run_path("org-a", ws),
                json=body | {"task": "changed"},
                headers={"Idempotency-Key": domain},
            )
            assert conflict.status_code == 409
            runs.append((ws, domain, run_id))
        assert ledger.all_receipts() == (), "HTTP must not execute model or tools"
        checks["asynchronous_acceptance"] = True
        checks["two_applications"] = len(runs) == 2
        ws, _domain, first_run = runs[0]
        checks["same_role_owner_isolation"] = (
            await bea.request("GET", run_path("org-a", ws, first_run))
        ).status_code == 404
        wrong_paths = (
            (alice, "org-a", "workspace-b"),
            (alice, "org-b", "workspace-c"),
            (foreign, "org-b", "workspace-c"),
        )
        checks["forged_path_denied"] = all(
            [
                (await actor.request("GET", run_path(org, workspace, first_run))).status_code == 404
                for actor, org, workspace in wrong_paths
            ]
        )
        await api.stop()
        worker_payload = payload | {"worker": {"org_id": "org-a", "actor_id": "service"}}
        pids: set[int] = set()
        stage = "api-exit-independent-worker"
        for _ in runs:
            result = await execute_worker(worker_payload, env)
            pids.add(result["pid"])
            assert result["progressed"] == 1
        await api.start()
        stage = "independent-approval"
        for ws, domain, run_id in runs:
            path = run_path("org-a", ws, run_id)
            assert (await alice.request("GET", path)).json()["status"] == "awaiting_approval"
            assert (await admin_a.request("GET", path + "/approval")).status_code == 404
            # The owner has real current use and approval permissions. Multiple
            # roles still identify one person and must never permit self-review.
            await admin_a.put(
                f"/v1/organizations/org-a/deployments/synthetic-{domain}/grants/{alice.account_id}",
                {"active": True, "can_use": True, "can_approve": True},
            )
            assert (
                await admin.fetchval(
                    "SELECT can_approve FROM fh_cloud.members WHERE org_id='org-a' AND actor_id=$1",
                    alice.account_id,
                )
                is True
            )
            assert (await alice.request("GET", path + "/approval")).status_code == 404
            proposal = (await reviewer.request("GET", path + "/approval")).json()
            assert proposal["proposal"]["name"] == "oracle.write"
            for decision in ("approve", "reject"):
                self_decision = await alice.request(
                    "POST",
                    path + "/" + decision,
                    json={"fingerprint": proposal["fingerprint"]},
                )
                assert self_decision.status_code == 404
            assert (
                await admin.fetchval(
                    "SELECT count(*) FROM fh_cloud.approval_decisions "
                    "WHERE org_id='org-a' AND run_id=$1",
                    run_id,
                )
                == 0
            )
            assert (
                await admin.fetchval(
                    "SELECT count(*) FROM fh_cloud.approvals WHERE org_id='org-a' AND run_id=$1",
                    run_id,
                )
                == 0
            )
            bad = await reviewer.request("POST", path + "/approve", json={"fingerprint": "f" * 64})
            assert bad.status_code == 409
            response = await reviewer.request(
                "POST", path + "/approve", json={"fingerprint": proposal["fingerprint"]}
            )
            assert response.status_code == 200
        checks["independent_approval"] = True
        stage = "committed-effect-process-faults"
        recoveries = []
        for index, (_ws, domain, run_id) in enumerate(runs):
            result = await recover_interrupted_worker(
                worker_payload,
                env,
                admin,
                run_id,
                domain,
                directory,
                stop_and_resume=index == 0,
            )
            recoveries.append(result)
            pids.update((result["old_pid"], result["new_pid"]))
        checks["worker_process_recovery"] = len(recoveries) == 2 and len(pids) == 6
        for ws, domain, run_id in runs:
            value = (await alice.request("GET", run_path("org-a", ws, run_id))).json()
            assert value["status"] == "succeeded" and value["used_units"] == 20
            assert value["reserved_units"] == 0 and json.loads(value["result"])["domain"] == domain
            assert ledger.effect_count(domain) == 1
        checks["independent_business_oracle"] = all(
            item.dispatches == 1 for item in ledger.all_receipts()
        )
        stage = "durable-rejection"
        response = await alice.request(
            "POST",
            run_path("org-a", "workspace-a"),
            json={"deployment_id": "synthetic-forms", "task": "Reject this proposal."},
            headers={"Idempotency-Key": "reject"},
        )
        assert response.status_code == 202
        reject_run = response.json()["run_id"]
        await execute_worker(worker_payload, env)
        path = run_path("org-a", "workspace-a", reject_run)
        proposal = (await reviewer.request("GET", path + "/approval")).json()
        for _ in range(2):
            assert (
                await reviewer.request(
                    "POST", path + "/reject", json={"fingerprint": proposal["fingerprint"]}
                )
            ).status_code == 200
        assert (
            await reviewer.request(
                "POST", path + "/approve", json={"fingerprint": proposal["fingerprint"]}
            )
        ).status_code == 409
        await execute_worker(worker_payload, env)
        rejected = (await alice.request("GET", path)).json()
        checks["durable_rejection"] = (
            rejected["status"] == "cancelled"
            and rejected["error"] == "approval_rejected"
            and ledger.effect_count("forms") == 1
        )
        stage = "queued-revocation"
        count_before = len(ledger.all_receipts())
        response = await bea.request(
            "POST",
            run_path("org-a", "workspace-a"),
            json={"deployment_id": "synthetic-forms", "task": "Never send."},
            headers={"Idempotency-Key": "revoked"},
        )
        assert response.status_code == 202
        await admin_a.put(
            f"/v1/organizations/org-a/deployments/synthetic-forms/grants/{bea.account_id}",
            {"active": False, "can_use": True, "can_approve": False},
        )
        await execute_worker(worker_payload, env)
        assert len(ledger.all_receipts()) == count_before
        checks["revocation_prevents_send"] = (
            await bea.request("GET", run_path("org-a", "workspace-a", response.json()["run_id"]))
        ).status_code == 404
        stage = "real-browser"
        browser_payload = payload | {
            "password": PASSWORD,
            "admin_email": "admin-b@example.invalid",
            "reviewer_email": "reviewer-b@example.invalid",
            "member_email": "browser@example.invalid",
            "org_id": "org-b",
            "workspace_id": "workspace-c",
            "cancel_workspace_id": "workspace-d",
            "worker": {"org_id": "org-b", "actor_id": "service"},
            "worker_python": sys.executable,
            "worker_script": str(Path(__file__).resolve()),
            "playwright_module": args.playwright_module,
            "browser_executable": args.browser_executable,
        }
        process = await asyncio.create_subprocess_exec(
            args.browser_node,
            str(Path(__file__).with_name("browser.cjs")),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=env,
        )
        try:
            output, _ = await asyncio.wait_for(
                process.communicate(json.dumps(browser_payload).encode()), 90
            )
        except BaseException:
            process.kill()
            await process.wait()
            raise
        browser_result = json.loads(output)
        assert process.returncode == 0 and browser_result["passed"], "real browser flow"
        checks["real_browser_flow"] = True
        stage = "secret-scan"
        forbidden = [
            PASSWORD,
            *[value.client.cookies.get("fh_control_session", "") for value in clients],
        ]
        public = json.dumps(
            (await alice.request("GET", run_path("org-a", runs[0][0], first_run))).json()
        )
        evidence = await admin.fetchval(
            "SELECT coalesce(jsonb_agg(envelope)::text,'[]') FROM fh_cloud.execution_evidence"
        )
        audit = await admin.fetchval(
            "SELECT coalesce(jsonb_agg(to_jsonb(a))::text,'[]') FROM fh_cloud.identity_audit a"
        )
        checks["secrets_not_exposed"] = all(
            not secret or secret not in public + evidence + audit for secret in forbidden
        )
        return {
            "checks": checks,
            "passed": all(checks.values()),
            "worker_pids": sorted(pids),
            "worker_recoveries": recoveries,
            "approval_evidence": {"dual_role_owner_self_decisions_denied": True},
            "browser": browser_result,
            "loaded_from_installation": loaded_from_installation(),
            "external_requests": 0,
            "saas_qualified": False,
            "business_qualified": False,
        }
    except Exception as exc:
        failure = traceback.extract_tb(exc.__traceback__)[-1]
        return {
            "checks": checks,
            "passed": False,
            "error_type": type(exc).__name__,
            "failure_location": {"function": failure.name, "line": failure.lineno},
            "stage": stage,
            "browser": browser_result,
            "loaded_from_installation": loaded_from_installation(),
            "external_requests": 0,
            "saas_qualified": False,
            "business_qualified": False,
        }
    finally:
        for value in clients:
            await value.client.aclose()
        if api is not None:
            await api.stop()
        if identity is not None:
            await identity.close()
        await admin.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--serve", action="store_true")
    mode.add_argument("--worker", action="store_true")
    parser.add_argument("--require-installed", action="store_true")
    parser.add_argument("--wheel", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--browser-node", default="node")
    parser.add_argument("--playwright-module", default="playwright")
    parser.add_argument("--browser-executable")
    args = parser.parse_args()
    if args.require_installed and not loaded_from_installation():
        parser.error("control consumer must import production modules from its installed wheel")
    if args.serve or args.worker:
        try:
            payload = json.loads(sys.stdin.readline())
            if args.serve:
                asyncio.run(serve(payload))
            else:
                print(json.dumps(asyncio.run(worker(payload))))
        except Exception as exc:
            print(json.dumps({"error_type": type(exc).__name__}))
            raise SystemExit(1) from None
        return
    if args.wheel is None or args.output is None:
        parser.error("--wheel and --output are required")
    embedded = importlib.import_module("embedded_postgres")
    with TemporaryDirectory(prefix="fh-control-consumer-") as temporary:
        directory = Path(temporary)
        server = embedded.get_server(directory / "pgdata", cleanup_mode="stop")
        try:
            result = asyncio.run(exercise(server.get_uri(), directory, args))
        finally:
            server.cleanup()
        result["server_stopped"] = True
    result["wheel_sha256"] = hashlib.sha256(args.wheel.read_bytes()).hexdigest()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"passed": result["passed"], "stage": result.get("stage", "complete")}))
    raise SystemExit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
