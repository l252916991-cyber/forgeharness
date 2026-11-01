"""Rejected logical calls and physical-result facts remain distinct and atomic."""

from __future__ import annotations

import json
import time

import asyncpg
import pytest

from forgeharness.cloud import execution_state
from forgeharness.cloud.models import CloudBudgetExceeded, CloudConflict, CloudDenied, StaleLease
from forgeharness.domain.models import Message, MessageRole, RunStatus, ToolCall, Usage
from forgeharness.tools.base import ToolErrorCode, ToolFailure

from .test_execution_calls import active, envelope, physical, request
from .test_execution_state import prepared


def rejected(previous, evidence, cause="policy_denied", *, logical_id="denied-1"):
    call = ToolCall(id=logical_id, name="synthetic", arguments={"text": "original"})
    if cause == "policy_denied":
        content = "policy denied tool: resource is outside the owner scope"
        evidence.append(
            "policy.decided",
            {
                "call_id": call.id,
                "tool": call.name,
                "decision": "deny",
                "reason": "resource is outside the owner scope",
            },
        )
    elif cause == "unknown_tool":
        failure = ToolFailure(
            code=ToolErrorCode.INVALID_ARGUMENTS,
            message="No such tool is registered.",
            recoverable=True,
            details={"tool": call.name},
            suggestion="Choose one of the available tools.",
        )
        content = failure.as_observation()
        evidence.append(
            "tool.unknown",
            {
                "call_id": call.id,
                "tool": call.name,
                "error_code": failure.code.value,
                "recoverable": True,
            },
        )
    else:
        failure = ToolFailure(
            code=ToolErrorCode.INVALID_ARGUMENTS,
            message="The arguments do not match the tool's required schema.",
            recoverable=True,
            suggestion="Send arguments that satisfy the tool's input schema.",
        )
        content = failure.as_observation()
        evidence.append(
            "tool.invalid_arguments",
            {
                "call_id": call.id,
                "tool": call.name,
                "error_code": failure.code.value,
                "recoverable": True,
            },
        )
    return previous.model_copy(
        update={
            "status": RunStatus.RUNNING,
            "messages": (
                *previous.messages,
                Message(role=MessageRole.ASSISTANT, tool_calls=(call,)),
                Message(
                    role=MessageRole.TOOL,
                    tool_call_id=call.id,
                    tool_name=call.name,
                    content=content,
                ),
            ),
            "usage": previous.usage.model_copy(
                update={"tool_calls": previous.usage.tool_calls + 1}
            ),
        }
    )


@pytest.mark.parametrize("cause", ["policy_denied", "unknown_tool", "invalid_arguments"])
async def test_logical_rejection_consumes_budget_once_without_physical_call_or_receipt(
    pgcase, cause
):
    state, owner, worker, run, ticket, initial, evidence = await prepared(pgcase)
    candidate = rejected(initial, evidence, cause)
    saved = await state.save(worker, ticket, candidate, evidence)
    assert saved.usage == Usage(tool_calls=1) and not evidence.pending()
    fact = await pgcase.admin.fetchrow("SELECT * FROM fh_cloud.execution_logical_usage")
    assert fact["logical_id"] == "denied-1" and fact["cause"] == cause
    assert fact["checkpoint_revision"] == saved.checkpoint_revision
    assert json.loads(fact["usage"]) == Usage(tool_calls=1).model_dump(mode="json")
    assert await pgcase.admin.fetchval("SELECT COUNT(*) FROM fh_cloud.invocations") == 0
    assert await pgcase.admin.fetchval("SELECT COUNT(*) FROM fh_cloud.receipts") == 0
    public = await pgcase.store.get(owner, run.run_id)
    assert public.used_units == public.reserved_units == 0
    resumed = await state.load(owner, run.run_id)
    await state.save(worker, ticket, resumed, evidence)
    assert await pgcase.admin.fetchval("SELECT COUNT(*) FROM fh_cloud.execution_logical_usage") == 1
    assert (await state.load(owner, run.run_id)).usage.tool_calls == 1


