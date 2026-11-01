"""A focused, keyless read-only commerce integration using public APIs only."""

import asyncio
import json
import tempfile
from pathlib import Path

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
    ToolAction,
    ToolCall,
)


class FixtureModel:
    """Deterministic wiring fixture; replace its factory to use a real model."""

    async def decide(self, request: ModelRequest) -> ModelResult:
        if request.messages[-1].role.value == "tool":
            return ModelResult(action=FinalAction(content=request.messages[-1].content or ""))
        sku = json.loads(request.messages[-1].content or "{}")["sku"]
        return ModelResult(
            action=ToolAction(
                call=ToolCall(id="lookup", name="product_lookup", arguments={"sku": sku})
            )
        )


async def demo(root: Path) -> None:
    store = CommerceStore(root / "shop.sqlite")
    store.seed(
        tenant="demo-shop", sku="sample", name="Synthetic product", price_cents=9900, stock=3
    )
    # A host-owned access table for this fixture, not production authentication.
    active_users = {"demo-customer"}
    app = commerce_readonly_application(
        store=store,
        model_factory=FixtureModel,
        authorize=lambda ctx: ctx.actor.id in active_users and ctx.scope.tenant_id == "demo-shop",
    )
    harness = Harness(app, state_dir=root / "state")
    context = RunContext(
        application_id=app.id,
        application_version=app.version,
        policy_version=app.policy_version,
        run_id="lookup-1",
        session_id="consultation-1",
        correlation_id="demo-1",
        actor=Actor(id="demo-customer", roles=("customer",)),
        scope=ResourceScope(tenant_id="demo-shop", resources=("product:sample",)),
    )
    result = await harness.run(context=context, task=json.dumps({"sku": "sample"}))
    assert result.status is RunStatus.SUCCEEDED
    print("synthetic product:", result.final_output)
    # The durable result can be inspected after rebuilding the controller.
    assert Harness(app, state_dir=root / "state").inspect(context) == result


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="forge-commerce-readonly-") as directory:
        asyncio.run(demo(Path(directory)))
