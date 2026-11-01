"""Run installed hosted AgentRuntime APIs on a private, disposable PostgreSQL.

Install forgeharness[cloud,postgres-test]. The deterministic provider keeps its
own request/response ledger; no external model or business request is made. The
CLI accepts no database address and only provisions its newly created server.
"""

import argparse
import asyncio
import hashlib
import importlib
import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

from forgeharness.cloud.execution_contract import PhysicalResult
from forgeharness.cloud.hosted import HostedApplication
from forgeharness.cloud.models import TenantContext
from forgeharness.cloud.provision import grant_runtime, migrate
from forgeharness.cloud.runtime import HostedRuntime
from forgeharness.cloud.store import PostgresRunStore
from forgeharness.domain.models import FinalAction, ModelResult, ModelUsage, Usage
from forgeharness.runtime.budget import RunBudget
from forgeharness.runtime.verification import VerificationResult
from forgeharness.sdk.spec import ApplicationSpec


def loaded_from_installation() -> bool:
    """Reject editable/source imports in the isolated wheel-consumer gate."""
    prefix = Path(sys.prefix).resolve()
    for name in (
        "forgeharness",
        "forgeharness.cloud.runtime",
        "forgeharness.cloud.execution",
        "forgeharness.cloud.execution_calls",
        "forgeharness.cloud.execution_state",
        "forgeharness.cloud.execution_contract",
    ):
        source = Path(importlib.import_module(name).__file__).resolve()
        if not source.is_relative_to(prefix) or "site-packages" not in source.parts:
            return False
    return True


class LocalProvider:
    """An independent deterministic provider oracle, separate from Cloud facts."""

    def __init__(self):
        self.records = {}
        self.dispatches = 0

    async def decide(self, request):
        self.dispatches += 1
        result = ModelResult(
            action=FinalAction(content="Local hosted execution confirmed."),
            model_name="independent-local-v1",
            usage=ModelUsage(input_tokens=3, output_tokens=2),
        )
        # Detached serialized values are provider facts, not aliases retained
        # from the result subsequently supplied by the runtime to its verifier.
        record = {
            "request": request.model_dump(mode="json"),
            "response": result.model_dump(mode="json"),
            "receipt_id": "local-" + hashlib.sha256(request.task_id.encode()).hexdigest(),
        }
        if request.task_id in self.records:
            raise RuntimeError("the example provider was dispatched more than once")
        self.records[request.task_id] = record
        return result

    async def verify_physical(self, physical, observed):
        record = self.records.get(physical.owner.run_id)
        if (
            record is None
            or record["request"] != physical.envelope.canonical_input["request"]
            or observed.result is None
            or record["response"] != observed.result.model_dump(mode="json")
        ):
            return None
        return PhysicalResult(
            receipt_id=record["receipt_id"],
            actual_units=5,
            kind="model",
            model_result=ModelResult.model_validate(record["response"]),
        )

    async def verify(self, request):
        record = self.records.get(request.task_id)
        passed = record is not None and record["response"]["action"] == request.final.model_dump(
            mode="json"
        )
        return VerificationResult(
            passed=passed,
            reason="independent local provider record checked",
            evidence=("local-provider://" + record["receipt_id"],) if passed else (),
        )


class NoToolPolicy:
    def evaluate(self, request):
        raise AssertionError("this registered application has no tools")

    def can_approve(self, **kwargs):
        return False


async def no_tool_result(physical, observed):
    raise AssertionError("this registered application has no physical tool calls")