@pytest.mark.parametrize("mutation", ["omit_event", "observation", "call", "undercharge"])
async def test_logical_charge_requires_exact_new_message_pair_and_committed_denial(
    pgcase, mutation
):
    state, owner, worker, run, ticket, initial, evidence = await prepared(pgcase)
    candidate = rejected(initial, evidence)
    if mutation == "omit_event":
        evidence = await state.trace(owner, run.run_id)
    elif mutation == "observation":
        candidate = candidate.model_copy(
            update={
                "messages": (
                    *candidate.messages[:-1],
                    candidate.messages[-1].model_copy(update={"content": "invented rejection"}),
                )
            }
        )
    elif mutation == "call":
        candidate = candidate.model_copy(
            update={
                "messages": (
                    *candidate.messages[:-1],
                    candidate.messages[-1].model_copy(update={"tool_call_id": "another-id"}),
                )
            }
        )
    else:
        candidate = candidate.model_copy(update={"usage": Usage()})
    with pytest.raises(CloudConflict, match="usage differs"):
        await state.save(worker, ticket, candidate, evidence)
    assert await state.load(owner, run.run_id) == initial
    assert await pgcase.admin.fetchval("SELECT COUNT(*) FROM fh_cloud.execution_logical_usage") == 0
    assert await pgcase.admin.fetchval("SELECT COUNT(*) FROM fh_cloud.execution_evidence") == 0


async def test_denial_fact_checkpoint_trace_and_usage_rollback_together(pgcase):
    state, owner, worker, run, ticket, initial, evidence = await prepared(pgcase)
    candidate = rejected(initial, evidence)
    await pgcase.admin.execute(
        "ALTER TABLE fh_cloud.execution_checkpoints ADD CONSTRAINT fail_after_denial "
        "CHECK (revision=0)"
    )
    with pytest.raises(asyncpg.CheckViolationError):
        await state.save(worker, ticket, candidate, evidence)
    assert evidence.pending() and await state.load(owner, run.run_id) == initial
    for table in ("execution_logical_usage", "execution_evidence"):
        assert await pgcase.admin.fetchval(f"SELECT COUNT(*) FROM fh_cloud.{table}") == 0
    assert json.loads(
        await pgcase.admin.fetchval("SELECT used_usage FROM fh_cloud.execution_checkpoints")
    ) == Usage().model_dump(mode="json")


async def test_rejected_logical_identity_cannot_be_replayed_or_physically_admitted(pgcase):
    calls, owner, worker, run, ticket, current, evidence = await active(pgcase)
    saved = await calls.state.save(worker, ticket, rejected(current, evidence), evidence)
    item = envelope(logical_id="denied-1")
    with pytest.raises(CloudConflict, match="already rejected"):
        await calls.admit(
            worker, ticket, item, request=request(item), expected_revision=saved.checkpoint_revision
        )
    with pytest.raises(CloudConflict, match="identity or evidence was reused"):
        await calls.state.save(worker, ticket, rejected(saved, evidence), evidence)
    assert (await calls.state.load(owner, run.run_id)).usage == Usage(tool_calls=1)
    assert await pgcase.admin.fetchval("SELECT COUNT(*) FROM fh_cloud.execution_logical_usage") == 1
    assert await pgcase.admin.fetchval("SELECT COUNT(*) FROM fh_cloud.invocations") == 0


