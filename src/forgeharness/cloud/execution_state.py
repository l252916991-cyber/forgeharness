"""PG-authoritative execution input, CAS checkpoints and committed trace batches.

These operations are the persistence side of the async execution boundary. They
do not dispatch a model/tool or by themselves implement AsyncExecutionBackend.
Call admission/settlement must compose the in-transaction checkpoint operation
with their own reservations and facts; a separate SQLite authority is forbidden.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from itertools import pairwise
from typing import Any

from forgeharness.cloud.evidence import BufferedEvidence
from forgeharness.cloud.execution_contract import PhysicalResult, canon_result_digest
from forgeharness.cloud.models import (
    CloudBudgetExceeded,
    CloudConflict,
    CloudDenied,
    LeaseTicket,
    Receipt,
    RunDefinition,
    RunRecord,
    TenantContext,
)
from forgeharness.cloud.store import PostgresRunStore
from forgeharness.domain.models import (
    Message,
    MessageRole,
    RunContext,
    RunResult,
    RunStatus,
    ToolCall,
    Usage,
)
from forgeharness.runtime.budget import RunBudget
from forgeharness.sdk.privacy import CredentialMask, EvidenceTransform
from forgeharness.tools.base import ToolErrorCode, ToolFailure

_TERMINAL = {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED, RunStatus.EXHAUSTED}
_LOGICAL_USAGE = Usage(tool_calls=1)


@dataclass(frozen=True)
class _LogicalRejection:
    call: ToolCall
    observation: Message
    cause: str
    event_hash: str


def _rejection_matches(call: ToolCall, observation: Message, event: dict[str, Any]) -> str | None:
    payload = event["payload"]
    if (
        observation.role is not MessageRole.TOOL
        or observation.tool_call_id != call.id
        or observation.tool_name != call.name
        or payload.get("call_id") != call.id
        or payload.get("tool") != call.name
    ):
        return None
    if (
        event["type"] == "policy.decided"
        and payload.get("decision") == "deny"
        and isinstance(payload.get("reason"), str)
        and observation.content == f"policy denied tool: {payload['reason']}"
    ):
        return "policy_denied"
    unknown = ToolFailure(
        code=ToolErrorCode.INVALID_ARGUMENTS,
        message="No such tool is registered.",
        recoverable=True,
        details={"tool": call.name},
        suggestion="Choose one of the available tools.",
    )
    if (
        event["type"] == "tool.unknown"
        and payload.get("error_code") == unknown.code.value
        and payload.get("recoverable") is True
        and observation.content == unknown.as_observation()
    ):
        return "unknown_tool"
    invalid = ToolFailure(
        code=ToolErrorCode.INVALID_ARGUMENTS,
        message="The arguments do not match the tool's required schema.",
        recoverable=True,
        suggestion="Send arguments that satisfy the tool's input schema.",
    )
    if (
        event["type"] == "tool.invalid_arguments"
        and payload.get("error_code") == invalid.code.value
        and payload.get("recoverable") is True
        and observation.content == invalid.as_observation()
    ):
        return "invalid_arguments"
    return None


def _new_logical_rejections(
    previous: RunResult, result: RunResult, batch: tuple[str, ...]
) -> tuple[_LogicalRejection, ...]:
    """Only a new exact transcript pair and its fresh denial event consume usage."""
    prefix = len(previous.messages)
    if result.messages[:prefix] != previous.messages:
        return ()
    prior_ids = {call.id for message in previous.messages for call in message.tool_calls}
    messages = result.messages[prefix:]
    events = [json.loads(item) for item in batch]
    rejections = []
    seen = set(prior_ids)
    for proposal, observation in pairwise(messages):
        if proposal.role is not MessageRole.ASSISTANT or len(proposal.tool_calls) != 1:
            continue
        call = proposal.tool_calls[0]
        matches = [
            (cause, envelope["hash"])
            for envelope in events
            if (cause := _rejection_matches(call, observation, envelope["event"])) is not None
        ]
        if not matches:
            continue
        if call.id in seen or len(matches) != 1:
            raise CloudConflict("logical rejection identity or evidence was reused")
        seen.add(call.id)
        cause, event_hash = matches[0]
        rejections.append(_LogicalRejection(call, observation, cause, event_hash))
    return tuple(rejections)


def _json(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    if len(encoded.encode()) > 16 * 1024 * 1024:
        raise ValueError("execution document exceeds limit")
    return encoded


def _decode(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def execution_input_digest(task: str, instructions: tuple[Message, ...]) -> str:
    return hashlib.sha256(
        _json(
            {
                "task": task,
                "instructions": [m.model_dump(mode="json") for m in instructions],
            }
        ).encode()
    ).hexdigest()


class ExecutionStateStore:
    def __init__(
        self, runs: PostgresRunStore, *, transform: EvidenceTransform | None = None
    ) -> None:
        self.runs = runs
        self.transform = transform or CredentialMask()

    def _clean(self, value: Any) -> None:
        if CredentialMask().transform(value) != value or self.transform.transform(value) != value:
            raise ValueError("execution evidence contains credentials")
        _json(value)

    async def accept(
        self,
        context: TenantContext,
        request_key: str,
        definition: RunDefinition,
        *,
        task: str,
        budget: RunBudget,
        create_context: Callable[[str], RunContext],
        instructions: tuple[Message, ...] = (),
        accept_existing_deadline: bool = False,
        admission_guard: Callable[[Any], Awaitable[None]] | None = None,
        bind_run: Callable[[Any, RunRecord, bool], Awaitable[None]] | None = None,
    ) -> RunRecord:
        """Accept complete initial input/checkpoint and scheduling intent atomically.

        create_context is a trusted host function, never a client import/path.
        Existing governance-only runs cannot be silently adopted as executions.
        """
        if not isinstance(task, str) or not 1 <= len(task) <= 64_000:
            raise ValueError("execution task needs 1-64000 characters")
        budget = RunBudget.model_validate_json(budget.model_dump_json())
        instructions = tuple(Message.model_validate_json(m.model_dump_json()) for m in instructions)
        if any(m.role != MessageRole.SYSTEM for m in instructions):
            raise ValueError("execution instructions must be system messages")
        if budget.max_attempts_per_call != 1:
            raise ValueError("hosted serial profile requires one physical attempt per call")
        if execution_input_digest(task, instructions) != definition.input_digest:
            raise CloudConflict("execution input differs from its immutable digest")
        initial_input = {
            "task": task,
            "instructions": [m.model_dump(mode="json") for m in instructions],
        }
        self._clean(initial_input)
        async with self.runs.transaction(context) as connection:
            if admission_guard is not None:
                await admission_guard(connection)
            run, created = await self.runs.submit_in_transaction(
                connection,
                context,
                request_key,
                definition,
                accept_existing_deadline=accept_existing_deadline,
            )
            if bind_run is not None:
                await bind_run(connection, run, created)
            owner = RunContext.model_validate_json(create_context(run.run_id).model_dump_json())
            if (
                owner.run_id != run.run_id
                or owner.actor.id != context.actor_id
                or owner.scope.tenant_id != context.org_id
                or owner.scope.workspace is not None
                or (owner.application_id, owner.application_version, owner.policy_version)
                != (
                    definition.application_id,
                    definition.application_version,
                    definition.policy_version,
                )
            ):
                raise CloudDenied("hosted execution context does not match the accepted binding")
            owner_json = owner.model_dump(mode="json")
            self._clean(owner_json)
            row = await connection.fetchrow(
                "SELECT * FROM fh_cloud.execution_checkpoints WHERE org_id=$1 AND run_id=$2",
                context.org_id,
                run.run_id,
            )
            if not created:
                if row is None or any(
                    _decode(row[key]) != value
                    for key, value in {
                        "owner_context": owner_json,
                        "budget": budget.model_dump(mode="json"),
                        "initial_input": initial_input,
                    }.items()
                ):
                    raise CloudConflict("request key was used for another execution binding")
                return run
            initial = RunResult(
                task_id=run.run_id,
                status=RunStatus.CREATED,
                messages=(*instructions, Message(role=MessageRole.USER, content=task)),
                usage=Usage(),
                run_context=owner,
            )
            await connection.execute(
                "INSERT INTO fh_cloud.execution_checkpoints "
                "(org_id,run_id,budget,owner_context,initial_input,checkpoint) "
                "VALUES($1,$2,$3::jsonb,$4::jsonb,$5::jsonb,$6::jsonb)",
                context.org_id,
                run.run_id,
                budget.model_dump_json(),
                owner.model_dump_json(),
                _json(initial_input),
                initial.model_dump_json(),
            )
            await self.runs.emit(
                connection, context.org_id, run.run_id, "execution.accepted", {"revision": 0}
            )
            return run

    async def load(
        self, context: TenantContext, run_id: str, *, internal_worker: bool = False
    ) -> RunResult:
        async with self.runs.transaction(
            context, "execute" if internal_worker else "member", allow_inactive_org=internal_worker
        ) as connection:
            await self.runs._visible_run(connection, context, run_id)
            row = await connection.fetchrow(
                "SELECT checkpoint FROM fh_cloud.execution_checkpoints "
                "WHERE org_id=$1 AND run_id=$2",
                context.org_id,
                run_id,
            )
            if row is None:
                raise CloudDenied("execution unavailable")
            return RunResult.model_validate(_decode(row["checkpoint"]))

    async def trace(
        self, context: TenantContext, run_id: str, *, internal_worker: bool = False
    ) -> BufferedEvidence:
        async with self.runs.transaction(
            context, "execute" if internal_worker else "member", allow_inactive_org=internal_worker
        ) as connection:
            await self.runs._visible_run(connection, context, run_id)
            row = await connection.fetchrow(
                "SELECT trace_sequence,trace_hash FROM fh_cloud.execution_checkpoints "
                "WHERE org_id=$1 AND run_id=$2 FOR SHARE",
                context.org_id,
                run_id,
            )
            if row is None:
                raise CloudDenied("execution unavailable")
            history = await self._history(connection, context.org_id, run_id)
            if len(history) != row["trace_sequence"]:
                raise CloudConflict("execution evidence is incomplete")
            result = BufferedEvidence.from_json(
                run_id,
                _json(history),
                transform=self.transform,
                expected_head=row["trace_hash"],
            )
            result.ack_committed(result.snapshot_pending())
            return result

    @staticmethod
    async def _history(connection: Any, org_id: str, run_id: str) -> list[Any]:
        rows = await connection.fetch(
            "SELECT envelope FROM fh_cloud.execution_evidence "
            "WHERE org_id=$1 AND run_id=$2 ORDER BY sequence",
            org_id,
            run_id,
        )
        return [_decode(row["envelope"]) for row in rows]

    async def save(
        self,
        context: TenantContext,
        ticket: LeaseTicket,
        result: RunResult,
        evidence: BufferedEvidence,
    ) -> RunResult:
        batch = evidence.snapshot_pending()
        async with self.runs.transaction(context, "execute", allow_inactive_org=True) as connection:
            saved = await self.save_in_transaction(connection, context, ticket, result, batch)
        evidence.ack_committed(batch)
        return saved

    async def commit_trace_in_transaction(
        self, connection: Any, context: TenantContext, ticket: LeaseTicket, batch: tuple[str, ...]
    ) -> tuple[int, str]:
        """Commit exact evidence/cursor without changing the runtime control revision.

        Admission and settlement can change physical facts while the RunResult's
        message cursor remains the same. Caller owns the authorized transaction.
        """
        await self.runs.lock_lease(connection, context, ticket)
        row = await connection.fetchrow(
            "SELECT trace_sequence,trace_hash FROM fh_cloud.execution_checkpoints "
            "WHERE org_id=$1 AND run_id=$2 FOR UPDATE",
            context.org_id,
            ticket.run_id,
        )
        if row is None:
            raise CloudDenied("execution unavailable")
        history = await self._history(connection, context.org_id, ticket.run_id)
        if len(history) != row["trace_sequence"]:
            raise CloudConflict("execution evidence is incomplete")
        BufferedEvidence.from_json(
            ticket.run_id, _json(history), transform=self.transform, expected_head=row["trace_hash"]
        )
        combined = BufferedEvidence.from_json(
            ticket.run_id,
            _json([*history, *(json.loads(item) for item in batch)]),
            transform=self.transform,
        ).snapshot_pending()
        head = json.loads(combined[-1])["hash"] if combined else ""
        for item in batch:
            envelope = json.loads(item)
            await connection.execute(
                "INSERT INTO fh_cloud.execution_evidence"
                "(org_id,run_id,sequence,event_hash,envelope) VALUES($1,$2,$3,$4,$5::jsonb)",
                context.org_id,
                ticket.run_id,
                envelope["event"]["sequence"],
                envelope["hash"],
                item,
            )
        await self.runs.lock_lease(connection, context, ticket)
        await connection.execute(
            "UPDATE fh_cloud.execution_checkpoints SET trace_sequence=$3,trace_hash=$4 "
            "WHERE org_id=$1 AND run_id=$2",
            context.org_id,
            ticket.run_id,
            len(combined),
            head,
        )
        return len(combined), head

    async def save_in_transaction(
        self,
        connection: Any,
        context: TenantContext,
        ticket: LeaseTicket,
        result: RunResult,
        batch: tuple[str, ...],
    ) -> RunResult:
        """Caller owns authorized tenant transaction; never invoke a provider here."""
        run = await self.runs.lock_lease(connection, context, ticket)
        row = await connection.fetchrow(
            "SELECT * FROM fh_cloud.execution_checkpoints WHERE org_id=$1 AND run_id=$2 FOR UPDATE",
            context.org_id,
            ticket.run_id,
        )
        if row is None:
            raise CloudDenied("execution unavailable")
        previous = RunResult.model_validate(_decode(row["checkpoint"]))
        if (
            result.task_id != ticket.run_id
            or result.checkpoint_revision != row["revision"]
            or result.run_context is None
            or result.run_context.model_dump(mode="json") != _decode(row["owner_context"])
            or previous.status in _TERMINAL
        ):
            raise CloudConflict("execution checkpoint binding or revision changed")
        used = Usage.model_validate_json(_json(_decode(row["used_usage"])), strict=True)
        self._clean(result.model_dump(mode="json"))
        history = await self._history(connection, context.org_id, ticket.run_id)
        if len(history) != row["trace_sequence"]:
            raise CloudConflict("execution evidence is incomplete")
        BufferedEvidence.from_json(
            ticket.run_id,
            _json(history),
            transform=self.transform,
            expected_head=row["trace_hash"],
        )
        combined = BufferedEvidence.from_json(
            ticket.run_id,
            _json([*history, *(json.loads(item) for item in batch)]),
            transform=self.transform,
        )
        rejections = _new_logical_rejections(previous, result, batch)
        if rejections:
            budget = RunBudget.model_validate(_decode(row["budget"]))
            reserved = Usage.model_validate_json(_json(_decode(row["reserved_usage"])), strict=True)
            if used.tool_calls + reserved.tool_calls + len(rejections) > budget.max_tool_calls:
                raise CloudConflict("logical rejection exceeds its tool-call budget")
            count = await connection.fetchval(
                "SELECT (SELECT COUNT(*) FROM fh_cloud.invocations "
                "WHERE org_id=$1 AND run_id=$2) + "
                "(SELECT COUNT(*) FROM fh_cloud.execution_logical_usage "
                "WHERE org_id=$1 AND run_id=$2)",
                context.org_id,
                ticket.run_id,
            )
            if count + len(rejections) > self.runs.record(run).definition.max_calls:
                raise CloudConflict("logical rejection exceeds its execution call budget")
            for rejection in rejections:
                if await connection.fetchval(
                    "SELECT EXISTS(SELECT 1 FROM fh_cloud.execution_calls "
                    "WHERE org_id=$1 AND run_id=$2 AND kind='tool' AND logical_id=$3) "
                    "OR EXISTS(SELECT 1 FROM fh_cloud.execution_logical_usage "
                    "WHERE org_id=$1 AND run_id=$2 AND logical_id=$3)",
                    context.org_id,
                    ticket.run_id,
                    rejection.call.id,
                ):
                    raise CloudConflict("logical rejection identity was already used")
            used = used.model_copy(update={"tool_calls": used.tool_calls + len(rejections)})
        if result.usage != used:
            raise CloudConflict(
                "checkpoint usage differs from settled physical calls and logical rejections"
            )
        for item in batch:
            envelope = json.loads(item)
            await connection.execute(
                "INSERT INTO fh_cloud.execution_evidence"
                "(org_id,run_id,sequence,event_hash,envelope) "
                "VALUES($1,$2,$3,$4,$5::jsonb)",
                context.org_id,
                ticket.run_id,
                envelope["event"]["sequence"],
                envelope["hash"],
                item,
            )
        for rejection in rejections:
            await connection.execute(
                "INSERT INTO fh_cloud.execution_logical_usage "
                "(org_id,run_id,logical_id,cause,call,observation,event_hash,checkpoint_revision) "
                "VALUES($1,$2,$3,$4,$5::jsonb,$6::jsonb,$7,$8)",
                context.org_id,
                ticket.run_id,
                rejection.call.id,
                rejection.cause,
                rejection.call.model_dump_json(),
                rejection.observation.model_dump_json(),
                rejection.event_hash,
                result.checkpoint_revision + 1,
            )
        run = await self.runs.lock_lease(connection, context, ticket)
        status = result.status
        if status == RunStatus.CREATED:
            raise CloudConflict("created checkpoint can only be written during acceptance")
        if status in _TERMINAL or status == RunStatus.AWAITING_APPROVAL:
            unresolved = await connection.fetchval(
                "SELECT EXISTS(SELECT 1 FROM fh_cloud.invocations "
                "WHERE org_id=$1 AND run_id=$2 AND state<>'settled')",
                context.org_id,
                ticket.run_id,
            )
            if unresolved or run["reserved_units"] or any(_decode(row["reserved_usage"]).values()):
                raise CloudConflict("unresolved calls prevent execution suspension/completion")
            if run["cancel_requested"]:
                result = result.model_copy(
                    update={
                        "status": RunStatus.CANCELLED,
                        "final_output": None,
                        "error": "cancelled",
                        "pending_approval": None,
                        "authorized_call_id": None,
                    }
                )
                status = result.status
        if status == RunStatus.SUCCEEDED:
            budget = RunBudget.model_validate(_decode(row["budget"]))
            limits = {
                "steps": budget.max_steps,
                "tool_calls": budget.max_tool_calls,
                "input_tokens": budget.max_input_tokens,
                "output_tokens": budget.max_output_tokens,
            }
            definition = self.runs.record(run).definition
            if run["used_units"] > definition.max_units or any(
                getattr(used, key) > value for key, value in limits.items()
            ):
                raise CloudBudgetExceeded("actual execution usage exceeded its budget")
        settled = await connection.fetch(
            "SELECT i.receipt_id AS invocation_receipt_id,c.kind,c.actual_usage,c.canonical_output,"
            "r.receipt_id,r.actual_units,r.result_digest,r.outcome FROM fh_cloud.invocations i "
            "LEFT JOIN fh_cloud.execution_calls c USING(org_id,run_id,invocation_id) "
            "LEFT JOIN fh_cloud.receipts r USING(org_id,run_id,invocation_id) "
            "WHERE i.org_id=$1 AND i.run_id=$2 AND i.state='settled'",
            context.org_id,
            ticket.run_id,
        )
        actual = Usage()
        actual_units = 0
        for fact in settled:
            if (
                fact["actual_usage"] is None
                or fact["receipt_id"] is None
                or (fact["outcome"] == "completed" and fact["canonical_output"] is None)
            ):
                raise CloudConflict("settled execution call is missing its full usage evidence")
            delta = Usage.model_validate_json(_json(_decode(fact["actual_usage"])), strict=True)
            try:
                receipt = Receipt.model_validate({key: fact[key] for key in Receipt.model_fields})
                physical = (
                    PhysicalResult.model_validate_json(
                        _json(_decode(fact["canonical_output"])), strict=True
                    )
                    if fact["canonical_output"] is not None
                    else None
                )
            except ValueError:
                raise CloudConflict(
                    "settled execution facts contain invalid result evidence"
                ) from None
            if (
                receipt.receipt_id != fact["invocation_receipt_id"]
                or (
                    receipt.outcome == "not_executed"
                    and (physical is not None or delta != Usage() or receipt.actual_units)
                )
                or (
                    receipt.outcome == "completed"
                    and (
                        physical is None
                        or physical.kind != fact["kind"]
                        or physical.receipt_id != receipt.receipt_id
                        or physical.actual_usage != delta
                        or physical.actual_units != receipt.actual_units
                        or canon_result_digest(physical) != receipt.result_digest
                    )
                )
            ):
                raise CloudConflict("settled execution facts differ from their result or receipt")
            actual_units += receipt.actual_units
            actual = Usage(
                **{key: getattr(actual, key) + getattr(delta, key) for key in Usage.model_fields}
            )
        logical = await connection.fetch(
            "SELECT l.logical_id,l.cause,l.call,l.observation,l.usage,e.envelope "
            "FROM fh_cloud.execution_logical_usage l JOIN fh_cloud.execution_evidence e "
            "USING(org_id,run_id,event_hash) WHERE l.org_id=$1 AND l.run_id=$2",
            context.org_id,
            ticket.run_id,
        )
        for fact in logical:
            call = ToolCall.model_validate(_decode(fact["call"]))
            observation = Message.model_validate(_decode(fact["observation"]))
            if (
                call.id != fact["logical_id"]
                or _decode(fact["usage"]) != _LOGICAL_USAGE.model_dump(mode="json")
                or _rejection_matches(call, observation, _decode(fact["envelope"])["event"])
                != fact["cause"]
            ):
                raise CloudConflict("logical usage fact differs from its denial evidence")
            actual = actual.model_copy(update={"tool_calls": actual.tool_calls + 1})
        if used != actual or run["used_units"] != actual_units:
            raise CloudConflict("execution usage projection differs from settled facts")
        if status == RunStatus.SUCCEEDED:
            confirmed = await connection.fetchval(
                "SELECT EXISTS(SELECT 1 FROM fh_cloud.invocations "
                "WHERE org_id=$1 AND run_id=$2) "
                "AND NOT EXISTS(SELECT 1 FROM fh_cloud.invocations i "
                "LEFT JOIN fh_cloud.execution_calls c USING(org_id,run_id,invocation_id) "
                "LEFT JOIN fh_cloud.receipts r "
                "USING(org_id,run_id,invocation_id) WHERE i.org_id=$1 AND i.run_id=$2 "
                "AND (c.canonical_output IS NULL OR c.actual_usage IS NULL "
                "OR r.outcome IS DISTINCT FROM 'completed'))",
                context.org_id,
                ticket.run_id,
            )
            if not confirmed:
                raise CloudConflict("success requires confirmed execution results")
        saved = result.model_copy(update={"checkpoint_revision": result.checkpoint_revision + 1})
        pending = combined.snapshot_pending()
        head = json.loads(pending[-1])["hash"] if pending else ""
        saved_json = saved.model_dump_json()
        used_json = used.model_dump_json()
        # Complete-result validation and hashing can consume the remaining lease.
        # An expired worker must not commit even while its row lock delays a new
        # claimant; wall-clock validity is part of the fence, not only the epoch.
        await self.runs.lock_lease(connection, context, ticket)
        await connection.execute(
            "UPDATE fh_cloud.execution_checkpoints SET revision=$3,checkpoint=$4::jsonb,"
            "trace_sequence=$5,trace_hash=$6,used_usage=$7::jsonb "
            "WHERE org_id=$1 AND run_id=$2",
            context.org_id,
            ticket.run_id,
            saved.checkpoint_revision,
            saved_json,
            len(pending),
            head,
            used_json,
        )
        public_status = status.value
        if status == RunStatus.RUNNING and run["status"] == "reconciling":
            public_status = "reconciling"
        await connection.execute(
            "UPDATE fh_cloud.runs SET status=$3,"
            "lease_until=CASE WHEN $4 THEN NULL ELSE lease_until END "
            "WHERE org_id=$1 AND run_id=$2",
            context.org_id,
            ticket.run_id,
            public_status,
            status in _TERMINAL or status == RunStatus.AWAITING_APPROVAL,
        )
        await self.runs.emit(
            connection,
            context.org_id,
            ticket.run_id,
            "execution.checkpoint",
            {
                "revision": saved.checkpoint_revision,
                "status": public_status,
                "trace_sequence": len(pending),
                "trace_hash": head,
            },
        )
        return saved
