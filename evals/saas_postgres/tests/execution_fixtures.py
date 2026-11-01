"""Independent synthetic business oracle for actual hosted execution tests.

Only this simulated external business system uses SQLite. Cloud checkpoints,
approvals, reservations, leases and execution evidence must remain in PostgreSQL.
Its receipts and effect counts are independent of all Cloud tables.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from forgeharness.domain.models import (
    BusinessReceipt,
    FinalAction,
    MessageRole,
    ModelResult,
    ModelUsage,
    RunContext,
    ToolAction,
    ToolCall,
    Usage,
)
from forgeharness.models.base import ModelRequest
from forgeharness.runtime.budget import RunBudget
from forgeharness.runtime.verification import (
    VerificationRequest,
    VerificationResult,
)
from forgeharness.sdk.policy import AuthorizationRequest
from forgeharness.sdk.spec import ApplicationSpec
from forgeharness.tools.base import (
    RiskLevel,
    ToolContext,
    ToolEffectClass,
    ToolOutput,
    ToolSpec,
)
from forgeharness.tools.policy import PolicyDecision, PolicyDecisionType


def canonical(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


@dataclass(frozen=True)
class OracleReceipt:
    key: str
    receipt_id: str
    kind: str
    request: dict[str, Any]
    result: dict[str, Any]
    actual_units: int
    actual_usage: Usage
    dispatches: int
    physical_key: str | None
    physical_input: dict[str, Any] | None
    physical_fact: dict[str, Any] | None


class SyntheticBusinessLedger:
    """Commit a business effect and its authoritative receipt in one transaction."""

    def __init__(self, path: Path) -> None:
        self.path = path
        with self._transaction() as connection:
            connection.executescript(
                "CREATE TABLE IF NOT EXISTS records (domain TEXT NOT NULL, tenant TEXT NOT NULL,"
                " record_key TEXT NOT NULL, value TEXT NOT NULL, revision INTEGER NOT NULL,"
                " PRIMARY KEY(domain,tenant,record_key));"
                "CREATE TABLE IF NOT EXISTS receipts (key TEXT PRIMARY KEY,"
                " receipt_id TEXT UNIQUE NOT NULL, kind TEXT NOT NULL, request TEXT NOT NULL,"
                " result TEXT NOT NULL, actual_units INTEGER NOT NULL, actual_usage TEXT NOT NULL,"
                " dispatches INTEGER NOT NULL DEFAULT 1, physical_key TEXT UNIQUE,"
                " physical_input TEXT, physical_fact TEXT);"
                "CREATE TABLE IF NOT EXISTS effects (key TEXT PRIMARY KEY, domain TEXT NOT NULL,"
                " tenant TEXT NOT NULL, record_key TEXT NOT NULL, value TEXT NOT NULL);"
            )

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def seed(
        self, domain: str, tenant: str = "org-a", record_key: str = "record-1", value: str = "draft"
    ) -> None:
        with self._transaction() as connection:
            connection.execute(
                "INSERT INTO records VALUES(?,?,?,?,1)", (domain, tenant, record_key, value)
            )

    @staticmethod
    def tool_request(
        domain: str, name: str, arguments: dict[str, Any], context: ToolContext
    ) -> dict[str, Any]:
        if (
            not context.idempotency_key
            or not context.logical_call_id
            or context.run_context is None
        ):
            raise ValueError("fixture tools require the governed physical call context")
        return {
            "domain": domain,
            "tenant": context.run_context.scope.tenant_id,
            "task_id": context.task_id,
            "logical_call_id": context.logical_call_id,
            "tool_name": name,
            "arguments": arguments,
        }

    @staticmethod
    def _receipt(row: sqlite3.Row) -> OracleReceipt:
        return OracleReceipt(
            key=row["key"],
            receipt_id=row["receipt_id"],
            kind=row["kind"],
            request=json.loads(row["request"]),
            result=json.loads(row["result"]),
            actual_units=row["actual_units"],
            actual_usage=Usage.model_validate_json(row["actual_usage"]),
            dispatches=row["dispatches"],
            physical_key=row["physical_key"],
            physical_input=json.loads(row["physical_input"]) if row["physical_input"] else None,
            physical_fact=json.loads(row["physical_fact"]) if row["physical_fact"] else None,
        )

    def receipt(self, key: str) -> OracleReceipt | None:
        with self._transaction() as connection:
            row = connection.execute("SELECT * FROM receipts WHERE key=?", (key,)).fetchone()
            return self._receipt(row) if row is not None else None

    def receipt_by_id(self, receipt_id: str) -> OracleReceipt | None:
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM receipts WHERE receipt_id=?", (receipt_id,)
            ).fetchone()
            return self._receipt(row) if row is not None else None

    def all_receipts(self, kind: str | None = None) -> tuple[OracleReceipt, ...]:
        with self._transaction() as connection:
            rows = connection.execute("SELECT * FROM receipts ORDER BY rowid").fetchall()
            return tuple(self._receipt(row) for row in rows if kind is None or row["kind"] == kind)

    def effect_count(self, domain: str, tenant: str = "org-a", record_key: str = "record-1") -> int:
        with self._transaction() as connection:
            return int(
                connection.execute(
                    "SELECT count(*) FROM effects WHERE domain=? AND tenant=? AND record_key=?",
                    (domain, tenant, record_key),
                ).fetchone()[0]
            )

    def record(
        self, domain: str, tenant: str = "org-a", record_key: str = "record-1"
    ) -> dict[str, Any]:
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM records WHERE domain=? AND tenant=? AND record_key=?",
                (domain, tenant, record_key),
            ).fetchone()
            if row is None:
                raise ValueError("synthetic record missing")
            return dict(row)

    @staticmethod
    def _existing(
        connection: sqlite3.Connection, key: str, kind: str, request: dict[str, Any]
    ) -> OracleReceipt | None:
        row = connection.execute("SELECT * FROM receipts WHERE key=?", (key,)).fetchone()
        if row is None:
            return None
        if row["kind"] != kind or row["request"] != canonical(request):
            raise ValueError("business idempotency key reused with changed arguments")
        connection.execute("UPDATE receipts SET dispatches=dispatches+1 WHERE key=?", (key,))
        return SyntheticBusinessLedger._receipt(row)

    @staticmethod
    def _insert(
        connection: sqlite3.Connection,
        key: str,
        receipt_id: str,
        kind: str,
        request: dict[str, Any],
        result: dict[str, Any],
        units: int,
        usage: Usage,
    ) -> None:
        connection.execute(
            "INSERT INTO receipts "
            "(key,receipt_id,kind,request,result,actual_units,actual_usage,dispatches) "
            "VALUES(?,?,?,?,?,?,?,1)",
            (
                key,
                receipt_id,
                kind,
                canonical(request),
                canonical(result),
                units,
                usage.model_dump_json(),
            ),
        )

    def tool(
        self,
        domain: str,
        name: str,
        arguments: dict[str, Any],
        context: ToolContext,
        *,
        write: bool,
    ) -> ToolOutput:
        request = self.tool_request(domain, name, arguments, context)
        key = context.idempotency_key
        assert key is not None
        with self._transaction() as connection:
            existing = self._existing(connection, key, "tool", request)
            if existing is not None:
                return ToolOutput.model_validate(existing.result)
            tenant = request["tenant"]
            record_key = arguments["record_key"]
            row = connection.execute(
                "SELECT value,revision FROM records WHERE domain=? AND tenant=? AND record_key=?",
                (domain, tenant, record_key),
            ).fetchone()
            if row is None:
                raise ValueError("synthetic record missing")
            value, revision = row["value"], row["revision"]
            if write:
                value, revision = arguments["value"], revision + 1
                connection.execute(
                    "UPDATE records SET value=?,revision=? "
                    "WHERE domain=? AND tenant=? AND record_key=?",
                    (value, revision, domain, tenant, record_key),
                )
                connection.execute(
                    "INSERT INTO effects VALUES(?,?,?,?,?)",
                    (key, domain, tenant, record_key, value),
                )
            receipt_id = "business-" + hashlib.sha256(key.encode()).hexdigest()[:32]
            content = {
                "domain": domain,
                "tenant": tenant,
                "record_key": record_key,
                "value": value,
                "revision": revision,
                "receipt_id": receipt_id,
            }
            output = ToolOutput(
                ok=True,
                content=canonical(content),
                usage=Usage(),
                receipt=BusinessReceipt(
                    resource_id=receipt_id,
                    status="completed",
                    version="1",
                    evidence_refs=("synthetic-ledger://" + receipt_id,),
                ),
            )
            self._insert(
                connection,
                key,
                receipt_id,
                "tool",
                request,
                output.model_dump(mode="json"),
                3 if write else 2,
                Usage(tool_calls=1),
            )
            return output

    def model(self, request: ModelRequest, result: ModelResult) -> ModelResult:
        data = request.model_dump(mode="json")
        key = "model-" + digest(data)
        with self._transaction() as connection:
            existing = self._existing(connection, key, "model", data)
            if existing is not None:
                if existing.result != result.model_dump(mode="json"):
                    raise ValueError("fixture model changed a response to an identical request")
                return ModelResult.model_validate(existing.result)
            self._insert(
                connection,
                key,
                key,
                "model",
                data,
                result.model_dump(mode="json"),
                result.usage.input_tokens + result.usage.output_tokens,
                Usage(
                    steps=1,
                    input_tokens=result.usage.input_tokens,
                    output_tokens=result.usage.output_tokens,
                ),
            )
            return result

    def verify_tool(
        self, domain: str, call: ToolCall, context: ToolContext, result: ToolOutput
    ) -> OracleReceipt:
        receipt = self.receipt(context.idempotency_key or "")
        expected = self.tool_request(domain, call.name, call.arguments, context)
        if (
            receipt is None
            or receipt.kind != "tool"
            or receipt.request != expected
            or receipt.result != result.model_dump(mode="json")
        ):
            raise ValueError("business receipt could not be independently verified")
        return receipt

    def freeze_physical(
        self, key: str, physical_key: str, physical_input: dict[str, Any], fact: dict[str, Any]
    ) -> dict[str, Any]:
        """Freeze the first independently verified complete response for its resolver."""
        with self._transaction() as connection:
            row = connection.execute("SELECT * FROM receipts WHERE key=?", (key,)).fetchone()
            if row is None:
                raise ValueError("cannot freeze an absent business receipt")
            if row["physical_fact"] is not None:
                if (
                    row["physical_key"] != physical_key
                    or row["physical_input"] != canonical(physical_input)
                    or row["physical_fact"] != canonical(fact)
                ):
                    raise ValueError("conflicting complete physical response")
                return json.loads(row["physical_fact"])
            connection.execute(
                "UPDATE receipts SET physical_key=?,physical_input=?,physical_fact=? WHERE key=?",
                (physical_key, canonical(physical_input), canonical(fact), key),
            )
            return fact

    def verify_model(self, request: ModelRequest, result: ModelResult) -> OracleReceipt:
        receipt = self.receipt("model-" + digest(request.model_dump(mode="json")))
        if (
            receipt is None
            or receipt.kind != "model"
            or receipt.result != result.model_dump(mode="json")
        ):
            raise ValueError("model receipt could not be independently verified")
        return receipt


class SyntheticResponseLost(RuntimeError):
    """An actual business commit happened but its response was not delivered."""


@dataclass
class FaultBarrier:
    point: str | None = None
    raise_after: bool = False
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)

    def arm(self, point: str, *, raise_after: bool = False) -> None:
        self.point = point
        self.raise_after = raise_after
        self.entered.clear()
        self.release.clear()

    async def checkpoint(self, point: str) -> None:
        if self.point != point:
            return
        self.point = None
        self.entered.set()
        if self.raise_after:
            raise SyntheticResponseLost("synthetic response lost after business commit")
        await self.release.wait()


class ReadInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    record_key: str = Field(min_length=1, max_length=64)


class WriteInput(ReadInput):
    value: str = Field(min_length=1, max_length=128)


class LedgerTool:
    def __init__(
        self,
        ledger: SyntheticBusinessLedger,
        domain: str,
        *,
        write: bool,
        faults: FaultBarrier | None = None,
    ) -> None:
        self.ledger, self.domain, self.write = ledger, domain, write
        self.faults = faults or FaultBarrier()
        self.input_model = WriteInput if write else ReadInput
        self.spec = ToolSpec(
            name="oracle.write" if write else "oracle.read",
            description="Update the synthetic business record."
            if write
            else "Read the synthetic business record.",
            input_schema=self.input_model.model_json_schema(),
            risk=RiskLevel.WRITE if write else RiskLevel.READ,
            effect_class=ToolEffectClass.IDEMPOTENT if write else ToolEffectClass.READ_ONLY,
        )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolOutput:
        data = self.input_model.model_validate(arguments).model_dump(mode="json")
        phase = "write" if self.write else "read"
        await self.faults.checkpoint(phase + ".before_effect")
        result = await asyncio.to_thread(
            self.ledger.tool, self.domain, self.spec.name, data, context, write=self.write
        )
        await self.faults.checkpoint(phase + ".after_commit")
        return result


class IndependentApprovalPolicy:
    revoked = False

    def evaluate(self, request: AuthorizationRequest) -> PolicyDecision:
        if self.revoked:
            return PolicyDecision(type=PolicyDecisionType.DENY, reason="synthetic owner revoked")
        return PolicyDecision(
            type=PolicyDecisionType.REQUIRE_APPROVAL
            if request.spec.risk == RiskLevel.WRITE
            else PolicyDecisionType.ALLOW,
            reason="independent approval is required for business writes",
        )

    def can_approve(self, *, requester: RunContext, owner: RunContext, call: ToolCall) -> bool:
        return (
            not self.revoked
            and call.name == "oracle.write"
            and requester.actor.id != owner.actor.id
            and "approver" in requester.actor.roles
            and requester.scope == owner.scope
        )


class TranscriptModel:
    """A restartable deterministic model whose decisions use the actual transcript."""

    def __init__(self, ledger: SyntheticBusinessLedger, *, invalid_final: bool = False) -> None:
        self.ledger = ledger
        self.invalid_final = invalid_final

    async def decide(self, request: ModelRequest) -> ModelResult:
        observations = {
            message.tool_name: message.content
            for message in request.messages
            if message.role == MessageRole.TOOL
        }
        if "oracle.write" in observations:
            action = FinalAction(
                content="invalid final"
                if self.invalid_final
                else observations["oracle.write"] or ""
            )
        elif "oracle.read" in observations:
            observed = json.loads(observations["oracle.read"] or "{}")
            action = ToolAction(
                call=ToolCall(
                    id="write-record",
                    name="oracle.write",
                    arguments={"record_key": observed["record_key"], "value": "approved"},
                )
            )
        else:
            action = ToolAction(
                call=ToolCall(
                    id="read-record", name="oracle.read", arguments={"record_key": "record-1"}
                )
            )
        result = ModelResult(
            action=action,
            usage=ModelUsage(input_tokens=3, output_tokens=2),
            model_name="synthetic-transcript-v1",
        )
        return await asyncio.to_thread(self.ledger.model, request, result)


class LedgerFinalVerifier:
    def __init__(self, ledger: SyntheticBusinessLedger, domain: str) -> None:
        self.ledger, self.domain = ledger, domain
        self.calls = 0

    async def verify(self, request: VerificationRequest) -> VerificationResult:
        self.calls += 1
        try:
            content = json.loads(request.final.content)
            receipt = await asyncio.to_thread(self.ledger.receipt_by_id, content["receipt_id"])
            current = await asyncio.to_thread(
                self.ledger.record, self.domain, content["tenant"], content["record_key"]
            )
            effects = await asyncio.to_thread(
                self.ledger.effect_count, self.domain, content["tenant"], content["record_key"]
            )
            passed = (
                receipt is not None
                and receipt.request["tool_name"] == "oracle.write"
                and receipt.request["task_id"] == request.task_id
                and receipt.result["content"] == request.final.content
                and content["domain"] == self.domain
                and request.run_context is not None
                and content["tenant"] == request.run_context.scope.tenant_id
                and content["value"] == "approved"
                and current["value"] == "approved"
                and effects == 1
                and {item.name for item in request.tool_results} == {"oracle.read", "oracle.write"}
            )
        except (KeyError, TypeError, ValueError):
            passed = False
        return VerificationResult(
            passed=passed,
            reason="independent business ledger checked",
            evidence=("synthetic-ledger://verified",) if passed else (),
        )


@dataclass
class ApplicationFixture:
    ledger: SyntheticBusinessLedger
    domain: str
    application: ApplicationSpec
    faults: FaultBarrier
    policy: IndependentApprovalPolicy
    verifier: LedgerFinalVerifier


def application_fixture(
    ledger: SyntheticBusinessLedger, domain: str = "orders", *, invalid_final: bool = False
) -> ApplicationFixture:
    faults = FaultBarrier()
    policy = IndependentApprovalPolicy()
    verifier = LedgerFinalVerifier(ledger, domain)
    application = ApplicationSpec(
        id="synthetic-" + domain,
        version="1",
        policy_version="1",
        model_factory=lambda: TranscriptModel(ledger, invalid_final=invalid_final),
        tools=(
            LedgerTool(ledger, domain, write=False, faults=faults),
            LedgerTool(ledger, domain, write=True, faults=faults),
        ),
        policy=policy,
        verifier=verifier,
        budget=RunBudget(
            max_steps=10,
            max_tool_calls=4,
            max_input_tokens=10_000,
            max_output_tokens=1_000,
            reserve_output_tokens=10,
            max_attempts_per_call=1,
        ),
    )
    return ApplicationFixture(ledger, domain, application, faults, policy, verifier)


def hosted_registration(fixture: ApplicationFixture):
    """Register trusted verification/resolution backed only by the external oracle."""
    from forgeharness.cloud.execution_contract import PhysicalResult
    from forgeharness.cloud.hosted import HostedApplication
    from forgeharness.models.prepared import PreparedModelCall
    from forgeharness.tools.dispatcher import DispatchResult

    ledger = fixture.ledger

    def binding(physical):
        return {
            "owner": physical.owner.model_dump(mode="json"),
            "envelope": physical.envelope.model_dump(mode="json"),
        }

    def prepared_request(physical):
        return PreparedModelCall.model_validate(
            physical.envelope.canonical_input["prepared"]
        ).request

    async def freeze(physical, receipt, result):
        frozen = await asyncio.to_thread(
            ledger.freeze_physical,
            receipt.key,
            physical.idempotency_key,
            binding(physical),
            result.model_dump(mode="json"),
        )
        return PhysicalResult.model_validate(frozen)

    async def verify_model(physical, outcome):
        if outcome.result is None:
            return None
        try:
            receipt = await asyncio.to_thread(
                ledger.verify_model, prepared_request(physical), outcome.result
            )
            result = PhysicalResult(
                receipt_id=receipt.receipt_id,
                actual_units=receipt.actual_units,
                kind="model",
                model_result=outcome.result,
            )
            return await freeze(physical, receipt, result)
        except (KeyError, TypeError, ValueError):
            return None

    async def verify_tool(physical, outcome):
        try:
            call = ToolCall.model_validate(physical.envelope.canonical_input["call"])
            context = ToolContext(
                task_id=physical.owner.run_id,
                run_context=physical.owner,
                logical_call_id=call.id,
                idempotency_key=physical.idempotency_key,
            )
            receipt = await asyncio.to_thread(
                ledger.verify_tool, fixture.domain, call, context, outcome.output
            )
            result = PhysicalResult(
                receipt_id=receipt.receipt_id,
                actual_units=receipt.actual_units,
                kind="tool",
                tool_result=outcome,
            )
            return await freeze(physical, receipt, result)
        except (KeyError, TypeError, ValueError):
            return None

    async def resolve(physical):
        if physical.envelope.kind == "model":
            key = "model-" + digest(prepared_request(physical).model_dump(mode="json"))
        else:
            key = physical.idempotency_key
        receipt = await asyncio.to_thread(ledger.receipt, key)
        if receipt is None:
            return None
        if receipt.physical_fact is not None:
            if (
                receipt.physical_key != physical.idempotency_key
                or receipt.physical_input != binding(physical)
            ):
                return None
            return PhysicalResult.model_validate(receipt.physical_fact)
        if receipt.kind == "model":
            result = PhysicalResult(
                receipt_id=receipt.receipt_id,
                actual_units=receipt.actual_units,
                kind="model",
                model_result=ModelResult.model_validate(receipt.result),
            )
        else:
            # No DispatchResult was delivered. This is an oracle's canonical
            # resolver response, not an assertion about the original latency.
            result = PhysicalResult(
                receipt_id=receipt.receipt_id,
                actual_units=receipt.actual_units,
                kind="tool",
                tool_result=DispatchResult(
                    output=ToolOutput.model_validate(receipt.result), elapsed_ms=0
                ),
            )
        return await freeze(physical, receipt, result)

    return HostedApplication(
        application=fixture.application,
        deployment_id="synthetic-" + fixture.domain,
        release_digest=digest({"synthetic-release": fixture.domain, "version": "1"}),
        model_binding_digest=digest({"synthetic-model": "transcript-v1"}),
        max_run_units=100_000,
        model_reservation_units=1_000,
        tool_reservation_units={"oracle.read": 10, "oracle.write": 10},
        model_verifier=verify_model,
        tool_verifier=verify_tool,
        resolver=resolve,
        resources=("record:record-1",),
        roles=("user",),
    )


async def _hosted_worker(payload: dict[str, Any]) -> dict[str, Any]:
    """Reconstruct the trusted application in an independent Python process."""
    from forgeharness.cloud.models import TenantContext
    from forgeharness.cloud.runtime import HostedRuntime
    from forgeharness.cloud.store import PostgresRunStore

    if os.environ.get("FH_TEST_PG_DISPOSABLE") != "1":
        raise RuntimeError("hosted worker requires the isolated PostgreSQL runner")
    store = await PostgresRunStore.connect(os.environ["FH_TEST_PG_HOSTED_RUNTIME_DSN"])
    try:
        ledger = SyntheticBusinessLedger(Path(payload["ledger_path"]))
        registration = hosted_registration(application_fixture(ledger, payload["domain"]))
        runtime = HostedRuntime(store, {registration.deployment_id: registration})
        view = await runtime.advance(
            TenantContext.model_validate(payload["worker"]),
            payload["run_id"],
            worker_id="independent-worker",
            ttl_seconds=2,
        )
        return {
            "run_id": view.run.run_id,
            "status": view.run.status,
            "checkpoint_status": view.checkpoint.status,
            "epoch": view.run.epoch,
            "used_units": view.run.used_units,
            "reserved_units": view.run.reserved_units,
        }
    finally:
        await store.close()


if __name__ == "__main__":
    try:
        worker_payload = json.loads(sys.stdin.readline())
        print(canonical(asyncio.run(_hosted_worker(worker_payload))))
    except Exception as error:
        # Do not echo a DSN, arbitrary exception text or tracebacks to the runner.
        print(canonical({"error_type": type(error).__name__}))
        raise SystemExit(1) from None