async def test_physical_identity_cannot_also_be_charged_as_a_logical_denial(pgcase):
    calls, _, worker, run, ticket, current, evidence = await active(pgcase)
    item = envelope(logical_id="denied-1")
    await calls.admit(
        worker, ticket, item, request=request(item), expected_revision=current.checkpoint_revision
    )
    await calls.mark_sent(worker, ticket, item.invocation_id)
    result = physical()
    await calls.record_result(worker, run.run_id, item.invocation_id, result)
    await calls.settle(worker, ticket, item.invocation_id)
    current = await calls.state.save(
        worker, ticket, current.model_copy(update={"usage": result.actual_usage}), evidence
    )
    with pytest.raises(CloudConflict, match="identity was already used"):
        await calls.state.save(worker, ticket, rejected(current, evidence), evidence)
    assert await pgcase.admin.fetchval("SELECT COUNT(*) FROM fh_cloud.execution_logical_usage") == 0


async def test_logical_denials_count_toward_tool_and_total_call_limits(pgcase):
    calls, _, worker, _, ticket, current, evidence = await active(pgcase)
    await pgcase.admin.execute(
        "UPDATE fh_cloud.execution_checkpoints SET budget=jsonb_set(budget,'{max_tool_calls}','1')"
    )
    saved = await calls.state.save(worker, ticket, rejected(current, evidence), evidence)
    with pytest.raises(CloudConflict, match="tool-call budget"):
        await calls.state.save(
            worker, ticket, rejected(saved, evidence, logical_id="denied-2"), evidence
        )
    await pgcase.admin.execute(
        "UPDATE fh_cloud.runs SET definition=jsonb_set(definition,'{max_calls}','1')"
    )
    item = envelope("model")
    with pytest.raises(CloudBudgetExceeded, match="call budget"):
        await calls.admit(
            worker, ticket, item, request=request(item), expected_revision=saved.checkpoint_revision
        )
    assert await pgcase.admin.fetchval("SELECT COUNT(*) FROM fh_cloud.invocations") == 0


async def test_logical_facts_are_tenant_scoped_and_immutable_for_runtime_role(pgcase):
    state, _, worker, _, ticket, initial, evidence = await prepared(pgcase)
    await state.save(worker, ticket, rejected(initial, evidence), evidence)
    foreign = await pgcase.seed("org-b", "foreign")
    async with pgcase.store.transaction(foreign) as connection:
        assert (
            await connection.fetchval("SELECT COUNT(*) FROM fh_cloud.execution_logical_usage") == 0
        )
    async with pgcase.store.transaction(worker) as connection:
        assert (
            await connection.fetchval("SELECT COUNT(*) FROM fh_cloud.execution_logical_usage") == 1
        )
    for statement in (
        "UPDATE fh_cloud.execution_logical_usage SET cause='unknown_tool'",
        "DELETE FROM fh_cloud.execution_logical_usage",
        "INSERT INTO fh_cloud.execution_logical_usage "
        "(org_id,run_id,logical_id,cause,call,observation,event_hash,checkpoint_revision) "
        "SELECT 'org-b',run_id,logical_id,cause,call,observation,event_hash,checkpoint_revision "
        "FROM fh_cloud.execution_logical_usage",
    ):
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            async with pgcase.store.transaction(worker) as connection:
                await connection.execute(statement)


async def test_logical_usage_projection_revalidates_its_exact_denial_evidence(pgcase):
    state, owner, worker, run, ticket, initial, evidence = await prepared(pgcase)
    current = await state.save(worker, ticket, rejected(initial, evidence), evidence)
    await pgcase.admin.execute("UPDATE fh_cloud.execution_logical_usage SET cause='unknown_tool'")
    with pytest.raises(CloudConflict, match="logical usage fact differs"):
        await state.save(worker, ticket, current, evidence)
    assert await state.load(owner, run.run_id) == current


