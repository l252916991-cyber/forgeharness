"""Run both reference applications from the installed wheel, without a model key."""

import asyncio
import tempfile
from pathlib import Path

from forgeharness.applications.commerce import CommerceStore, commerce_application
from forgeharness.coding.application import coding_application
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


class CommerceModel:
    async def decide(self, request: ModelRequest) -> ModelResult:
        if request.messages[-1].role.value == "tool":
            return ModelResult(action=FinalAction(content=request.messages[-1].content or ""))
        return ModelResult(
            action=ToolAction(
                call=ToolCall(id="lookup", name="product_lookup", arguments={"sku": "sample"})
            )
        )


class CodingModel:
    async def decide(self, request: ModelRequest) -> ModelResult:
        results = [message for message in request.messages if message.role.value == "tool"]
        if not results:
            action = ToolAction(
                call=ToolCall(
                    id="write",
                    name="write_file",
                    arguments={"path": "sample.txt", "content": "framework demo\n"},
                )
            )
        elif results[-1].tool_name == "write_file":
            action = ToolAction(call=ToolCall(id="test", name="run_tests", arguments={}))
        else:
            action = FinalAction(content="sample written; verification command passed")
        return ModelResult(action=action)


def setup(root: Path) -> tuple[CommerceStore, Path]:
    store = CommerceStore(root / "commerce.sqlite")
    store.seed(tenant="sample-store", sku="sample", name="Sample", price_cents=100, stock=2)
    repository = root / "repository"
    repository.mkdir()
    (repository / ".git").mkdir()  # Minimal coding context fixture, not a Git correctness proof.
    return store, repository.resolve()


async def demo(root: Path) -> None:
    store, repository = setup(root)
    commerce = Harness(
        commerce_application(store=store, model_factory=CommerceModel),
        state_dir=root / "commerce-state",
    )
    commerce_context = RunContext(
        application_id="commerce",
        application_version="1",
        policy_version="1",
        run_id="demo",
        session_id="demo",
        correlation_id="demo",
        actor=Actor(id="host", roles=("customer",)),
        scope=ResourceScope(tenant_id="sample-store", resources=("product:sample",)),
    )
    result = await commerce.run(context=commerce_context, task="read sample product")
    assert result.status is RunStatus.SUCCEEDED
    print("commerce:", result.final_output)
    coding = Harness(
        coding_application(model_factory=CodingModel, test_command=("true",)),
        state_dir=root / "coding-state",
    )
    coding_context = RunContext(
        application_id="coding",
        application_version="1",
        policy_version="1",
        run_id="demo",
        session_id="demo",
        correlation_id="demo",
        actor=Actor(id="host", roles=("developer", "maintainer")),
        scope=ResourceScope(tenant_id="local", workspace=repository),
    )
    suspended = await coding.run(context=coding_context, task="write a sample")
    assert suspended.status is RunStatus.AWAITING_APPROVAL
    grant = coding.approve(context=coding_context, approver=coding_context)
    # Rebuild controller to demonstrate durable approval, independent of active handles.
    rebuilt = Harness(coding.application, state_dir=root / "coding-state")
    result = await rebuilt.resume(context=coding_context, grant=grant, approver=coding_context)
    assert result.status is RunStatus.SUCCEEDED
    print("coding:", result.final_output)


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="forge-sdk-demo-") as directory:
        asyncio.run(demo(Path(directory)))
