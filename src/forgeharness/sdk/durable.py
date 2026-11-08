"""Explicit, rebuildable R2 graphs over the shared governed invocation core."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

from pydantic import Field, model_validator

from forgeharness import __version__
from forgeharness.context.assembly import ContextAssembler
from forgeharness.domain.models import (
    FinalAction,
    FrozenModel,
    Message,
    MessageRole,
    ModelResult,
    PendingApproval,
    RunContext,
    RunResult,
    RunStatus,
    ToolCall,
    ToolEvidence,
    Usage,
)
from forgeharness.models.base import ModelRequest
from forgeharness.runtime.budget import RunBudget
from forgeharness.runtime.model_call import ModelCallKind, ModelCallOutcome, invoke_model_once
from forgeharness.runtime.structured import parse_structured_output, validate_output_schema
from forgeharness.runtime.tool_executor import ToolExecutor
from forgeharness.runtime.verification import VerificationRequest
from forgeharness.sdk.parallel import ModelDispatcher, ParallelExecution
from forgeharness.sdk.spec import ScopeError
from forgeharness.state.approval import ApprovalError, ApprovalGrant, SQLiteApprovalLedger
from forgeharness.state.checkpoint import CheckpointConflict
from forgeharness.state.invocation_journal import (
    InvocationState,
    SQLiteInvocationJournal,
    args_digest,
    idempotency_key_for,
)
from forgeharness.state.orchestration import (
    R2_RECOVERY_VERSION,
    TERMINAL,
    NodeProgress,
    OrchestrationState,
    OrchestrationStore,
    add_usage,
)
from forgeharness.state.sqlite import connect_wal
from forgeharness.tools.base import ToolOutput
from forgeharness.tools.dispatcher import ToolDispatcher
from forgeharness.tools.policy import PolicyDecisionType

if TYPE_CHECKING:
    from forgeharness.sdk.harness import Harness


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def fits(used: Usage, limit: Usage) -> bool:
    return all(getattr(used, key) <= getattr(limit, key) for key in Usage.model_fields)


class DurableNode(FrozenModel):
    """A fixed node instance. Expand bounded loops into stable IDs before start."""

    id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,95}$")
    kind: Literal["tool", "model", "join"]
    dependencies: tuple[str, ...] = ()
    call: ToolCall | None = None
    messages: tuple[Message, ...] = ()
    max_usage: Usage = Usage()
    output_schema: dict[str, Any] | None = None

    @model_validator(mode="after")
    def coherent(self) -> DurableNode:
        if len(set(self.dependencies)) != len(self.dependencies):
            raise ValueError("duplicate dependencies")
        if self.kind == "tool":
            if self.call is None or self.messages or self.max_usage.tool_calls < 1:
                raise ValueError("tool node requires a call and physical-call reservation")
        elif self.call is not None:
            raise ValueError("only tool nodes accept a call")
        if self.kind == "model" and self.max_usage.steps < 1:
            raise ValueError("model node requires a step reservation")
        if self.kind == "join" and (self.messages or self.max_usage != Usage()):
            raise ValueError("join is deterministic and has no invocation budget")
        if self.output_schema is not None:
            if self.kind != "model":
                raise ValueError("only model nodes accept an output schema")
            validate_output_schema(self.output_schema)
        return self


class DurableGraph(FrozenModel):
    schema_version: Literal[1] = 1
    version: str = Field(min_length=1)
    model_identity: str = Field(min_length=1)
    resource_versions: dict[str, str]
    nodes: tuple[DurableNode, ...] = Field(min_length=1, max_length=1000)
    final_node: str

    @model_validator(mode="after")
    def acyclic(self) -> DurableGraph:
        ids = {node.id for node in self.nodes}
        if len(ids) != len(self.nodes) or self.final_node not in ids:
            raise ValueError("duplicate or missing final node")
        if any(set(node.dependencies) - ids for node in self.nodes):
            raise ValueError("unknown dependency")
        done: set[str] = set()
        while len(done) < len(ids):
            ready = {n.id for n in self.nodes if n.id not in done and set(n.dependencies) <= done}
            if not ready:
                raise ValueError("cyclic graph cannot be rebuilt")
            done.update(ready)
        return self


def rejected_output(outcome: ModelCallOutcome) -> tuple[dict[str, Any], str]:
    """The settled output and reason for a definitive provider refusal (ADR 050)."""
    status = getattr(outcome.violation, "status_code", None)
    return (
        {"error": "provider_rejected", "status_code": status},
        f"provider rejected the request (HTTP {status})",
    )


def _invalid_structured_output(node: DurableNode, output: dict[str, Any]) -> bool:
    if node.output_schema is None:
        return False
    try:
        action = ModelResult.model_validate(output).action
        if not isinstance(action, FinalAction):
            raise ValueError("structured node requires a final action")
        parse_structured_output(action.content, node.output_schema)
    except ValueError:
        return True
    return False


class ReconciledNode(FrozenModel):
    """Authoritative response/receipt and TOTAL usage, supplied by a trusted host."""

    output: dict[str, Any]
    usage: Usage
    evidence_refs: tuple[str, ...] = Field(min_length=1)


@dataclass(frozen=True)
class DurableOrchestrator:
    graph: DurableGraph
    validate_resources: Callable[[RunContext, dict[str, str]], bool]
    reconcile_node: (
        Callable[[RunContext, DurableNode, str], Awaitable[ReconciledNode | None]] | None
    ) = None
    execution: ParallelExecution = field(default_factory=ParallelExecution)
    dispatcher: ModelDispatcher | None = None

    def __post_init__(self) -> None:
        ParallelExecution.model_validate(self.execution.model_dump())
        if self.execution.max_in_flight > 1 and self.dispatcher is None:
            raise ValueError("parallel R2 requires a shared provider dispatcher")
        if (
            self.dispatcher is not None
            and self.dispatcher.capacity > 1
            and not self.execution.model_concurrency_safe
        ):
            raise ValueError(
                "shared concurrent provider requires a concurrency-safe model declaration"
            )

    def validate(self, context: RunContext) -> None:
        # Revalidate mutable nested dicts and models supplied by trusted callers.
        self.__post_init__()
        DurableGraph.model_validate(self.graph.model_dump())
        if self.validate_resources(context, dict(self.graph.resource_versions)) is not True:
            raise ScopeError("R2 resources changed or access was revoked")

    def identity(self, harness: Harness, context: RunContext, messages: tuple[Message, ...]) -> str:
        self.validate(context)
        app = harness.application
        return digest(
            {
                "context": harness._internal(context).model_dump(mode="json"),
                "graph": self.graph.model_dump(mode="json"),
                "messages": [m.model_dump(mode="json") for m in messages],
                "instructions": app.instructions,
                "tools": [tool.spec.model_dump(mode="json") for tool in app.tools],
                "budget": app.budget.model_dump(mode="json"),
                "framework": __version__,
                "execution": self.execution.model_dump(mode="json"),
                "dispatcher": None if self.dispatcher is None else self.dispatcher.identity,
            }
        )


class _StopAdmission(RuntimeError):
    pass


class _Unknown(RuntimeError):
    pass


class DurableRunner:
    """Internal scheduler. Harness owns the OS lock for its entire lifetime."""

    def __init__(self, harness: Harness, context: RunContext) -> None:
        self.harness, self.context = harness, context
        orchestrator = harness.orchestrator
        if not isinstance(orchestrator, DurableOrchestrator):
            raise ScopeError("R2 runner requires a durable definition")
        self.orchestrator = orchestrator
        self.store = OrchestrationStore(harness.database)
        self.journal = SQLiteInvocationJournal(harness.database)
        self.internal, self.model, self.registry, self.policy = harness._components(context)
        self.trace = harness._trace(context)
        self.state: OrchestrationState
        self.current_node: DurableNode | None = None

    @staticmethod
    def load(harness: Harness, context: RunContext) -> OrchestrationState:
        orchestrator = harness.orchestrator
        if not isinstance(orchestrator, DurableOrchestrator):
            raise ScopeError("R2 state requires a registered durable orchestrator")
        state = OrchestrationStore(harness.database).load(harness._key(context))
        if state is None or state.result.run_context != harness._internal(context):
            raise ScopeError("R2 state unavailable for this execution binding")
        if state.identity != orchestrator.identity(harness, context, state.result.messages):
            raise ScopeError("R2 definition, resource, model, budget or framework identity changed")
        if state.result.recovery_semantics_version != R2_RECOVERY_VERSION:
            raise ScopeError("unknown R2 recovery version")
        return state

    async def start(self, *, task: str, instructions: tuple[Message, ...]) -> RunResult:
        messages = (*instructions, Message(role=MessageRole.USER, content=task))
        identity = self.orchestrator.identity(self.harness, self.context, messages)
        # Credential-bearing definitions must be rejected, not silently rebound.
        raw = self.orchestrator.graph.model_dump(mode="json")
        if self.harness.application.evidence_transform.transform(raw) != raw:
            raise ScopeError("R2 definitions must not contain credentials")
        now = datetime.now(UTC)
        seconds = self.harness.application.budget.max_wall_seconds
        self.state = OrchestrationState(
            identity=identity,
            owner=uuid4().hex,
            started_at=now,
            observed_at=now,
            deadline=None if seconds is None else now + timedelta(seconds=seconds),
            nodes=tuple(NodeProgress(id=n.id) for n in self.orchestrator.graph.nodes),
            result=RunResult(
                task_id=self.internal.run_id,
                status=RunStatus.RUNNING,
                messages=messages,
                usage=Usage(),
                run_context=self.internal,
                recovery_semantics_version=R2_RECOVERY_VERSION,
                state_version=2,
            ),
        )
        self._save("created")
        return await self.advance()

    async def resume(
        self, *, grant: ApprovalGrant | None, approver: RunContext | None
    ) -> RunResult:
        self.state = self.load(self.harness, self.context)
        self.publish()
        if self.state.result.status in TERMINAL:
            if grant is not None:
                raise ApprovalError("terminal runs cannot consume approval")
            return self.state.result
        self.state = self.store.take_ownership(self.state, uuid4().hex)
        if grant is not None:
            pending = [
                n
                for n in self.state.nodes
                if n.status == "awaiting_approval"
                and n.output is not None
                and n.output.get("id") == self._grant_call_id(grant)
            ]
            if len(pending) != 1 or approver is None:
                raise ApprovalError("grant does not select one pending node")
            node = self._definition(pending[0].id)
            call = ToolCall.model_validate(pending[0].output)
            self._validate_approver(grant, approver, call)
            try:
                self._admit(node, call=call, grant=grant)
            except _StopAdmission as exc:
                return await self._finish(str(exc))
        return await self.advance()

    def _grant_call_id(self, grant: ApprovalGrant) -> str | None:
        from forgeharness.state.approval import call_fingerprint

        for n in self.state.nodes:
            if n.status == "awaiting_approval" and n.output is not None:
                call = ToolCall.model_validate(n.output)
                if call_fingerprint(self.internal.run_id, call) == grant.call_fingerprint:
                    return call.id
        return None

    def _definition(self, node_id: str) -> DurableNode:
        return next(n for n in self.orchestrator.graph.nodes if n.id == node_id)

    def _progress(self, node_id: str) -> NodeProgress:
        return next(n for n in self.state.nodes if n.id == node_id)

    def _model_invocation_id(self, node_id: str) -> str:
        """Stable across resume, distinct across stores and complete run bindings."""
        existing = self._progress(node_id).invocation_id
        return existing or digest(
            {
                "protocol": "r2-model-invocation-v2",
                "store": str(self.harness.database.resolve()),
                "binding": self.state.identity,
                "node": node_id,
            }
        )

    def _replace(self, progress: NodeProgress) -> None:
        self.state = self.state.model_copy(
            update={
                "nodes": tuple(progress if n.id == progress.id else n for n in self.state.nodes)
            }
        )

    def _guard(self, *, admission: bool = True) -> None:
        self.harness._validate(self.context)
        if self.state.identity != self.orchestrator.identity(
            self.harness, self.context, self.state.result.messages
        ):
            raise ScopeError("R2 identity changed during execution")
        self._guard_accounting()
        if admission and self.state.cancel_requested:
            raise _StopAdmission("cancelled")
        if (
            admission
            and self.state.deadline is not None
            and datetime.now(UTC) >= self.state.deadline
        ):
            raise _StopAdmission("deadline exceeded")

    def _guard_accounting(self) -> None:
        """Fence incurred-cost commits even after authorization was revoked.

        This path cannot admit calls or return results to a caller. The public
        read/finish paths still revalidate the host binding and resources.
        """
        current = self.store.load(self.internal.run_id)
        if current is None or (current.owner, current.revision, current.identity) != (
            self.state.owner,
            self.state.revision,
            self.state.identity,
        ):
            raise CheckpointConflict("R2 owner, revision or identity changed")
        now = datetime.now(UTC)
        if now < self.state.observed_at:
            raise ScopeError("UTC clock moved backwards; R2 admission refused")
        self.state = self.state.model_copy(
            update={"observed_at": now, "cancel_requested": current.cancel_requested}
        )

    def _limit(self) -> Usage:
        budget = self.harness.application.budget
        return Usage(
            steps=budget.max_steps,
            tool_calls=budget.max_tool_calls,
            input_tokens=budget.max_input_tokens,
            output_tokens=budget.max_output_tokens,
        )

    def _save(
        self,
        event: str,
        *,
        grant: ApprovalGrant | None = None,
        connection: Any = None,
    ) -> None:
        now = datetime.now(UTC)
        if now < self.state.observed_at:
            raise ScopeError("UTC clock moved backwards; R2 commit refused")
        self.state = self.state.model_copy(
            update={"result": self._projection(), "observed_at": now}
        )
        self.state = self.store.save(
            self.state,
            event=event,
            grant=grant,
            ledger=self._ledger() if grant is not None else None,
            connection=connection,
        )
        if connection is None:
            self.publish()

    def _ledger(self) -> SQLiteApprovalLedger:
        # Bind grants to the complete R2 identity, including resources and graph.
        return SQLiteApprovalLedger(self.harness.database, binding=self.state.identity)

    def _projection(self) -> RunResult:
        pending = next((n for n in self.state.nodes if n.status == "awaiting_approval"), None)
        evidence = []
        for node in self.orchestrator.graph.nodes:
            p = self._progress(node.id)
            if node.kind == "tool" and p.status in {"completed", "failed"} and p.output is not None:
                assert node.call is not None
                output = ToolOutput.model_validate(p.output)
                evidence.append(
                    ToolEvidence(
                        name=node.call.name,
                        ok=output.ok,
                        content=output.content,
                        metadata=output.metadata,
                        logical_call_id=node.id,
                        receipt=output.receipt,
                    )
                )
        return self.state.result.model_copy(
            update={
                "usage": add_usage(*(n.used for n in self.state.nodes)),
                "pending_approval": None
                if pending is None
                else PendingApproval(
                    call=ToolCall.model_validate(pending.output), reason="R2 node requires approval"
                ),
                "tool_evidence": tuple(evidence),
            }
        )

    def publish(self) -> None:
        for revision, payload in self.store.events(self.internal.run_id):
            self.trace.append(
                "orchestration.transition",
                {
                    "event_id": f"{self.internal.run_id}:{revision}",
                    "revision": revision,
                    **json.loads(payload),
                },
            )
            self.store.acknowledge(self.internal.run_id, revision)

    def _call(self, node: DurableNode) -> ToolCall:
        if node.call is None:
            raise ScopeError("node is not a tool")
        tool = self.registry.get(node.call.name)
        if tool is None:
            raise ScopeError("R2 tool no longer registered")
        arguments = tool.input_model.model_validate(node.call.arguments).model_dump(mode="json")
        return node.call.model_copy(update={"id": node.id, "arguments": arguments})

    def _messages(self, node: DurableNode) -> tuple[Message, ...]:
        return (
            *self.state.result.messages,
            *node.messages,
            *(
                Message(
                    role=MessageRole.USER,
                    content="Untrusted dependency result: "
                    + json.dumps(
                        {"node": key, "output": self._progress(key).output}, sort_keys=True
                    ),
                )
                for key in node.dependencies
            ),
        )

    def _validate_approver(
        self,
        grant: ApprovalGrant,
        approver: RunContext,
        call: ToolCall,
        *,
        check_expiry: bool = True,
    ) -> None:
        self.harness._validate(approver)
        if (
            grant.approver_context != approver
            or grant.granted_by != approver.actor.id
            or approver.scope != self.context.scope
            or (check_expiry and datetime.now(UTC) >= grant.expires_at)
            or grant.binding_digest != self.state.identity
            or not self.harness.application.policy.can_approve(
                requester=approver, owner=self.context, call=call
            )
        ):
            raise ApprovalError("R2 approval expired, changed or no longer authorized")

    def _authorize(self, node: DurableNode, call: ToolCall) -> PolicyDecisionType:
        executor = self._executor(node)
        decision = executor.authorize(call, task_id=self.internal.run_id)
        if decision.type is PolicyDecisionType.DENY:
            raise ScopeError("R2 call denied by current policy")
        return decision.type

    def _admit(
        self, node: DurableNode, *, call: ToolCall | None = None, grant: ApprovalGrant | None = None
    ) -> None:
        self._guard()
        if call is not None:
            decision = self._authorize(node, call)
            if decision is PolicyDecisionType.REQUIRE_APPROVAL and grant is None:
                raise ApprovalError("R2 admission requires an exact approval")
        committed = add_usage(
            *(
                Usage(
                    **{
                        key: max(getattr(n.used, key), getattr(n.reservation, key))
                        for key in Usage.model_fields
                    }
                )
                if n.status in {"admitted", "unknown"}
                else n.used
                for n in self.state.nodes
            )
        )
        if not fits(add_usage(committed, node.max_usage), self._limit()):
            raise _StopAdmission("budget exhausted")
        raw = (
            call.model_dump(mode="json")
            if call is not None
            else {"messages": [m.model_dump(mode="json") for m in self._messages(node)]}
        )
        if self.harness.application.evidence_transform.transform(raw) != raw:
            raise ScopeError("R2 normalized inputs must not contain credentials")
        self._replace(
            self._progress(node.id).model_copy(
                update={
                    "status": "admitted",
                    "reservation": node.max_usage,
                    "input_digest": digest(raw),
                    "output": raw if call is not None else None,
                    "grant": grant,
                    "dispatch_phase": "reserved",
                    "capacity_granted_at": datetime.now(UTC),
                }
            )
        )
        self.state = self.state.model_copy(
            update={
                "result": self.state.result.model_copy(
                    update={"status": RunStatus.RUNNING, "error": None}
                )
            }
        )
        self._save("admitted:" + node.id, grant=grant)

    def _attempt_usage(self, node: DurableNode) -> Usage:
        attempts = self.journal.attempts_for(f"{self.internal.run_id}:{node.id}")
        if any(a.usage is None for a in attempts):
            raise _Unknown("physical attempt usage is unknown")
        return add_usage(*(a.usage for a in attempts if a.usage is not None))

    def _before_attempt(self, node: DurableNode) -> None:
        self._guard()
        call = self._call(node)
        self._authorize_current(node, call)
        used = self._attempt_usage(node)
        if not fits(add_usage(used, Usage(tool_calls=1)), node.max_usage):
            raise _StopAdmission("physical attempt budget exhausted")

    def _executor(self, node: DurableNode) -> ToolExecutor:
        return ToolExecutor(
            registry=self.registry,
            dispatcher=ToolDispatcher(self.registry),
            policy=self.policy,
            trace=self.trace,
            budget=self._node_budget(node),
            context=lambda: self.internal,
            journal=self.journal,
            before_attempt=lambda: self._before_attempt(node),
            attempt_usage=lambda: self._attempt_usage(node),
            step_limit=node.max_usage.steps,
        )

    def _node_budget(self, node: DurableNode) -> RunBudget:
        limit = node.max_usage
        return self.harness.application.budget.model_copy(
            update={
                "max_steps": max(1, limit.steps),
                "max_tool_calls": limit.tool_calls,
                "max_input_tokens": limit.input_tokens,
                "max_output_tokens": limit.output_tokens,
                "max_context_tokens": min(
                    self.harness.application.budget.max_context_tokens, max(1, limit.input_tokens)
                ),
            }
        )

    def _authorize_current(
        self, node: DurableNode, call: ToolCall, *, admission: bool = True
    ) -> None:
        decision = self._authorize(node, call)
        progress = self._progress(node.id)
        if progress.grant is not None:
            if progress.grant.approver_context is None:
                raise ApprovalError("missing original approver")
            self._validate_approver(
                progress.grant, progress.grant.approver_context, call, check_expiry=admission
            )
        elif decision is PolicyDecisionType.REQUIRE_APPROVAL:
            raise ApprovalError("current policy requires approval")

    async def _tool(self, node: DurableNode) -> None:
        call = self._call(node)
        progress = self._progress(node.id)
        if progress.input_digest != digest(call.model_dump(mode="json")):
            raise ScopeError("R2 admitted call changed")
        self._guard(admission=False)
        record = self.journal.load(run_id=self.internal.run_id, logical_call_id=node.id)
        self._authorize_current(
            node, call, admission=record is None or record.state is InvocationState.CLAIMED
        )
        executor = self._executor(node)
        if record is not None:
            tool = self.registry.get(call.name)
            if (
                tool is None
                or record.tool_name != call.name
                or record.args_digest != args_digest(call.arguments)
                or record.effect_class != tool.spec.effect_class
                or record.journal_version != 1
            ):
                raise ScopeError("R2 invocation identity changed")
            if record.state in {InvocationState.COMPLETED, InvocationState.FAILED}:
                if record.result is None:
                    raise _Unknown("journal result missing")
                self._complete(
                    node, record.result.model_dump(mode="json"), self._attempt_usage(node)
                )
                return
            if record.state is InvocationState.INDETERMINATE:
                raise _Unknown("tool effect requires reconciliation")
            if record.state is InvocationState.STARTED:
                # Even idempotent retries cannot discard unknown nested fees.
                self._attempt_usage(node)
                raise _Unknown("started invocation requires receipt reconciliation")
            self._before_attempt(node)
            started = self.journal.restart(record, checkpoint_revision=self.state.revision)
            dispatch = await executor._run_attempts(
                call=call,
                record=record,
                started=started,
                workspace=self.internal.scope.workspace,
                usage=Usage(),
                task_id=self.internal.run_id,
                effect_class=record.effect_class,
            )
        else:
            self._before_attempt(node)
            dispatch = await executor.dispatch(
                call,
                self.internal.scope.workspace,
                Usage(),
                task_id=self.internal.run_id,
                checkpoint_revision=self.state.revision,
            )
        settled = self.journal.load(run_id=self.internal.run_id, logical_call_id=node.id)
        if settled is None or settled.state is InvocationState.INDETERMINATE:
            raise _Unknown("tool effect requires reconciliation")
        self._complete(node, dispatch.output.model_dump(mode="json"), self._attempt_usage(node))

    async def _model(self, node: DurableNode) -> None:
        self._guard()
        progress = self._progress(node.id)
        raw = {"messages": [m.model_dump(mode="json") for m in self._messages(node)]}
        if progress.input_digest != digest(raw):
            raise ScopeError("R2 model request identity changed")
        # Persist uncertainty BEFORE entering a provider. No automatic resend.
        self._replace(
            progress.model_copy(
                update={
                    "status": "unknown",
                    "used": Usage(steps=1),
                    "dispatch_phase": "sending",
                    "sent_at": datetime.now(UTC),
                    "invocation_id": self._model_invocation_id(node.id),
                }
            )
        )
        self._save("model.sending:" + node.id)
        outcome = await invoke_model_once(
            model=self.model,
            request=ModelRequest(
                task_id=self.internal.run_id,
                messages=self._messages(node),
                tools=(),
            ),
            budget=self._node_budget(node),
            assembler=ContextAssembler(max_tokens=self._node_budget(node).max_context_tokens),
            trace=self.trace,
            usage=Usage(),
            phase="r2:" + node.id,
        )
        if outcome.kind in {ModelCallKind.PREPARATION_FAILED, ModelCallKind.CONTEXT_OVERFLOW}:
            self._complete(node, {"error": outcome.kind.value}, Usage(), failed=True)
        elif outcome.kind is ModelCallKind.MODEL_FAILED:
            if not outcome.rejected:
                raise _Unknown("model response or usage is unknown")
            # The provider's answer proves nothing ran and nothing was billed (ADR 050).
            output, reason = rejected_output(outcome)
            self._complete(node, output, Usage(), failed=True, reason=reason)
        elif outcome.kind is ModelCallKind.PROTOCOL_VIOLATION:
            if getattr(outcome.violation, "usage", None) is None:
                raise _Unknown("rejected model usage is unknown")
            self._complete(node, {"error": "protocol_violation"}, outcome.usage, failed=True)
        else:
            if outcome.result is None:
                raise _Unknown("model returned no durable response")
            self._complete(
                node,
                outcome.result.model_dump(mode="json"),
                outcome.usage,
                failed=not isinstance(outcome.result.action, FinalAction),
            )

    def _complete(
        self,
        node: DurableNode,
        output: dict[str, Any],
        usage: Usage,
        *,
        failed: bool = False,
        accounting: bool = False,
        reason: str | None = None,
    ) -> None:
        if accounting:
            self._guard_accounting()
        else:
            self._guard(admission=False)
        if self._progress(node.id).status in {"completed", "failed"}:
            raise CheckpointConflict("node already settled")
        if node.kind == "tool":
            failed = failed or not ToolOutput.model_validate(output).ok
        schema_error = _invalid_structured_output(node, output)
        failed = failed or schema_error
        failed = failed or not fits(usage, node.max_usage)
        self._replace(
            self._progress(node.id).model_copy(
                update={
                    "status": "failed" if failed else "completed",
                    "output": output,
                    "used": usage,
                    "reservation": Usage(),
                    "error": (
                        reason
                        if reason is not None
                        else "invalid structured model output"
                        if schema_error
                        else "node failed or exceeded declared usage"
                        if failed
                        else None
                    ),
                    "dispatch_phase": "settled",
                    "completed_at": datetime.now(UTC),
                }
            )
        )
        self._save("settled:" + node.id)

    async def advance(self) -> RunResult:
        if self.orchestrator.dispatcher is not None:
            from forgeharness.sdk.parallel_runner import advance_parallel

            return await advance_parallel(self)
        stop: str | None = None
        for node in self.orchestrator.graph.nodes:
            p = self._progress(node.id)
            if p.status in {"admitted", "unknown"}:
                if node.kind == "model" and p.status == "unknown":
                    continue
                try:
                    await self._execute(node)
                except _Unknown as exc:
                    self._mark_unknown(node, str(exc))
                except _StopAdmission as exc:
                    stop = str(exc)
                    break
        while stop is None:
            ready = [
                n
                for n in self.orchestrator.graph.nodes
                if self._progress(n.id).status == "pending"
                and all(self._progress(k).status == "completed" for k in n.dependencies)
            ]
            if not ready or any(n.status == "failed" for n in self.state.nodes):
                break
            for node in ready:
                try:
                    self._guard()
                    if node.kind == "join":
                        self._complete(
                            node, {k: self._progress(k).output for k in node.dependencies}, Usage()
                        )
                        continue
                    call = self._call(node) if node.kind == "tool" else None
                    if (
                        call is not None
                        and self._authorize(node, call) is PolicyDecisionType.REQUIRE_APPROVAL
                    ):
                        self._replace(
                            self._progress(node.id).model_copy(
                                update={
                                    "status": "awaiting_approval",
                                    "output": call.model_dump(mode="json"),
                                    "input_digest": digest(call.model_dump(mode="json")),
                                }
                            )
                        )
                        self._save("approval.pending:" + node.id)
                        continue
                    self._admit(node, call=call)
                    await self._execute(node)
                except _Unknown as exc:
                    self._mark_unknown(node, str(exc))
                except _StopAdmission as exc:
                    stop = str(exc)
                    break
        return await self._finish(stop)

    async def _execute(self, node: DurableNode) -> None:
        if node.kind == "tool":
            await self._tool(node)
        else:
            await self._model(node)

    def _mark_unknown(self, node: DurableNode, error: str, *, accounting: bool = False) -> None:
        if accounting:
            self._guard_accounting()
        used = self._progress(node.id).used
        if node.kind == "tool":
            attempts = self.journal.attempts_for(f"{self.internal.run_id}:{node.id}")
            used = add_usage(
                *(a.usage if a.usage is not None else Usage(tool_calls=1) for a in attempts)
            )
        self._replace(
            self._progress(node.id).model_copy(
                update={
                    "status": "unknown",
                    "error": error,
                    "used": used,
                    "dispatch_phase": "unknown",
                }
            )
        )
        self._save("unknown:" + node.id)

    async def _finish(self, stop: str | None) -> RunResult:
        self._guard(admission=False)
        if self.state.deadline is not None and datetime.now(UTC) >= self.state.deadline:
            stop = stop or "deadline exceeded"
        if stop is not None or self.state.cancel_requested:
            for p in self.state.nodes:
                node = self._definition(p.id)
                if p.status == "admitted" and node.kind == "tool":
                    attempts = self.journal.attempts_for(f"{self.internal.run_id}:{node.id}")
                    record = self.journal.load(run_id=self.internal.run_id, logical_call_id=node.id)
                    if any(a.usage is None for a in attempts) or (
                        record is not None
                        and record.state in {InvocationState.STARTED, InvocationState.INDETERMINATE}
                    ):
                        self._mark_unknown(node, "interrupted invocation requires reconciliation")
                    elif attempts:
                        self._replace(
                            p.model_copy(
                                update={
                                    "used": add_usage(
                                        *(a.usage for a in attempts if a.usage is not None)
                                    ),
                                }
                            )
                        )
        unknown = [n.id for n in self.state.nodes if n.status == "unknown"]
        failed = any(n.status == "failed" for n in self.state.nodes)
        pending = any(n.status == "awaiting_approval" for n in self.state.nodes)
        final_output: str | None = None
        if unknown:
            status, error = RunStatus.RUNNING, "needs_reconciliation:" + ",".join(unknown)
        elif stop is not None or self.state.cancel_requested:
            status = RunStatus.CANCELLED if self.state.cancel_requested else RunStatus.EXHAUSTED
            error = stop or "cancelled"
            for n in self.state.nodes:
                if n.status not in {"completed", "failed"}:
                    self._replace(
                        n.model_copy(update={"status": "cancelled", "reservation": Usage()})
                    )
        elif failed:
            status, error = RunStatus.FAILED, "R2 node failed"
        elif pending:
            status, error = RunStatus.AWAITING_APPROVAL, None
        elif all(n.status == "completed" for n in self.state.nodes):
            status, error = RunStatus.SUCCEEDED, None
            node = self._definition(self.orchestrator.graph.final_node)
            output = self._progress(node.id).output
            if node.kind == "tool":
                final_output = ToolOutput.model_validate(output).content
            elif node.kind == "model":
                action = ModelResult.model_validate(output).action
                final_output = action.content if isinstance(action, FinalAction) else None
            else:
                final_output = json.dumps(output, sort_keys=True)
            verdict = await self.harness.application.verifier.verify(
                VerificationRequest(
                    task_id=self.internal.run_id,
                    final=FinalAction(content=final_output or ""),
                    workspace=self.internal.scope.workspace,
                    tool_results=self._projection().tool_evidence,
                    run_context=self.internal,
                )
            )
            if not verdict.passed:
                status, error, final_output = RunStatus.FAILED, "verification rejected", None
        else:
            status, error = RunStatus.FAILED, "R2 graph cannot advance"
        self._guard(admission=False)
        self.state = self.state.model_copy(
            update={
                "result": self.state.result.model_copy(
                    update={
                        "status": status,
                        "error": error,
                        "final_output": final_output,
                    }
                )
            }
        )
        self._save("finished" if status in TERMINAL else "suspended")
        return self.state.result

    async def reconcile(self, node_id: str) -> RunResult:
        self.state = self.load(self.harness, self.context)
        self.state = self.store.take_ownership(self.state, uuid4().hex)
        self._guard(admission=False)
        node = self._definition(node_id)
        if self._progress(node_id).status != "unknown":
            raise ScopeError("only unknown nodes require reconciliation")
        resolver = self.orchestrator.reconcile_node
        if resolver is None:
            return self.state.result
        if node.kind == "tool":
            self._authorize_current(node, self._call(node), admission=False)
        resolution_key = (
            self._progress(node.id).invocation_id
            if node.kind == "model"
            else idempotency_key_for(run_id=self.internal.run_id, logical_call_id=node.id)
        )
        if resolution_key is None:
            raise ScopeError("model reconciliation requires its persisted invocation identity")
        resolution = await resolver(self.context, node, resolution_key)
        self._guard(admission=False)
        if resolution is None:
            return self.state.result
        transformed = self.harness.application.evidence_transform.transform(
            resolution.model_dump(mode="json")
        )
        resolution = ReconciledNode.model_validate(transformed)
        if node.kind == "model":
            model = ModelResult.model_validate(resolution.output)
            minimum = Usage(
                steps=1,
                input_tokens=model.usage.input_tokens,
                output_tokens=model.usage.output_tokens,
            )
        else:
            output = ToolOutput.model_validate(resolution.output)
            attempts = self.journal.attempts_for(f"{self.internal.run_id}:{node.id}")
            known = add_usage(*(a.usage for a in attempts if a.usage is not None))
            minimum = add_usage(known, Usage(tool_calls=sum(a.usage is None for a in attempts)))
        if not fits(minimum, resolution.usage):
            raise ScopeError("reconciliation cannot erase known usage or physical attempts")
        with connect_wal(self.harness.database) as connection:
            connection.execute("BEGIN IMMEDIATE")
            if node.kind == "tool":
                record = self.journal.load(run_id=self.internal.run_id, logical_call_id=node.id)
                if record is None:
                    raise ScopeError("cannot reconcile a tool with no invocation")
                if record.state in {InvocationState.COMPLETED, InvocationState.FAILED}:
                    if record.result != output:
                        raise ScopeError("reconciliation cannot replace a known result")
                else:
                    self.journal.reconcile_in_transaction(connection, record, output)
            failed = not fits(resolution.usage, node.max_usage)
            failed = failed or (node.kind == "tool" and not output.ok)
            failed = failed or (node.kind == "model" and not isinstance(model.action, FinalAction))
            schema_error = _invalid_structured_output(node, resolution.output)
            failed = failed or schema_error
            self._replace(
                self._progress(node.id).model_copy(
                    update={
                        "status": "failed" if failed else "completed",
                        "output": resolution.output,
                        "used": resolution.usage,
                        "reservation": Usage(),
                        "error": "invalid structured model output" if schema_error else None,
                        "evidence_refs": resolution.evidence_refs,
                        "dispatch_phase": "settled",
                        "completed_at": datetime.now(UTC),
                    }
                )
            )
            self._save("reconciled:" + node.id, connection=connection)
        self.publish()
        dispatcher = self.orchestrator.dispatcher
        invocation_id = self._progress(node.id).invocation_id
        if dispatcher is not None and invocation_id in dispatcher.unresolved:
            assert invocation_id is not None
            dispatcher.confirm_ended(invocation_id, evidence_refs=resolution.evidence_refs)
        return await self.advance()
