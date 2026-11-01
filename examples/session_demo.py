"""Keyless S1 acceptance client: synthetic sessions, history, approval and restart.

No legal/customer data, external API or real-model quality claim is involved.
"""

import asyncio
import json
import tempfile
from dataclasses import replace
from pathlib import Path

from forgeharness.applications.legal import legal_review_workflow
from forgeharness.sdk import (
    Actor,
    ApplicationSpec,
    AuthorizationRequest,
    ContextMaterial,
    FinalAction,
    ModelRequest,
    ModelResult,
    NoopVerifier,
    PolicyDecision,
    PolicyDecisionType,
    PublishedContextProvider,
    RecordDraft,
    ResourceScope,
    RevisionStore,
    RoleRecordPolicy,
    RunContext,
    RunStatus,
    ScopeError,
    SourceAnchor,
    ToolCall,
)
from forgeharness.sdk.evidence import published_source_validator
from forgeharness.sdk.sessions import Session, SessionOperation, SessionService


class Host:
    """Synthetic stand-in for an authenticated host and its current source ledger."""

    def __init__(self) -> None:
        self.active = {"owner", "supervisor"}
        self.version = "1"

    def authorize(
        self, context: RunContext, operation: SessionOperation, session: Session | None
    ) -> bool:
        return context.actor.id in self.active

    def validate(self, context: RunContext, materials: tuple[ContextMaterial, ...]) -> bool:
        return all(material.version == self.version for material in materials)


class Source:
    async def provide(self, *, task: str, context: RunContext) -> tuple[ContextMaterial, ...]:
        return (
            ContextMaterial(
                id="synthetic-source",
                source="synthetic-fixture",
                content="Synthetic source only: the demonstration value is 100.",
                version="1",
                application_id=context.application_id,
                tenant_id=context.scope.tenant_id,
                resource_id="source:1",
            ),
        )


class ReadOnlyPolicy:
    def evaluate(self, request: AuthorizationRequest) -> PolicyDecision:
        return PolicyDecision(type=PolicyDecisionType.DENY, reason="No tools in chat fixture")

    def can_approve(self, *, requester: RunContext, owner: RunContext, call: ToolCall) -> bool:
        return False


class HistoryModel:
    def __init__(self, requests: list[ModelRequest]) -> None:
        self.requests = requests

    async def decide(self, request: ModelRequest) -> ModelResult:
        self.requests.append(request)
        return ModelResult(
            action=FinalAction(content="Synthetic answer: demonstration value 100."),
            model_name="deterministic-session-fixture",
        )


def identity(application: str, session: str = "conversation") -> RunContext:
    return RunContext(
        application_id=application,
        application_version="1",
        policy_version="1",
        session_id=session,
        run_id="authenticated-request",
        correlation_id="synthetic-request",
        actor=Actor(id="owner", roles=("editor", "reader", "customer")),
        scope=ResourceScope(tenant_id="synthetic-tenant", resources=("source:1", "draft:1")),
    )


def approver(context: RunContext, role: str = "supervisor") -> RunContext:
    return context.model_copy(update={"actor": Actor(id=role, roles=(role,))})


async def demo(root: Path) -> dict[str, object]:
    host = Host()
    results: dict[str, object] = {"data": "synthetic", "model": "deterministic fixture"}
    for domain in ("legal", "commerce"):
        requests: list[ModelRequest] = []
        definition = ApplicationSpec(
            domain,
            "1",
            "1",
            # Bind the per-domain recorder now; a bare closure would share the
            # last loop value if the factory were ever called after the loop.
            model_factory=lambda requests=requests: HistoryModel(requests),
            tools=(),
            policy=ReadOnlyPolicy(),
            verifier=NoopVerifier(),
            context_providers=(Source(),),
        )
        context = identity(domain)
        service = SessionService(
            definition,
            state_dir=root / domain,
            authorize=host.authorize,
            validate_materials=host.validate,
        )
        service.create(context)
        first = await service.submit(context, request_id="first", text="first-turn-marker")
        assert first.status == RunStatus.SUCCEEDED
        assert (await service.submit(context, request_id="first", text="first-turn-marker")).id == (
            first.id
        )
        service = SessionService(
            definition,
            state_dir=root / domain,
            authorize=host.authorize,
            validate_materials=host.validate,
        )
        second = await service.submit(context, request_id="second", text="Please explain again")
        assert second.status == RunStatus.SUCCEEDED
        assert any(
            "first-turn-marker" in (message.content or "") for message in requests[-1].messages
        )
        assert len(service.history(context)) == 2
        assert service.events(context, second.id)
        stranger = context.model_copy(update={"scope": ResourceScope(tenant_id="another-tenant")})
        try:
            service.history(stranger)
        except ScopeError:
            pass
        else:
            raise AssertionError("cross-tenant access was not denied")
        host.active.remove("owner")
        try:
            service.history(context)
        except ScopeError:
            pass
        else:
            raise AssertionError("revocation was not enforced")
        host.active.add("owner")
        service.close(context)
        results[domain] = "two turns, restart, replay, isolation, revocation and close verified"

    context = identity("legal", "note-workflow")
    store = RevisionStore(root / "records.sqlite", policy=RoleRecordPolicy())
    store.save(
        context,
        RecordDraft(
            resource_id="source:1",
            kind="synthetic_source",
            content="Synthetic case: a payment record lists 100 units.",
            sources=(SourceAnchor(source="synthetic-fixture", version="1", locator="page:1"),),
        ),
        expected_version=0,
        request_id="seed",
    )
    store.review(approver(context, "reviewer"), "source:1", expected_version=1, approve=True)
    store.publish(approver(context, "publisher"), "source:1", expected_version=1)
    workflow = legal_review_workflow(
        store=store, evidence_id="source:1", note_id="draft:1", business_request_id="note"
    )
    # Explicit source dependency also covers material read by the source tool.
    provider = PublishedContextProvider(store, ("source:1",))
    workflow = replace(
        workflow,
        steps=tuple(
            replace(step, application=replace(step.application, context_providers=(provider,)))
            for step in workflow.steps
        ),
    )

    def current_source(current: RunContext, materials: tuple[ContextMaterial, ...]) -> bool:
        revision = store.published(current, "source:1")
        return all(item.version == str(revision.version) for item in materials)

    notes = SessionService(
        workflow,
        state_dir=root / "notes",
        authorize=host.authorize,
        validate_materials=current_source,
        validate_sources=published_source_validator(store),
    )
    notes.create(context)
    waiting = await notes.submit(context, request_id="note", text="Prepare a cited draft")
    assert waiting.status == RunStatus.AWAITING_APPROVAL
    supervisor = approver(context)
    grant = notes.approve(context, waiting.id, approver=supervisor)
    notes = SessionService(
        workflow,
        state_dir=root / "notes",
        authorize=host.authorize,
        validate_materials=current_source,
        validate_sources=published_source_validator(store),
    )
    completed = await notes.resume(context, waiting.id, grant=grant, approver=supervisor)
    assert completed.status == RunStatus.SUCCEEDED
    assert store.read(context, "draft:1").state == "draft"
    results["workflow"] = (
        "delegated approval survived restart; note awaits separate business review"
    )
    return results


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="forge-session-demo-") as directory:
        print(json.dumps(asyncio.run(demo(Path(directory))), ensure_ascii=False, indent=2))
