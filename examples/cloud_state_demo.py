"""Exercise installed Cloud state APIs on an owned, temporary Unix-socket server.

Install forgeharness[cloud,postgres-test]. Uses synthetic facts, no model or
business requests. The optional embedded-postgres binary is a development tool.
"""

import asyncio
import importlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

from forgeharness.cloud.calls import PostgresCallStore
from forgeharness.cloud.models import CallRequest, Receipt, RunDefinition, TenantContext
from forgeharness.cloud.provision import grant_runtime, migrate
from forgeharness.cloud.store import PostgresRunStore


async def exercise(dsn: str) -> dict[str, object]:
    asyncpg = importlib.import_module("asyncpg")
    admin = await asyncpg.connect(dsn)
    runs = None
    try:
        role = "fh_demo_" + uuid4().hex
        await admin.execute(
            f"CREATE ROLE {role} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
            "NOINHERIT NOREPLICATION NOBYPASSRLS"
        )
        await migrate(admin)
        await grant_runtime(admin, role)
        await admin.execute("INSERT INTO fh_cloud.organizations(org_id) VALUES('synthetic')")
        await admin.execute(
            "INSERT INTO fh_cloud.members(org_id,actor_id,can_execute,can_approve) VALUES "
            "('synthetic','owner',false,false),('synthetic','worker',true,false),"
            "('synthetic','reviewer',false,true)"
        )
        parsed = urlsplit(dsn)
        query = dict(parse_qsl(parsed.query)) | {"user": role}
        runtime_dsn = urlunsplit(
            (parsed.scheme, parsed.netloc.rsplit("@", 1)[-1], parsed.path, urlencode(query), "")
        )
        runs = await PostgresRunStore.connect(runtime_dsn)
        calls = PostgresCallStore(runs)
        owner, worker, reviewer = (
            TenantContext(org_id="synthetic", actor_id=name)
            for name in ("owner", "worker", "reviewer")
        )
        run = await runs.submit(
            owner,
            "synthetic-request",
            RunDefinition(
                application_id="generic-example",
                application_version="1",
                policy_version="1",
                binding_digest="a" * 64,
                input_digest="b" * 64,
                max_units=100,
                max_calls=1,
                deadline_at=datetime.now(UTC) + timedelta(minutes=5),
            ),
        )
        ticket = await runs.claim(worker, run.run_id, "example-worker")
        call = CallRequest(
            invocation_id="logical-call-1",
            tool_name="synthetic.read",
            effect_class="read",
            arguments_digest="c" * 64,
            reserve_units=25,
        )
        approval = await calls.grant(reviewer, run.run_id, call)
        await calls.admit(worker, ticket, call, approval)
        await calls.mark_sent(worker, ticket, call.invocation_id)
        # Synthetic oracle stands in for a verified provider receipt. No external
        # execution happens; this validates state, not a working provider adapter.
        await calls.record_receipt(
            worker,
            run.run_id,
            call.invocation_id,
            Receipt(receipt_id="synthetic-receipt", actual_units=17, result_digest="d" * 64),
        )
        await calls.settle(worker, ticket, call.invocation_id)
        result = await runs.finish(worker, ticket)
        assert result.used_units == 17 and result.reserved_units == 0
        return {
            "state_contract": "passed",
            "status": result.status,
            "used_units": result.used_units,
            "reserved_units": result.reserved_units,
            "events": len(await runs.events(owner, run.run_id)),
            "external_requests": 0,
            "saas_qualified": False,
        }
    finally:
        if runs is not None:
            await runs.close()
        await admin.close()


def main() -> None:
    embedded = importlib.import_module("embedded_postgres")
    with TemporaryDirectory(prefix="fh-cloud-demo-") as directory:
        server = embedded.get_server(Path(directory) / "pgdata", cleanup_mode="stop")
        try:
            result = asyncio.run(exercise(server.get_uri()))
        finally:
            server.cleanup()
    print(json.dumps(result | {"server_stopped": True}))


if __name__ == "__main__":
    main()
