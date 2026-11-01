"""Same runtime matrix for a legal DAG and the commerce ReAct app."""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from forgeharness.applications.commerce import CommerceStore, commerce_application
from forgeharness.applications.legal.lexvault import (
    BundleCaseAdapter,
    BundleCitationVerifier,
    BundlePage,
    CaseBundle,
    LexVaultDagOrchestrator,
    lexvault_dag_application,
)
from forgeharness.sdk import (
    Actor,
    FinalAction,
    Harness,
    MessageRole,
    ModelRequest,
    ModelResult,
    ResourceScope,
    RunContext,
    RunStatus,
    ScopeError,
    ScriptedModel,
    ToolAction,
    ToolCall,
    verify_trace,
)


@dataclass(frozen=True)
class RuntimeCase:
    harness: Harness
    context: RunContext
    task: str
    wrong_context: RunContext
    unauthorized_context: RunContext


class ProductModel:
    """Call product_lookup once, then return the tool's exact JSON."""

    async def decide(self, request: ModelRequest) -> ModelResult:
        for message in reversed(request.messages):
            if message.role is MessageRole.TOOL:
                return ModelResult(action=FinalAction(content=message.content or ""))
        return ModelResult(
            action=ToolAction(
                call=ToolCall(
                    id="lookup",
                    name="product_lookup",
                    arguments={"sku": "a"},
                )
            )
        )


def build_legal(tmp_path: Path, *, run_id: str, invalid: bool) -> RuntimeCase:
    bundle = CaseBundle(
        tenant_id="tenant-a",
        case_id="case-runtime",
        title="Runtime conformance",
        pages=(BundlePage(page_no=1, text="甲公司支付资金100万元。", summary="付款"),),
    )
    adapter = BundleCaseAdapter(bundle)
    verifier = BundleCitationVerifier(bundle)
    final = "not a cited answer" if invalid else "材料不足[page:1]。"
    app = lexvault_dag_application(
        model_factory=lambda: ScriptedModel([ModelResult(action=FinalAction(content=final))]),
        verifier=verifier,
    )
    harness = Harness(
        app,
        state_dir=tmp_path / "state",
        orchestrator=LexVaultDagOrchestrator(adapter, case_id=bundle.case_id),
    )
    context = RunContext(
        application_id="lexvault-dag",
        application_version="1",
        policy_version="1",
        run_id=run_id,
        session_id="runtime-conformance",
        actor=Actor(id="lawyer", roles=("use",)),
        scope=ResourceScope(
            tenant_id="tenant-a",
            resources=("case:case-runtime", "case:case-runtime/page:1"),
        ),
        correlation_id=run_id,
    )
    return RuntimeCase(
        harness=harness,
        context=context,
        task="梳理资金流向",
        wrong_context=context.model_copy(update={"application_id": "another-app"}),
        unauthorized_context=context.model_copy(
            update={"scope": context.scope.model_copy(update={"resources": ("case:case-runtime",)})}
        ),
    )


def build_commerce(tmp_path: Path, *, run_id: str, invalid: bool) -> RuntimeCase:
    store = CommerceStore(tmp_path / "commerce.sqlite")
    store.seed(
        tenant="tenant-a",
        sku="a",
        name="synthetic product",
        price_cents=123,
        stock=4,
    )
    model = (
        ScriptedModel([ModelResult(action=FinalAction(content="not a product receipt"))])
        if invalid
        else ProductModel()
    )
    app = commerce_application(store=store, model_factory=lambda: model)
    harness = Harness(app, state_dir=tmp_path / "state")
    context = RunContext(
        application_id="commerce",
        application_version="1",
        policy_version="1",
        run_id=run_id,
        session_id="runtime-conformance",
        actor=Actor(id="customer", roles=("customer",)),
        scope=ResourceScope(tenant_id="tenant-a", resources=("product:a",)),
        correlation_id=run_id,
    )
    return RuntimeCase(
        harness=harness,
        context=context,
        task="look up product a",
        wrong_context=context.model_copy(update={"application_id": "another-app"}),
        unauthorized_context=context.model_copy(
            update={"scope": context.scope.model_copy(update={"resources": ()})}
        ),
    )


BUILDERS: tuple[Callable[..., RuntimeCase], ...] = (build_legal, build_commerce)


@pytest.mark.parametrize("builder", BUILDERS, ids=lambda item: item.__name__)
async def test_success_run_trace_and_inspect(
    tmp_path: Path, builder: Callable[..., RuntimeCase]
) -> None:
    case = builder(tmp_path, run_id="run-success", invalid=False)

    result = await case.harness.run(context=case.context, task=case.task)

    assert result.status is RunStatus.SUCCEEDED
    assert case.harness.inspect(case.context).status is RunStatus.SUCCEEDED
    assert verify_trace(case.harness.trace_path(case.context)).valid


@pytest.mark.parametrize("builder", BUILDERS, ids=lambda item: item.__name__)
async def test_invalid_final_fails_verification(
    tmp_path: Path, builder: Callable[..., RuntimeCase]
) -> None:
    case = builder(tmp_path, run_id="run-invalid", invalid=True)

    result = await case.harness.run(context=case.context, task=case.task)

    assert result.status is RunStatus.FAILED
    assert result.error is not None
    assert "verification.rejected" in case.harness.trace_path(case.context).read_text()


@pytest.mark.parametrize("builder", BUILDERS, ids=lambda item: item.__name__)
async def test_wrong_application_binding_is_rejected(
    tmp_path: Path, builder: Callable[..., RuntimeCase]
) -> None:
    case = builder(tmp_path, run_id="run-wrong-binding", invalid=False)

    with pytest.raises(ScopeError, match="application or policy version mismatch"):
        await case.harness.run(context=case.wrong_context, task=case.task)


@pytest.mark.parametrize("builder", BUILDERS, ids=lambda item: item.__name__)
async def test_unauthorized_resource_fails_closed(
    tmp_path: Path, builder: Callable[..., RuntimeCase]
) -> None:
    case = builder(tmp_path, run_id="run-unauthorized", invalid=False)

    result = await case.harness.run(context=case.unauthorized_context, task=case.task)

    assert result.status is not RunStatus.SUCCEEDED
    assert result.error is not None