async def _exercise_disposable(dsn: str) -> dict[str, object]:
    """Only main supplies the address of the server it just created."""
    asyncpg = importlib.import_module("asyncpg")
    admin = await asyncpg.connect(dsn)
    store = None
    try:
        assert await admin.fetchval("SELECT current_setting('listen_addresses')") == ""
        role = "fh_hosted_demo_" + uuid4().hex
        await admin.execute(
            f"CREATE ROLE {role} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
            "NOINHERIT NOREPLICATION NOBYPASSRLS"
        )
        await migrate(admin)
        await grant_runtime(admin, role)
        await admin.execute("INSERT INTO fh_cloud.organizations(org_id) VALUES('synthetic')")
        await admin.execute(
            "INSERT INTO fh_cloud.members(org_id,actor_id,can_execute,can_approve) VALUES "
            "('synthetic','owner',false,false),('synthetic','worker',true,false)"
        )
        parsed = urlsplit(dsn)
        query = dict(parse_qsl(parsed.query)) | {"user": role}
        runtime_dsn = urlunsplit(
            (parsed.scheme, parsed.netloc.rsplit("@", 1)[-1], parsed.path, urlencode(query), "")
        )
        store = await PostgresRunStore.connect(runtime_dsn)
        provider = LocalProvider()
        app = HostedApplication(
            application=ApplicationSpec(
                id="generic-hosted-example",
                version="1",
                policy_version="1",
                model_factory=lambda: provider,
                tools=(),
                policy=NoToolPolicy(),
                verifier=provider,
                budget=RunBudget(
                    max_steps=2,
                    max_tool_calls=0,
                    max_input_tokens=1_000,
                    max_output_tokens=100,
                    reserve_output_tokens=10,
                    max_attempts_per_call=1,
                ),
            ),
            deployment_id="generic-hosted-example",
            release_digest=hashlib.sha256(b"generic-hosted-example-v1").hexdigest(),
            model_binding_digest=hashlib.sha256(b"independent-local-v1").hexdigest(),
            max_run_units=100,
            model_reservation_units=20,
            tool_reservation_units={},
            model_verifier=provider.verify_physical,
            tool_verifier=no_tool_result,
        )
        runtime = HostedRuntime(store, {app.deployment_id: app})
        owner = TenantContext(org_id="synthetic", actor_id="owner")
        worker = TenantContext(org_id="synthetic", actor_id="worker")
        accepted = await runtime.accept(
            owner, app.deployment_id, "local-request", "Confirm the registered hosted flow."
        )
        duplicate = await runtime.accept(
            owner, app.deployment_id, "local-request", "Confirm the registered hosted flow."
        )
        assert accepted.run.run_id == duplicate.run.run_id
        finished = await runtime.advance(worker, accepted.run.run_id)
        assert finished.run.status == finished.checkpoint.status == "succeeded"
        assert finished.checkpoint.usage == Usage(steps=1, input_tokens=3, output_tokens=2)
        assert finished.run.used_units == 5 and finished.run.reserved_units == 0
        assert finished.checkpoint.final_output == "Local hosted execution confirmed."
        inspected = await runtime.inspect(owner, accepted.run.run_id)
        assert inspected.checkpoint == finished.checkpoint
        trace = await runtime.trace(owner, accepted.run.run_id)
        assert {event.type for event in trace.events} >= {
            "model.request",
            "model.action",
            "verification.passed",
            "run.finished",
        }
        assert trace.snapshot_pending() == ()
        physical = await admin.fetchrow(
            "SELECT c.canonical_output, c.actual_usage, i.state FROM fh_cloud.execution_calls c "
            "JOIN fh_cloud.invocations i USING(org_id,run_id,invocation_id)"
        )
        assert physical["state"] == "settled"
        assert PhysicalResult.model_validate_json(physical["canonical_output"]).actual_units == 5
        assert Usage.model_validate_json(physical["actual_usage"]) == finished.checkpoint.usage
        assert await admin.fetchval("SELECT count(*) FROM fh_cloud.receipts") == 1
        await runtime.advance(worker, accepted.run.run_id)
        assert provider.dispatches == 1
        return {
            "hosted_execution_contract": "passed",
            "status": finished.run.status,
            "used_units": finished.run.used_units,
            "reserved_units": finished.run.reserved_units,
            "provider_dispatches": provider.dispatches,
            "trace_events": len(trace.events),
            "external_requests": 0,
            "saas_qualified": False,
            "loaded_from_installation": loaded_from_installation(),
        }
    finally:
        if store is not None:
            await store.close()
        await admin.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--require-installed", action="store_true")
    args = parser.parse_args()
    if args.require_installed and not loaded_from_installation():
        parser.error("the hosted consumer must load production modules from its installed wheel")
    embedded = importlib.import_module("embedded_postgres")
    with TemporaryDirectory(prefix="fh-hosted-demo-") as directory:
        server = embedded.get_server(Path(directory) / "pgdata", cleanup_mode="stop")
        try:
            result = asyncio.run(_exercise_disposable(server.get_uri()))
        finally:
            server.cleanup()
    print(json.dumps(result | {"server_stopped": True}))


if __name__ == "__main__":
    main()