@pytest.mark.parametrize(
    "tamper", ["kind", "receipt_id", "units", "digest", "not_executed", "usage", "body"]
)
async def test_checkpoint_revalidates_full_settled_result_receipt_and_usage(pgcase, tamper):
    calls, owner, worker, run, ticket, current, evidence = await active(pgcase)
    item = envelope()
    await calls.admit(
        worker, ticket, item, request=request(item), expected_revision=current.checkpoint_revision
    )
    await calls.mark_sent(worker, ticket, item.invocation_id)
    result = physical()
    await calls.record_result(worker, run.run_id, item.invocation_id, result)
    await calls.settle(worker, ticket, item.invocation_id)
    if tamper == "kind":
        await pgcase.admin.execute("UPDATE fh_cloud.execution_calls SET kind='model'")
    elif tamper == "receipt_id":
        await pgcase.admin.execute("UPDATE fh_cloud.invocations SET receipt_id='another'")
    elif tamper == "units":
        await pgcase.admin.execute("UPDATE fh_cloud.receipts SET actual_units=0")
    elif tamper == "digest":
        await pgcase.admin.execute("UPDATE fh_cloud.receipts SET result_digest=$1", "b" * 64)
    elif tamper == "not_executed":
        await pgcase.admin.execute(
            "UPDATE fh_cloud.receipts SET outcome='not_executed',actual_units=0"
        )
    elif tamper == "usage":
        await pgcase.admin.execute(
            "UPDATE fh_cloud.execution_calls SET actual_usage=$1::jsonb", Usage().model_dump_json()
        )
    else:
        await pgcase.admin.execute("UPDATE fh_cloud.execution_calls SET canonical_output='{}'")
    with pytest.raises(CloudConflict, match="settled execution facts"):
        await calls.state.save(
            worker, ticket, current.model_copy(update={"usage": result.actual_usage}), evidence
        )
    assert await calls.state.load(owner, run.run_id) == current


async def test_only_current_executor_can_read_suspended_org_state_for_reconciliation(pgcase):
    state, owner, worker, run, _, initial, _ = await prepared(pgcase)
    await pgcase.admin.execute("UPDATE fh_cloud.organizations SET active=false")
    for context in (owner, worker):
        with pytest.raises(CloudDenied):
            await state.load(context, run.run_id)
        with pytest.raises(CloudDenied):
            await state.trace(context, run.run_id)
    assert await state.load(worker, run.run_id, internal_worker=True) == initial
    assert not (await state.trace(worker, run.run_id, internal_worker=True)).events
    with pytest.raises(CloudDenied):
        await state.load(owner, run.run_id, internal_worker=True)
    with pytest.raises(CloudDenied):
        await state.trace(owner, run.run_id, internal_worker=True)


async def test_lease_expiring_during_settled_result_validation_cannot_commit(pgcase, monkeypatch):
    calls, owner, worker, run, ticket, current, evidence = await active(pgcase)
    item = envelope()
    await calls.admit(
        worker, ticket, item, request=request(item), expected_revision=current.checkpoint_revision
    )
    await calls.mark_sent(worker, ticket, item.invocation_id)
    result = physical()
    await calls.record_result(worker, run.run_id, item.invocation_id, result)
    await calls.settle(worker, ticket, item.invocation_id)
    before_events = await pgcase.store.events(owner, run.run_id)
    evidence.append("run.stopping", {"reason": "expiring while validating physical result"})
    ticket = await pgcase.store.renew(worker, ticket, ttl_seconds=0.2)
    original = execution_state.canon_result_digest
    validations = 0

    def delayed_validation(value):
        nonlocal validations
        validations += 1
        # Represent a bounded, expensive full-result canonicalization after the
        # earlier lease check. No provider, sleep polling or external race needed.
        time.sleep(0.25)
        return original(value)

    monkeypatch.setattr(execution_state, "canon_result_digest", delayed_validation)
    with pytest.raises(StaleLease):
        await calls.state.save(
            worker,
            ticket,
            current.model_copy(update={"usage": result.actual_usage, "status": RunStatus.FAILED}),
            evidence,
        )
    assert validations == 1
    assert await calls.state.load(owner, run.run_id) == current
    assert await pgcase.store.events(owner, run.run_id) == before_events
    assert evidence.pending()
    assert await pgcase.admin.fetchval("SELECT COUNT(*) FROM fh_cloud.execution_evidence") == 0
