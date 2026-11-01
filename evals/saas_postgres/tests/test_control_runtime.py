"""Current authorization is in PG admission/send transactions, beyond HTTP."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

import asyncpg
import pytest

from forgeharness.cloud.control import AdmissionLimits, PlatformRuntime
from forgeharness.cloud.execution import governance_request, tool_envelope
from forgeharness.cloud.execution_calls import ExecutionCallStore
from forgeharness.cloud.execution_state import ExecutionStateStore
from forgeharness.cloud.identity import PostgresIdentityStore
from forgeharness.cloud.models import CloudConflict, CloudDenied, TenantContext
from forgeharness.cloud.provision import grant_identity, grant_web
from forgeharness.cloud.runtime import HostedRuntime
from forgeharness.cloud.store import PostgresRunStore
from forgeharness.cloud.worker import HostedWorker
from forgeharness.domain.models import ToolCall

from .execution_fixtures import (
    SyntheticBusinessLedger,
    application_fixture,
    hosted_registration,
)


@dataclass
class ControlCase:
    pg: object
    identity: PostgresIdentityStore
    web: PostgresRunStore
    runtime: PlatformRuntime
    owner: object
    user: object
    peer: object
    reviewer: object
    fixture: object
    registration: object
    worker: TenantContext


def role_dsn(dsn, role):
    parsed = urlsplit(dsn)
    query = dict(parse_qsl(parsed.query))
    query["user"] = role
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ""))


@pytest.fixture
async def controlcase(pgcase, tmp_path):
    identity_role = "fh_identity_" + uuid4().hex
    web_role = "fh_web_" + uuid4().hex
    stores = []
    roles = []
    try:
        for role, grant in ((identity_role, grant_identity), (web_role, grant_web)):
            await pgcase.admin.execute(
                f"CREATE ROLE {role} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                "NOINHERIT NOREPLICATION NOBYPASSRLS"
            )
            roles.append(role)
            await grant(pgcase.admin, role)
        identity = await PostgresIdentityStore.connect(role_dsn(pgcase.runtime_dsn, identity_role))
        stores.append(identity)
        web = await PostgresRunStore.connect(role_dsn(pgcase.runtime_dsn, web_role))
        stores.append(web)
        owner = await identity.bootstrap("org-a", "owner@example.test", "Owner-passphrase-1")
        users = []
        for name in ("user", "peer", "reviewer"):
            token = await identity.invite(owner.principal, "org-a", name + "@example.test")
            users.append(await identity.accept_invitation(token, "Strong-passphrase-1"))
        await identity.put_workspace(owner.principal, "org-a", "work-a", "Workspace A")
        ledger = SyntheticBusinessLedger(tmp_path / "independent-oracle.sqlite")
        ledger.seed("orders")
        fixture = application_fixture(ledger)
        registration = hosted_registration(fixture)
        await identity.put_deployment(
            owner.principal,
            "org-a",
            registration.deployment_id,
            "work-a",
            registration.application.id,
            registration.binding_digest,
        )
        for credentials in users:
            await identity.set_workspace_member(
                owner.principal,
                "org-a",
                "work-a",
                credentials.principal.account_id,
            )
            await identity.set_deployment_grant(
                owner.principal,
                "org-a",
                registration.deployment_id,
                credentials.principal.account_id,
                can_use=True,
                can_approve=credentials is users[2],
            )
        worker = await pgcase.seed(actor_id="service-worker", can_execute=True)
        runtime = PlatformRuntime(web, {registration.deployment_id: registration})
        yield ControlCase(
            pgcase,
            identity,
            web,
            runtime,
            owner,
            *users,
            fixture,
            registration,
            worker,
        )
    finally:
        for store in reversed(stores):
            await store.close()
        for role in reversed(roles):
            await pgcase.admin.execute(f"DROP OWNED BY {role}")
            await pgcase.admin.execute(f"DROP ROLE {role}")


async def accepted(case, key="request-1"):
    return await case.runtime.accept(
        case.user.principal,
        "org-a",
        "work-a",
        case.registration.deployment_id,
        "Update the synthetic record",
        key,
    )


def execution(case):
    return HostedRuntime(case.pg.store, {case.registration.deployment_id: case.registration})


async def test_atomic_accept_idempotency_owner_workspace_and_client_privileges(controlcase):
    case = controlcase
    a, b = await asyncio.gather(accepted(case), accepted(case))
    assert a["run_id"] == b["run_id"]
    assert await case.pg.admin.fetchval("SELECT count(*) FROM fh_cloud.run_bindings") == 1
    assert (
        await case.pg.admin.fetchval(
            "SELECT count(*) FROM fh_cloud.outbox WHERE kind='execution.accepted'"
        )
        == 1
    )
    for principal, workspace in (
        (case.peer.principal, "work-a"),
        (case.user.principal, "work-b"),
        (case.owner.principal, "work-a"),
    ):
        with pytest.raises(CloudDenied):
            await case.runtime.inspect(principal, "org-a", workspace, a["run_id"])
    with pytest.raises(CloudConflict):
        await case.runtime.accept(
            case.user.principal,
            "org-a",
            "work-a",
            case.registration.deployment_id,
            "A changed task",
            "request-1",
        )
    with pytest.raises(CloudDenied):
        await case.web.claim(
            TenantContext(org_id="org-a", actor_id=case.user.principal.account_id),
            a["run_id"],
            "web-worker",
        )
    async with case.web.pool.acquire() as connection:
        for sql in (
            "UPDATE fh_cloud.runs SET lease_until=clock_timestamp()",
            "UPDATE fh_cloud.execution_checkpoints SET revision=revision+1",
            "SELECT * FROM fh_cloud.invocations",
            "UPDATE fh_cloud.members SET can_execute=true",
        ):
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await connection.execute(sql)


@pytest.mark.parametrize("boundary", ["member", "workspace", "grant", "deployment"])
async def test_queued_revocation_prevents_every_physical_send(controlcase, boundary):
    case = controlcase
    receipt = await accepted(case)
    if boundary == "member":
        await case.identity.set_member(
            case.owner.principal,
            "org-a",
            case.user.principal.account_id,
            "member",
            False,
        )
    elif boundary == "workspace":
        await case.identity.set_workspace_member(
            case.owner.principal,
            "org-a",
            "work-a",
            case.user.principal.account_id,
            False,
        )
    elif boundary == "grant":
        await case.identity.set_deployment_grant(
            case.owner.principal,
            "org-a",
            case.registration.deployment_id,
            case.user.principal.account_id,
            active=False,
        )
    else:
        await case.identity.put_deployment(
            case.owner.principal,
            "org-a",
            case.registration.deployment_id,
            "work-a",
            case.registration.application.id,
            case.registration.binding_digest,
            False,
        )
    result = await execution(case).advance(case.worker, receipt["run_id"])
    assert result.run.status == "failed"
    assert not case.fixture.ledger.all_receipts()
    assert result.run.used_units == result.run.reserved_units == 0


async def test_historical_read_uses_pg_after_deactivation_and_registry_removal(controlcase):
    case = controlcase
    receipt = await accepted(case)
    await case.identity.put_deployment(
        case.owner.principal,
        "org-a",
        case.registration.deployment_id,
        "work-a",
        case.registration.application.id,
        case.registration.binding_digest,
        False,
    )
    reader = PlatformRuntime(case.web, {})
    view = await reader.inspect(case.user.principal, "org-a", "work-a", receipt["run_id"])
    assert view["status"] == "queued"
    assert "checkpoint" not in view and "messages" not in view
    with pytest.raises(CloudDenied):
        await accepted(case, "cannot-submit")


async def test_independent_decision_race_and_rejection_converge(controlcase):
    case = controlcase
    receipt = await accepted(case)
    runtime = execution(case)
    pending = await runtime.advance(case.worker, receipt["run_id"])
    assert pending.run.status == "awaiting_approval"
    detail = await case.runtime.approval_detail(
        case.reviewer.principal,
        "org-a",
        "work-a",
        receipt["run_id"],
    )
    # An owner/admin without a business grant never obtains the proposal.
    with pytest.raises(CloudDenied):
        await case.runtime.approval_detail(
            case.owner.principal,
            "org-a",
            "work-a",
            receipt["run_id"],
        )
    wrong = "f" * 64 if detail["fingerprint"] != "f" * 64 else "e" * 64
    with pytest.raises(CloudConflict):
        await case.runtime.decide(
            case.reviewer.principal,
            "org-a",
            "work-a",
            receipt["run_id"],
            "approve",
            wrong,
        )
    decisions = await asyncio.gather(
        *(
            case.runtime.decide(
                case.reviewer.principal,
                "org-a",
                "work-a",
                receipt["run_id"],
                value,
                detail["fingerprint"],
            )
            for value in ("reject", "approve")
        ),
        return_exceptions=True,
    )
    winners = [item for item in decisions if isinstance(item, dict)]
    assert len(winners) == 1
    assert len([item for item in decisions if isinstance(item, CloudConflict)]) == 1
    winner = winners[0]["decision"]
    assert (
        await case.runtime.decide(
            case.reviewer.principal,
            "org-a",
            "work-a",
            receipt["run_id"],
            winner,
            detail["fingerprint"],
        )
        == winners[0]
    )
    result = await runtime.advance(case.worker, receipt["run_id"])
    assert result.run.status == ("cancelled" if winner == "reject" else "succeeded")
    assert case.fixture.ledger.effect_count("orders") == (0 if winner == "reject" else 1)
    assert result.run.reserved_units == 0


async def test_expanded_permission_cannot_replace_a_queued_binding(controlcase):
    case = controlcase
    receipt = await accepted(case)
    before = await case.pg.admin.fetchrow("SELECT * FROM fh_cloud.run_bindings")
    await case.identity.put_workspace(case.owner.principal, "org-a", "work-b", "Workspace B")
    with pytest.raises(CloudConflict):
        await case.identity.put_deployment(
            case.owner.principal,
            "org-a",
            case.registration.deployment_id,
            "work-b",
            case.registration.application.id,
            case.registration.binding_digest,
        )
    after = await case.pg.admin.fetchrow("SELECT * FROM fh_cloud.run_bindings")
    assert before == after
    assert receipt["run_id"] == after["run_id"]


async def test_quota_is_atomic_but_retries_do_not_consume_a_second_slot(controlcase):
    case = controlcase
    case.runtime.limits = AdmissionLimits(user_pending=1)
    first = await accepted(case)
    assert (await accepted(case))["run_id"] == first["run_id"]
    with pytest.raises(CloudConflict, match="limit"):
        await accepted(case, "second")
    assert await case.pg.admin.fetchval("SELECT count(*) FROM fh_cloud.runs") == 1


async def test_worker_scans_durable_acceptance_and_stops_before_new_claim(controlcase):
    case = controlcase
    receipt = await accepted(case)
    worker = HostedWorker(execution(case), case.worker, worker_id="scanner")
    stop = asyncio.Event()
    stop.set()
    await worker.run(stop)
    assert worker.metrics.scans == 0
    assert await worker.tick() == 1
    assert worker.metrics.advanced == 1
    view = await case.runtime.inspect(case.user.principal, "org-a", "work-a", receipt["run_id"])
    assert view["status"] == "awaiting_approval"
    assert await worker.tick() == 0
    assert worker.metrics.idle == 1


async def test_revocation_after_model_result_blocks_tool_and_keeps_model_cost(controlcase):
    case = controlcase
    receipt = await accepted(case)
    original = case.registration

    async def verify_then_revoke(physical, outcome):
        result = await original.model_verifier(physical, outcome)
        await case.identity.set_deployment_grant(
            case.owner.principal,
            "org-a",
            original.deployment_id,
            case.user.principal.account_id,
            active=False,
        )
        return result

    registration = replace(original, model_verifier=verify_then_revoke)
    runtime = HostedRuntime(case.pg.store, {registration.deployment_id: registration})
    # The model was physically sent under current permission. Revocation after
    # its receipt may not erase that charge, but the next tool cannot be sent.
    with pytest.raises(CloudDenied):
        await runtime.advance(case.worker, receipt["run_id"])
    view = await execution(case).advance(case.worker, receipt["run_id"])
    assert view.run.status == "failed"
    assert view.run.used_units > 0 and view.run.reserved_units == 0
    assert len(case.fixture.ledger.all_receipts("model")) == 1
    assert case.fixture.ledger.all_receipts("tool") == ()


async def test_unknown_after_commit_survives_revocation_and_settles_without_resend(controlcase):
    case = controlcase
    receipt = await accepted(case)
    runtime = execution(case)
    await runtime.advance(case.worker, receipt["run_id"])
    detail = await case.runtime.approval_detail(
        case.reviewer.principal,
        "org-a",
        "work-a",
        receipt["run_id"],
    )
    await case.runtime.decide(
        case.reviewer.principal,
        "org-a",
        "work-a",
        receipt["run_id"],
        "approve",
        detail["fingerprint"],
    )
    case.fixture.faults.arm("write.after_commit", raise_after=True)
    unavailable = replace(case.registration, resolver=None)
    unknown_runtime = HostedRuntime(case.pg.store, {unavailable.deployment_id: unavailable})
    unknown = await unknown_runtime.advance(case.worker, receipt["run_id"])
    assert unknown.run.status == "reconciling"
    assert unknown.run.reserved_units > 0
    before = unknown.run.used_units
    assert case.fixture.ledger.effect_count("orders") == 1
    await case.identity.set_deployment_grant(
        case.owner.principal,
        "org-a",
        case.registration.deployment_id,
        case.user.principal.account_id,
        active=False,
    )
    still_unknown = await unknown_runtime.advance(case.worker, receipt["run_id"])
    assert still_unknown.run.reserved_units == unknown.run.reserved_units
    assert still_unknown.run.used_units == before
    settled = await runtime.advance(case.worker, receipt["run_id"])
    assert settled.run.status == "failed"
    assert settled.run.used_units > before and settled.run.reserved_units == 0
    assert case.fixture.ledger.effect_count("orders") == 1
    assert all(item.dispatches == 1 for item in case.fixture.ledger.all_receipts())


async def test_approver_grant_revocation_prevents_consumption(controlcase):
    case = controlcase
    receipt = await accepted(case)
    runtime = execution(case)
    await runtime.advance(case.worker, receipt["run_id"])
    detail = await case.runtime.approval_detail(
        case.reviewer.principal,
        "org-a",
        "work-a",
        receipt["run_id"],
    )
    await case.runtime.decide(
        case.reviewer.principal,
        "org-a",
        "work-a",
        receipt["run_id"],
        "approve",
        detail["fingerprint"],
    )
    await case.identity.set_deployment_grant(
        case.owner.principal,
        "org-a",
        case.registration.deployment_id,
        case.reviewer.principal.account_id,
        can_use=True,
        can_approve=False,
    )
    view = await runtime.advance(case.worker, receipt["run_id"])
    assert view.run.status == "awaiting_approval"
    assert case.fixture.ledger.effect_count("orders") == 0
    assert not await case.pg.admin.fetchval("SELECT bool_or(consumed) FROM fh_cloud.approvals")


async def test_mark_sent_rechecks_grant_after_successful_admission(controlcase):
    case = controlcase
    receipt = await accepted(case)
    ticket = await case.pg.store.claim(case.worker, receipt["run_id"], "send-boundary")
    calls = ExecutionCallStore(ExecutionStateStore(case.pg.store))
    envelope = tool_envelope(
        case.registration,
        ToolCall(
            id="read-record",
            name="oracle.read",
            arguments={"record_key": "record-1"},
        ),
    )
    admitted, _ = await calls.admit(
        case.worker,
        ticket,
        envelope,
        request=governance_request(envelope),
        expected_revision=0,
    )
    assert admitted.state == "admitted"
    await case.identity.set_deployment_grant(
        case.owner.principal,
        "org-a",
        case.registration.deployment_id,
        case.user.principal.account_id,
        active=False,
    )
    with pytest.raises(CloudDenied):
        await calls.mark_sent(case.worker, ticket, envelope.invocation_id)
    fact = await calls.get(case.worker, receipt["run_id"], envelope.invocation_id)
    assert fact.invocation.state == "admitted" and fact.receipt is None
    assert not case.fixture.ledger.all_receipts()
    # Revocation itself is not a zero-cost receipt. A newer epoch must first
    # fence the unsent admission before its reservation can be retired.
    assert (await case.pg.store.get(case.worker, receipt["run_id"])).reserved_units > 0
    await case.pg.store.release(case.worker, ticket)
    newer = await case.pg.store.claim(case.worker, receipt["run_id"], "send-retirement")
    await calls.retire_admission(case.worker, newer, envelope.invocation_id)
    assert (await case.pg.store.get(case.worker, receipt["run_id"])).reserved_units == 0


async def test_queue_keeps_later_proposals_after_an_earlier_approval_is_consumed(controlcase):
    case = controlcase
    receipt = await accepted(case)
    await execution(case).advance(case.worker, receipt["run_id"])
    # A consumed earlier action does not decide the current exact proposal. Its
    # durable decision remains immutable and must not hide this run wholesale.
    await case.pg.admin.execute(
        "INSERT INTO fh_cloud.approvals "
        "(org_id,approval_id,run_id,action_digest,approver_id,expires_at,consumed) "
        "VALUES('org-a','earlier-action',$1,$2,$3,clock_timestamp()+interval '1 day',true)",
        receipt["run_id"],
        "1" * 64,
        case.reviewer.principal.account_id,
    )
    await case.pg.admin.execute(
        "INSERT INTO fh_cloud.approval_decisions "
        "(org_id,run_id,action_digest,decision,decider_id,approval_id) "
        "VALUES('org-a',$1,$2,'approve',$3,'earlier-action')",
        receipt["run_id"],
        "1" * 64,
        case.reviewer.principal.account_id,
    )
    listed = await case.runtime.pending_approvals(case.reviewer.principal, "org-a", "work-a")
    assert [value["run_id"] for value in listed["items"]] == [receipt["run_id"]]
    assert listed["items"][0]["fingerprint"] != "1" * 64


async def test_shutdown_arriving_during_scan_does_not_take_a_new_lease(controlcase, monkeypatch):
    case = controlcase
    receipt = await accepted(case)
    runtime = execution(case)
    transaction = runtime.store.transaction
    stop = asyncio.Event()

    @asynccontextmanager
    async def stop_after_scan(*args, **kwargs):
        async with transaction(*args, **kwargs) as connection:
            yield connection
        stop.set()

    monkeypatch.setattr(runtime.store, "transaction", stop_after_scan)
    worker = HostedWorker(runtime, case.worker, worker_id="stopping-scanner")
    await worker.run(stop)
    assert worker.metrics.advanced == 0
    assert (
        await case.pg.admin.fetchval(
            "SELECT epoch FROM fh_cloud.runs WHERE run_id=$1",
            receipt["run_id"],
        )
        == 0
    )


async def test_authorized_second_workspace_cannot_rebind_an_existing_request_key(controlcase):
    case = controlcase
    first = await accepted(case)
    second = replace(case.registration, deployment_id="second-deployment")
    case.runtime = PlatformRuntime(
        case.web,
        {
            case.registration.deployment_id: case.registration,
            second.deployment_id: second,
        },
    )
    await case.identity.put_workspace(case.owner.principal, "org-a", "work-b", "Second")
    await case.identity.put_deployment(
        case.owner.principal,
        "org-a",
        second.deployment_id,
        "work-b",
        second.application.id,
        second.binding_digest,
    )
    await case.identity.set_workspace_member(
        case.owner.principal,
        "org-a",
        "work-b",
        case.user.principal.account_id,
    )
    await case.identity.set_deployment_grant(
        case.owner.principal,
        "org-a",
        second.deployment_id,
        case.user.principal.account_id,
    )
    with pytest.raises(CloudConflict):
        await case.runtime.accept(
            case.user.principal,
            "org-a",
            "work-b",
            second.deployment_id,
            "Update the synthetic record",
            "request-1",
        )
    assert await case.pg.admin.fetchval("SELECT count(*) FROM fh_cloud.runs") == 1
    stored = await case.pg.admin.fetchrow("SELECT * FROM fh_cloud.run_bindings")
    assert stored["run_id"] == first["run_id"] and stored["workspace_id"] == "work-a"


def test_admission_capacity_cannot_exceed_the_bounded_serial_scan():
    with pytest.raises(ValueError, match="100 pending"):
        AdmissionLimits(org_pending=101)
    with pytest.raises(ValueError, match="positive integers"):
        AdmissionLimits(user_pending=True)
    assert AdmissionLimits(org_pending=100).org_pending == 100


async def test_approval_on_another_deployment_does_not_keep_revoked_exact_grant(controlcase):
    case = controlcase
    receipt = await accepted(case)
    runtime = execution(case)
    await runtime.advance(case.worker, receipt["run_id"])
    detail = await case.runtime.approval_detail(
        case.reviewer.principal,
        "org-a",
        "work-a",
        receipt["run_id"],
    )
    await case.runtime.decide(
        case.reviewer.principal,
        "org-a",
        "work-a",
        receipt["run_id"],
        "approve",
        detail["fingerprint"],
    )
    other = replace(case.registration, deployment_id="another-deployment")
    await case.identity.put_deployment(
        case.owner.principal,
        "org-a",
        other.deployment_id,
        "work-a",
        other.application.id,
        other.binding_digest,
    )
    await case.identity.set_deployment_grant(
        case.owner.principal,
        "org-a",
        other.deployment_id,
        case.reviewer.principal.account_id,
        can_approve=True,
    )
    await case.identity.set_deployment_grant(
        case.owner.principal,
        "org-a",
        case.registration.deployment_id,
        case.reviewer.principal.account_id,
        can_approve=False,
    )
    assert await case.pg.admin.fetchval(
        "SELECT can_approve FROM fh_cloud.members WHERE actor_id=$1",
        case.reviewer.principal.account_id,
    )
    with pytest.raises(CloudDenied):
        await runtime.advance(case.worker, receipt["run_id"])
    assert not await case.pg.admin.fetchval("SELECT bool_or(consumed) FROM fh_cloud.approvals")
    assert case.fixture.ledger.effect_count("orders") == 0
