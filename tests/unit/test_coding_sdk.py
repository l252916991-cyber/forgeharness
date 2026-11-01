"""Coding via the same SDK as commerce; evidence persists through approval."""

from pathlib import Path

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
    ToolContext,
)


class CodingModel:
    async def decide(self, request: ModelRequest) -> ModelResult:
        tool_results = [message for message in request.messages if message.role.value == "tool"]
        if not tool_results:
            action = ToolAction(
                call=ToolCall(id="read", name="read_file", arguments={"path": "existing.py"})
            )
        elif tool_results[-1].tool_name == "read_file":
            action = ToolAction(
                call=ToolCall(
                    id="write",
                    name="write_file",
                    arguments={"path": "new.py", "content": "value = 1\n"},
                )
            )
        elif tool_results[-1].tool_name == "write_file":
            action = ToolAction(call=ToolCall(id="test", name="run_tests", arguments={}))
        else:
            action = FinalAction(content="changed and verified")
        return ModelResult(action=action)


async def test_coding_sdk_durable_approval_and_verification(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / ".git").mkdir()
    (workspace / "existing.py").write_text("value = 0\n")
    context = RunContext(
        application_id="coding",
        application_version="1",
        policy_version="1",
        run_id="coding-run",
        session_id="coding-session",
        correlation_id="c",
        actor=Actor(id="developer", roles=("developer", "maintainer")),
        scope=ResourceScope(tenant_id="local", workspace=workspace.resolve()),
    )
    spec = coding_application(model_factory=CodingModel, test_command=("true",))
    harness = Harness(spec, state_dir=tmp_path / "state")
    suspended = await harness.run(context=context, task="create a file")
    assert suspended.status is RunStatus.AWAITING_APPROVAL
    grant = harness.approve(context=context, approver=context)
    rebuilt = Harness(spec, state_dir=tmp_path / "state")
    result = await rebuilt.resume(context=context, grant=grant, approver=context)
    assert result.status is RunStatus.SUCCEEDED
    assert [item.name for item in result.tool_evidence] == ["read_file", "write_file", "run_tests"]
    assert (workspace / "new.py").read_text() == "value = 1\n"
    assert result.run_context.scope.workspace == workspace.resolve()


def test_changed_workspace_root_is_not_a_new_authority(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    trusted = RunContext(
        application_id="coding",
        application_version="1",
        policy_version="1",
        run_id="r",
        session_id="s",
        correlation_id="c",
        actor=Actor(id="host"),
        scope=ResourceScope(tenant_id="local", workspace=root.resolve()),
    )
    context = ToolContext(task_id="r", workspace=root, run_context=trusted)
    root.rmdir()
    root.symlink_to(outside, target_is_directory=True)
    import pytest

    with pytest.raises(ValueError, match="root changed"):
        context.require_workspace()
