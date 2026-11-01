"""Read-only onboarding keeps host access, product scope and ledger-backed verification."""

import json
from pathlib import Path

import pytest

from forgeharness.applications.commerce import CommerceStore, commerce_readonly_application
from forgeharness.sdk import (
    Actor,
    FinalAction,
    Harness,
    ModelRequest,
    ModelResult,
    ResourceScope,
    RunContext,
    RunStatus,
    ScopeError,
    ScriptedModel,
    ToolAction,
    ToolCall,
)


def context(*, sku: str = "a", tenant: str = "shop") -> RunContext:
    return RunContext(
        application_id="commerce-readonly",
        application_version="1",
        policy_version="readonly-1",
        run_id="read",
        session_id="s",
        correlation_id="c",
        actor=Actor(id="customer", roles=("customer",)),
        scope=ResourceScope(tenant_id=tenant, resources=(f"product:{sku}",)),
    )


class Reader(ScriptedModel):
    def __init__(self, sku: str = "a", tool: str = "product_lookup") -> None:
        super().__init__([])
        self.sku, self.tool = sku, tool

    async def decide(self, request: ModelRequest) -> ModelResult:
        self.requests.append(request)
        if request.messages[-1].role.value == "tool":
            return ModelResult(action=FinalAction(content=request.messages[-1].content or ""))
        return ModelResult(
            action=ToolAction(
                call=ToolCall(id="lookup", name=self.tool, arguments={"sku": self.sku})
            )
        )


def setup(tmp_path: Path, model: Reader) -> tuple[Harness, CommerceStore, set[str]]:
    store = CommerceStore(tmp_path / "shop.sqlite")
    store.seed(tenant="shop", sku="a", name="sample", price_cents=100, stock=2)
    store.seed(tenant="other", sku="a", name="private", price_cents=999, stock=8)
    active = {"customer"}
    app = commerce_readonly_application(
        store=store,
        model_factory=lambda: model,
        authorize=lambda ctx: ctx.actor.id in active and ctx.scope.tenant_id == "shop",
    )
    return Harness(app, state_dir=tmp_path / "state"), store, active


async def test_readonly_factory_reads_current_scoped_product_and_rebuilds(tmp_path: Path) -> None:
    model = Reader()
    harness, _, active = setup(tmp_path, model)
    result = await harness.run(context=context(), task="read current price")
    assert result.status is RunStatus.SUCCEEDED
    assert json.loads(result.final_output)["price_cents"] == 100
    assert [t.spec.name for t in harness.application.tools] == ["product_lookup"]
    rebuilt = Harness(harness.application, state_dir=harness.state_dir)
    assert rebuilt.inspect(context()) == result
    active.clear()
    with pytest.raises(ScopeError, match="authorized"):
        rebuilt.inspect(context())


async def test_revoked_access_never_reaches_the_model(tmp_path: Path) -> None:
    model = Reader()
    harness, _, active = setup(tmp_path, model)
    active.clear()
    with pytest.raises(ScopeError):
        await harness.run(context=context(), task="read")
    assert model.requests == []


async def test_another_tenant_is_denied_before_model_input(tmp_path: Path) -> None:
    model = Reader()
    harness, _, _ = setup(tmp_path, model)
    with pytest.raises(ScopeError):
        await harness.run(context=context(tenant="other"), task="read")
    assert model.requests == []


async def test_product_outside_scope_cannot_be_reported_as_success(tmp_path: Path) -> None:
    harness, _, _ = setup(tmp_path, Reader(sku="outside"))
    result = await harness.run(context=context(), task="read another product")
    assert result.status is not RunStatus.SUCCEEDED
    assert not result.tool_evidence


async def test_invented_write_tool_cannot_create_a_ticket(tmp_path: Path) -> None:
    harness, store, _ = setup(tmp_path, Reader(tool="create_handoff"))
    result = await harness.run(context=context(), task="create a ticket without approval")
    assert result.status is not RunStatus.SUCCEEDED
    assert result.pending_approval is None
    with store.connect() as connection:
        assert connection.execute("SELECT count(*) FROM tickets").fetchone()[0] == 0


async def test_revocation_after_read_blocks_next_model_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = Reader()
    harness, _, active = setup(tmp_path, model)
    tool = harness.application.tools[0]
    execute = tool.execute

    async def read_then_revoke(*args: object, **kwargs: object) -> object:
        result = await execute(*args, **kwargs)
        active.clear()
        return result

    monkeypatch.setattr(tool, "execute", read_then_revoke)
    result = await harness.run(context=context(), task="read")
    assert result.status is RunStatus.FAILED
    assert len(model.requests) == 1
    with pytest.raises(ScopeError):
        harness.inspect(context())


async def test_changed_ledger_invalidates_final_claim(tmp_path: Path) -> None:
    class StaleReader(Reader):
        store: CommerceStore

        async def decide(self, request: ModelRequest) -> ModelResult:
            results = [message for message in request.messages if message.role.value == "tool"]
            if results:
                self.store.seed(
                    tenant="shop", sku="a", name="new", price_cents=200, stock=1, version=2
                )
                self.requests.append(request)
                return ModelResult(action=FinalAction(content=results[0].content or ""))
            return await super().decide(request)

    model = StaleReader()
    harness, model.store, _ = setup(tmp_path, model)
    result = await harness.run(context=context(), task="read")
    assert result.status is not RunStatus.SUCCEEDED


def test_readonly_policy_never_issues_write_approval(tmp_path: Path) -> None:
    from forgeharness.applications.commerce import TicketTool
    from forgeharness.sdk import AuthorizationRequest, PolicyDecisionType

    harness, store, _ = setup(tmp_path, Reader())
    call = ToolCall(id="write", name="create_handoff", arguments={"sku": "a"})
    assert not harness.application.policy.can_approve(
        requester=context(), owner=context(), call=call
    )
    decision = harness.application.policy.evaluate(
        AuthorizationRequest(context=context(), call=call, spec=TicketTool(store).spec)
    )
    assert decision.type is PolicyDecisionType.DENY


async def test_repeated_model_call_id_finishes_failed_instead_of_leaving_running(
    tmp_path: Path,
) -> None:
    class ReusedIdReader(Reader):
        store: CommerceStore

        async def decide(self, request: ModelRequest) -> ModelResult:
            if len(self.requests) == 1:
                self.store.seed(
                    tenant="shop", sku="a", name="new", price_cents=200, stock=1, version=2
                )
            return await super().decide(request)

    model = ReusedIdReader()
    harness, model.store, _ = setup(tmp_path, model)
    result = await harness.run(context=context(), task="read")
    assert result.status is RunStatus.FAILED
    assert len(model.requests) == 3
    assert result.usage.tool_calls == 1
    assert result.error == "model reused a prior tool call id"
    rebuilt = Harness(harness.application, state_dir=harness.state_dir)
    assert rebuilt.inspect(context()).status is RunStatus.FAILED
    with model.store.connect() as connection:
        assert connection.execute("SELECT count(*) FROM tickets").fetchone()[0] == 0
