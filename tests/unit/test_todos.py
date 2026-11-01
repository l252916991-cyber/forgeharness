"""Tests for the mutable todo board and the finish gate it enforces."""

import itertools
from pathlib import Path

import pytest

from forgeharness.domain.models import (
    FinalAction,
    Message,
    MessageRole,
    ModelResult,
    ModelUsage,
    RunStatus,
    ToolAction,
    ToolCall,
)
from forgeharness.models.scripted import ScriptedModel
from forgeharness.observability.trace import InMemoryTrace
from forgeharness.runtime.loop import AgentRuntime
from forgeharness.runtime.todos import (
    MAX_TODOS,
    TodoBoard,
    TodoItem,
    TodoSnapshot,
    TodoStatus,
    WriteTodosInput,
    WriteTodosTool,
    render_todos,
    replay_board,
    todo_pinned_message,
)
from forgeharness.tools.base import ToolContext
from forgeharness.tools.dispatcher import ToolDispatcher
from forgeharness.tools.policy import RiskBasedPolicy
from forgeharness.tools.registry import ToolRegistry


def item(identifier: str, status: TodoStatus = TodoStatus.PENDING) -> TodoItem:
    return TodoItem(id=identifier, content=f"do {identifier}", status=status)


_CALLS = itertools.count(1)


def write(*items: TodoItem, rationale: str | None = None) -> ModelResult:
    """A model turn that replaces the board, with a fresh call id each time."""
    call = ToolCall(
        id=f"call-{next(_CALLS)}",
        name="write_todos",
        arguments=WriteTodosInput(todos=items, rationale=rationale).model_dump(mode="json"),
    )
    return ModelResult(
        action=ToolAction(call=call), usage=ModelUsage(input_tokens=5, output_tokens=5)
    )


def finish(content: str = "done") -> ModelResult:
    return ModelResult(
        action=FinalAction(content=content), usage=ModelUsage(input_tokens=3, output_tokens=2)
    )


def runtime(
    model: ScriptedModel,
    trace: InMemoryTrace,
    *,
    todos: TodoBoard | None = None,
    max_todo_rejections: int = 3,
) -> AgentRuntime:
    registry = ToolRegistry()
    return AgentRuntime(
        model=model,
        registry=registry,
        dispatcher=ToolDispatcher(registry),
        policy=RiskBasedPolicy(),
        trace=trace,
        todos=todos,
        max_todo_rejections=max_todo_rejections,
    )


def test_board_rejects_duplicate_ids_and_parallel_in_progress() -> None:
    with pytest.raises(ValueError, match="ids must be unique"):
        TodoBoard([item("a"), item("a")])
    with pytest.raises(ValueError, match="at most one todo may be in progress"):
        TodoBoard([item("a", TodoStatus.IN_PROGRESS), item("b", TodoStatus.IN_PROGRESS)])
    with pytest.raises(ValueError, match="at most 50 items"):
        TodoBoard([item(f"i{index}") for index in range(MAX_TODOS + 1)])


def test_revision_advances_only_when_content_changes() -> None:
    board = TodoBoard()
    assert board.revision == 0

    first = board.replace([item("a", TodoStatus.IN_PROGRESS)])
    assert first.revision == 1

    # The same payload twice must not invent a revision: that is what makes the
    # tool honestly IDEMPOTENT under at-least-once dispatch.
    repeated = board.replace([item("a", TodoStatus.IN_PROGRESS)])
    assert repeated.revision == 1
    assert repeated.digest == first.digest

    changed = board.replace([item("a", TodoStatus.COMPLETED)])
    assert changed.revision == 2
    assert changed.digest != first.digest
    assert changed.open_count == 0
    assert changed.completed_count == 1


def test_render_names_every_item_and_refuses_finish_while_open() -> None:
    snapshot = TodoBoard(
        [item("read", TodoStatus.COMPLETED), item("patch", TodoStatus.IN_PROGRESS)]
    ).snapshot()

    rendered = render_todos(snapshot)

    assert rendered.startswith("[todo state: revision 1; 1 open, 1 completed]")
    assert "- [x] read: do read (completed)" in rendered
    assert "- [~] patch: do patch (in_progress)" in rendered
    assert "[finish is refused while any item is open]" in rendered


def test_render_is_bounded_and_keeps_the_header() -> None:
    padded = TodoBoard(
        [TodoItem(id=f"t{index}", content="content " * 40) for index in range(MAX_TODOS)]
    )

    rendered = render_todos(padded.snapshot())

    assert len(rendered) <= 4_000 + len("\n[board truncated]")
    assert "[board truncated]" in rendered
    assert rendered.startswith("[todo state:")


def test_an_empty_board_pins_nothing() -> None:
    assert todo_pinned_message(TodoBoard().snapshot()) is None
    assert todo_pinned_message(TodoBoard([item("a")]).snapshot()) is not None


async def test_write_todos_reports_the_new_revision() -> None:
    board = TodoBoard()
    tool = WriteTodosTool(board)

    output = await tool.execute(
        WriteTodosInput(todos=(item("a", TodoStatus.IN_PROGRESS),), rationale="start with a"),
        ToolContext(task_id="task"),
    )

    assert output.ok is True
    assert output.metadata["revision"] == 1
    assert output.metadata["open_count"] == 1
    assert output.metadata["rationale"] == "start with a"
    assert board.revision == 1


async def test_write_todos_returns_a_malformed_board_as_an_observation() -> None:
    board = TodoBoard()
    output = await WriteTodosTool(board).execute(
        WriteTodosInput(
            todos=(item("a", TodoStatus.IN_PROGRESS), item("b", TodoStatus.IN_PROGRESS))
        ),
        ToolContext(task_id="task"),
    )

    assert output.ok is False
    assert output.metadata["error_code"] == "invalid_todos"
    assert "at most one todo may be in progress" in output.content
    assert board.revision == 0


