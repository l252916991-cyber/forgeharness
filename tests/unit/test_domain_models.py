"""Tests for transcript invariants at the model boundary."""

import pytest
from pydantic import ValidationError

from forgeharness.domain.models import (
    Message,
    MessageRole,
    RunResult,
    RunStatus,
    ToolCall,
    Usage,
)


def test_tool_message_requires_result_metadata() -> None:
    with pytest.raises(ValidationError, match="tool messages require"):
        Message(role=MessageRole.TOOL, content="result")


def test_assistant_accepts_structured_tool_call() -> None:
    call = ToolCall(id="call-1", name="echo", arguments={"text": "hello"})
    message = Message(role=MessageRole.ASSISTANT, tool_calls=(call,))

    assert message.tool_calls == (call,)
    assert message.content is None


@pytest.mark.parametrize("role", [MessageRole.SYSTEM, MessageRole.USER])
def test_non_tool_roles_reject_tool_metadata(role: MessageRole) -> None:
    with pytest.raises(ValidationError, match="cannot contain tool metadata"):
        Message(role=role, content="content", tool_call_id="call-1")


def test_tool_message_rejects_nested_call() -> None:
    call = ToolCall(id="call-1", name="echo")
    with pytest.raises(ValidationError, match="tool messages cannot request tools"):
        Message(
            role=MessageRole.TOOL,
            content="result",
            tool_calls=(call,),
            tool_call_id="call-1",
            tool_name="echo",
        )


def test_user_message_requires_content() -> None:
    with pytest.raises(ValidationError, match="user messages require content"):
        Message(role=MessageRole.USER)


def test_run_result_rejects_path_shaped_task_id() -> None:
    with pytest.raises(ValidationError, match="string_pattern_mismatch"):
        RunResult(task_id="../escape", status=RunStatus.CREATED, messages=(), usage=Usage())
