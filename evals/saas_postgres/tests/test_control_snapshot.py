"""A row-lock wait must not combine a new run tuple with an old proposal."""

from __future__ import annotations

import asyncio

import pytest

from forgeharness.cloud.models import CloudConflict

from .test_control_runtime import accepted, controlcase, execution

__all__ = ["controlcase"]


async def _wait_for_bound_lock(writer):
    async with asyncio.timeout(5):
        for _ in range(500):
            waiting = await writer.fetchval(
                "SELECT EXISTS(SELECT 1 FROM pg_stat_activity "
                "WHERE datname=current_database() AND pid<>pg_backend_pid() "
                "AND wait_event_type='Lock' AND query LIKE '%FROM fh_cloud.runs r%' "
                "AND (query LIKE '%FOR SHARE OF r%' OR query LIKE '%FOR UPDATE OF r%'))"
            )
            if waiting:
                return
            await asyncio.sleep(0.01)
    raise AssertionError("reader did not wait for the run row lock")


@pytest.mark.parametrize("operation", ["detail", "decide"])
async def test_bound_reads_checkpoint_after_acquiring_run_lock(controlcase, operation):
    case = controlcase
    run_id = (await accepted(case))["run_id"]
    pending = await execution(case).advance(case.worker, run_id)
    assert pending.run.status == "awaiting_approval"
    old = await case.runtime.approval_detail(case.reviewer.principal, "org-a", "work-a", run_id)
    # Simulate a worker committing the next exact proposal while retaining the
    # public awaiting_approval status. Both r and c receive a new tuple version.
    arguments = dict(pending.checkpoint.pending_approval.call.arguments)
    arguments["value"] = "new-committed-proposal"
    call = pending.checkpoint.pending_approval.call.model_copy(update={"arguments": arguments})
    proposal = pending.checkpoint.pending_approval.model_copy(update={"call": call})
    checkpoint = pending.checkpoint.model_copy(
        update={
            "pending_approval": proposal,
            "checkpoint_revision": pending.checkpoint.checkpoint_revision + 1,
        }
    )
    async with case.pg.admin.transaction():
        await case.pg.admin.fetchrow(
            "SELECT * FROM fh_cloud.runs WHERE org_id=$1 AND run_id=$2 FOR UPDATE",
            "org-a",
            run_id,
        )
        if operation == "detail":
            reader = asyncio.create_task(
                case.runtime.approval_detail(case.reviewer.principal, "org-a", "work-a", run_id)
            )
        else:
            reader = asyncio.create_task(
                case.runtime.decide(
                    case.reviewer.principal,
                    "org-a",
                    "work-a",
                    run_id,
                    "approve",
                    old["fingerprint"],
                )
            )
        await _wait_for_bound_lock(case.pg.admin)
        assert not reader.done()
        await case.pg.admin.execute(
            "UPDATE fh_cloud.execution_checkpoints SET checkpoint=$3::jsonb,revision=revision+1 "
            "WHERE org_id=$1 AND run_id=$2",
            "org-a",
            run_id,
            checkpoint.model_dump_json(),
        )
        await case.pg.admin.execute(
            "UPDATE fh_cloud.runs SET status='awaiting_approval' WHERE org_id=$1 AND run_id=$2",
            "org-a",
            run_id,
        )
    if operation == "detail":
        detail = await reader
        assert detail["proposal"] == call.model_dump(mode="json")
        assert detail["fingerprint"] != old["fingerprint"]
    else:
        with pytest.raises(CloudConflict):
            await reader
        assert not await case.pg.admin.fetchval(
            "SELECT EXISTS(SELECT 1 FROM fh_cloud.approval_decisions "
            "WHERE org_id=$1 AND run_id=$2)",
            "org-a",
            run_id,
        )
        assert not await case.pg.admin.fetchval(
            "SELECT EXISTS(SELECT 1 FROM fh_cloud.approvals WHERE org_id=$1 AND run_id=$2)",
            "org-a",
            run_id,
        )