def test_replay_board_prefers_the_last_accepted_write() -> None:
    messages = (
        Message(role=MessageRole.USER, content="fix it"),
        Message(
            role=MessageRole.ASSISTANT,
            content="",
            tool_calls=(
                ToolCall(
                    id="c1",
                    name="write_todos",
                    arguments=WriteTodosInput(todos=[item("a")]).model_dump(mode="json"),
                ),
            ),
        ),
        Message(
            role=MessageRole.TOOL,
            tool_call_id="c1",
            tool_name="write_todos",
            content=render_todos(TodoSnapshot(revision=1, todos=(item("a"),))),
        ),
        Message(
            role=MessageRole.ASSISTANT,
            content="",
            tool_calls=(
                ToolCall(
                    id="c2",
                    name="write_todos",
                    arguments=WriteTodosInput(
                        todos=[item("a", TodoStatus.COMPLETED), item("b")]
                    ).model_dump(mode="json"),
                ),
            ),
        ),
        Message(
            role=MessageRole.TOOL,
            tool_call_id="c2",
            tool_name="write_todos",
            content=render_todos(
                TodoSnapshot(revision=2, todos=(item("a", TodoStatus.COMPLETED), item("b")))
            ),
        ),
    )

    board = replay_board(messages)

    assert [entry.id for entry in board.items] == ["a", "b"]
    assert board.snapshot().open_count == 1
    assert board.revision == 2


def test_replay_board_skips_a_write_the_tool_would_have_rejected() -> None:
    rejected = WriteTodosInput(
        todos=[item("x", TodoStatus.IN_PROGRESS), item("y", TodoStatus.IN_PROGRESS)]
    ).model_dump(mode="json")
    messages = (
        Message(
            role=MessageRole.ASSISTANT,
            content="",
            tool_calls=(
                ToolCall(
                    id="ok",
                    name="write_todos",
                    arguments=WriteTodosInput(todos=[item("a")]).model_dump(mode="json"),
                ),
            ),
        ),
        Message(
            role=MessageRole.TOOL,
            tool_call_id="ok",
            tool_name="write_todos",
            content=render_todos(TodoSnapshot(revision=1, todos=(item("a"),))),
        ),
        Message(
            role=MessageRole.ASSISTANT,
            content="",
            tool_calls=(ToolCall(id="bad", name="write_todos", arguments=rejected),),
        ),
    )

    assert [entry.id for entry in replay_board(messages).items] == ["a"]
    assert replay_board(()).items == ()


async def test_a_final_answer_with_an_open_todo_is_refused(tmp_path: Path) -> None:
    trace = InMemoryTrace("task-1")
    model = ScriptedModel(
        [
            write(item("a", TodoStatus.IN_PROGRESS)),
            finish("premature"),
            write(item("a", TodoStatus.COMPLETED)),
            finish("complete"),
        ]
    )

    result = await runtime(model, trace, todos=TodoBoard()).run(
        task_id="task-1", task="fix the defect", workspace=tmp_path
    )

    assert result.status == RunStatus.SUCCEEDED
    assert result.final_output == "complete"
    rejected = [event for event in trace.events if event.type == "todo.final_rejected"]
    assert len(rejected) == 1
    assert rejected[0].payload["open_count"] == 1
    assert rejected[0].payload["items"] == ["a"]
    # The rejection is fed back as a user turn so the model can react to it.
    feedback = [message for message in result.messages if message.role is MessageRole.USER]
    assert any("todo board still has 1 open item" in message.content for message in feedback)
    updated = [event for event in trace.events if event.type == "todo.updated"]
    assert [event.payload["open_count"] for event in updated] == [1, 0]


async def test_persistent_unfinished_todos_fail_the_run(tmp_path: Path) -> None:
    trace = InMemoryTrace("task-1")
    model = ScriptedModel(
        [
            write(item("a")),
            finish("first"),
            finish("second"),
        ]
    )

    result = await runtime(model, trace, todos=TodoBoard(), max_todo_rejections=1).run(
        task_id="task-1", task="fix the defect", workspace=tmp_path
    )

    assert result.status == RunStatus.FAILED
    assert result.error is not None
    assert result.error.startswith("unfinished_todos:")
    assert "a" in result.error


async def test_a_run_without_a_board_keeps_the_old_finish_behaviour(tmp_path: Path) -> None:
    trace = InMemoryTrace("task-1")

    result = await runtime(ScriptedModel([finish()]), trace).run(
        task_id="task-1", task="fix the defect", workspace=tmp_path
    )

    assert result.status == RunStatus.SUCCEEDED
    assert [event.type for event in trace.events if event.type.startswith("todo.")] == []


def test_the_todo_tool_is_registered_only_with_a_board() -> None:
    plain = runtime(ScriptedModel([]), InMemoryTrace("task-1"))
    managed = runtime(ScriptedModel([]), InMemoryTrace("task-1"), todos=TodoBoard())

    assert plain._registry.get("write_todos") is None
    assert managed._registry.get("write_todos") is not None
    assert "write_todos" in [spec.name for spec in managed._registry.specs()]


async def test_a_second_run_does_not_inherit_the_first_boards_items(tmp_path: Path) -> None:
    board = TodoBoard()
    trace = InMemoryTrace("task-1")
    agent = runtime(ScriptedModel([finish()]), trace, todos=board)
    board.replace([item("leftover", TodoStatus.PENDING)])

    result = await agent.run(task_id="task-1", task="unrelated task", workspace=tmp_path)

    assert result.status == RunStatus.SUCCEEDED
    assert board.items == ()
