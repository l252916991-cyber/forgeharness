"""Todo recovery uses successful tool facts, never unexecuted model intent."""

from pathlib import Path

import pytest

from forgeharness.context.compression import ObservationCompressor
from forgeharness.domain.models import (
    FinalAction,
    Message,
    MessageRole,
    ModelResult,
    RunResult,
    RunStatus,
    ToolAction,
    ToolCall,
    ToolEvidence,
)
from forgeharness.models.scripted import ScriptedModel
from forgeharness.observability.trace import InMemoryTrace
from forgeharness.runtime.loop import AgentRuntime
from forgeharness.runtime.todos import (
    TodoBoard,
    TodoItem,
    TodoStatus,
    WriteTodosInput,
    WriteTodosTool,
    replay_board,
)
from forgeharness.state.checkpoint import SQLiteCheckpointStore
from forgeharness.state.invocation_journal import SQLiteInvocationJournal
from forgeharness.tools.base import ToolContext, ToolSpec
from forgeharness.tools.dispatcher import ToolDispatcher
from forgeharness.tools.policy import PolicyDecision, PolicyDecisionType, RiskBasedPolicy
from forgeharness.tools.registry import ToolRegistry


def todo(status=TodoStatus.PENDING):
    return TodoItem(id="work", content="Complete the authorized work", status=status)


def proposal(identifier, *items):
    return ToolCall(
        id=identifier,
        name="write_todos",
        arguments=WriteTodosInput(todos=items).model_dump(mode="json"),
    )


def assistant(call):
    return Message(role=MessageRole.ASSISTANT, tool_calls=(call,))


def observation(call, content):
    return Message(
        role=MessageRole.TOOL,
        tool_call_id=call.id,
        tool_name=call.name,
        content=content,
    )


async def apply(board, call):
    output = await WriteTodosTool(board).execute(
        WriteTodosInput.model_validate(call.arguments), ToolContext(task_id="task")
    )
    fact = ToolEvidence(
        name=call.name,
        logical_call_id=call.id,
        ok=output.ok,
        content=output.content,
        metadata=output.metadata,
    )
    return (assistant(call), observation(call, output.content)), fact


@pytest.mark.parametrize("ending", ["denied", "failed", "unexecuted"])
async def test_replay_keeps_pending_work_when_completed_proposal_was_not_applied(ending):
    board = TodoBoard()
    first, accepted = await apply(board, proposal("pending", todo()))
    complete = proposal("completed", todo(TodoStatus.COMPLETED))
    messages = (*first, assistant(complete))
    facts = (accepted,)
    if ending != "unexecuted":
        content = "policy denied tool: completion is not authorized"
        if ending == "failed":
            content = "todo board rejected: current state does not allow this update"
            facts += (
                ToolEvidence(
                    name="write_todos", logical_call_id=complete.id, ok=False, content=content
                ),
            )
        messages += (observation(complete, content),)
    for recovered in (replay_board(messages), replay_board(messages, evidence=facts)):
        assert recovered.items == (todo(),)
        assert recovered.revision == 1 and recovered.snapshot().open_count == 1


async def test_replay_preserves_noop_and_empty_board_revisions():
    board, messages, facts = TodoBoard(), (), ()
    for identifier, items in (
        ("first", (todo(),)),
        ("repeat", (todo(),)),
        ("complete", (todo(TodoStatus.COMPLETED),)),
        ("clear", ()),
        ("clear-again", ()),
    ):
        pair, fact = await apply(board, proposal(identifier, *items))
        messages += pair
        facts += (fact,)
    assert [fact.metadata["revision"] for fact in facts] == [1, 1, 2, 3, 3]
    for recovered in (replay_board(messages), replay_board(messages, evidence=facts)):
        assert recovered.revision == 3 and not recovered.items


@pytest.mark.parametrize("field", ["revision", "digest", "todos", "open_count"])
async def test_structured_success_with_wrong_board_or_revision_fails_closed(field):
    pair, fact = await apply(TodoBoard(), proposal("pending", todo()))
    metadata = dict(fact.metadata)
    metadata[field] = {
        "revision": True,
        "digest": "wrong-digest",
        "todos": [],
        "open_count": 0,
    }[field]
    with pytest.raises(ValueError, match="proposal or revision"):
        replay_board(pair, evidence=(fact.model_copy(update={"metadata": metadata}),))


async def test_observation_cannot_skip_a_revision_or_lose_success_evidence():
    pair, fact = await apply(TodoBoard(), proposal("pending", todo()))
    corrupted = pair[-1].model_copy(
        update={"content": pair[-1].content.replace("revision 1", "revision 2")}
    )
    with pytest.raises(ValueError, match="proposal or revision"):
        replay_board((*pair[:-1], corrupted))
    with pytest.raises(ValueError, match="missing its committed output"):
        replay_board(pair, evidence=())
    with pytest.raises(ValueError, match="observation does not match"):
        replay_board((*pair[:-1], corrupted), evidence=(fact,))


async def test_duplicate_call_ids_and_observations_cannot_overwrite_an_accepted_pending_board():
    board = TodoBoard()
    pending, fact = await apply(board, proposal("same-id", todo()))
    complete = proposal("same-id", todo(TodoStatus.COMPLETED))
    completed, _ = await apply(board, complete)
    messages = (*pending, pending[-1], *completed)
    for recovered in (replay_board(messages), replay_board(messages, evidence=(fact, fact))):
        assert recovered.items == (todo(),) and recovered.revision == 1


@pytest.mark.parametrize("field", ["tool_call_id", "tool_name"])
async def test_observation_must_match_the_proposed_call_and_tool(field):
    board = TodoBoard()
    pending, first = await apply(board, proposal("pending", todo()))
    completed, second = await apply(board, proposal("complete", todo(TodoStatus.COMPLETED)))
    wrong = completed[-1].model_copy(update={field: "another"})
    messages = (*pending, completed[0], wrong)
    recovered = replay_board(messages)
    assert recovered.items == (todo(),) and recovered.revision == 1
    with pytest.raises(ValueError, match="no matching observation"):
        replay_board(messages, evidence=(first, second))


async def test_successful_output_without_its_proposal_cannot_silently_clear_a_pending_board():
    pair, fact = await apply(TodoBoard(), proposal("pending", todo()))
    with pytest.raises(ValueError, match="no matching observation"):
        replay_board((pair[-1],), evidence=(fact,))


async def test_compressed_observation_requires_the_full_successful_result():
    items = tuple(TodoItem(id=f"item-{index}", content="work " * 60) for index in range(20))
    pair, fact = await apply(TodoBoard(), proposal("long-plan", *items))
    compressed = ObservationCompressor(max_chars=200).compress(pair[-1].content)
    messages = (pair[0], pair[-1].model_copy(update={"content": compressed}))
    assert compressed.startswith("[compressed observation:")
    recovered = replay_board(messages, evidence=(fact,))
    assert recovered.items == items and recovered.snapshot().open_count == 20
    with pytest.raises(ValueError, match="proposal or revision"):
        replay_board(messages)


@pytest.mark.parametrize("revision", [-1, True, 0])
def test_restore_rejects_invalid_revision_without_mutating_the_board(revision):
    board = TodoBoard((todo(),))
    before = board.snapshot()
    with pytest.raises(ValueError, match="nonnegative revision"):
        board.restore((todo(TodoStatus.COMPLETED),), revision=revision)
    assert board.snapshot() == before


class SimulatedRestart(RuntimeError):
    pass


class CrashAtCheckpoint(SQLiteCheckpointStore):
    def __init__(self, path: Path, call_id: str, *, after_commit: bool) -> None:
        super().__init__(path)
        self.call_id, self.after_commit = call_id, after_commit
        self.armed = True

    def save(self, result: RunResult) -> RunResult:
        reached = (
            self.armed
            and result.messages[-1].role is MessageRole.TOOL
            and result.messages[-1].tool_call_id == self.call_id
        )
        if reached:
            self.armed = False
            if not self.after_commit:
                raise SimulatedRestart("tool finished before checkpoint persisted")
        saved = super().save(result)
        if reached:
            raise SimulatedRestart("checkpoint persisted before worker stopped")
        return saved


class DenyCompletion:
    def evaluate(self, *, task_id: str, spec: ToolSpec, call: ToolCall) -> PolicyDecision:
        del task_id, spec
        completed = any(
            item.status is TodoStatus.COMPLETED
            for item in WriteTodosInput.model_validate(call.arguments).todos
        )
        return PolicyDecision(
            type=PolicyDecisionType.DENY if completed else PolicyDecisionType.ALLOW,
            reason="completion is not authorized" if completed else "plan may be started",
        )


def runner(model, board, store, trace, *, policy=None, journal=None, tool=None):
    registry = ToolRegistry()
    if tool is not None:
        registry.register(tool)
    return AgentRuntime(
        model=model,
        registry=registry,
        dispatcher=ToolDispatcher(registry),
        policy=policy or RiskBasedPolicy(),
        trace=trace,
        checkpoint_store=store,
        invocation_journal=journal,
        todos=board,
    )


async def test_resume_after_denied_completion_keeps_pending_board_and_refuses_premature_final(
    tmp_path,
):
    pending = proposal("pending", todo())
    completed = proposal("complete", todo(TodoStatus.COMPLETED))
    store = CrashAtCheckpoint(tmp_path / "checkpoint.sqlite", completed.id, after_commit=True)
    initial = runner(
        ScriptedModel([ModelResult(action=ToolAction(call=call)) for call in (pending, completed)]),
        TodoBoard(),
        store,
        InMemoryTrace("task"),
        policy=DenyCompletion(),
    )
    with pytest.raises(SimulatedRestart):
        await initial.run(task_id="task", task="Complete authorized work", workspace=tmp_path)
    previous = store.load("task")
    assert previous.status is RunStatus.RUNNING
    assert previous.messages[-1].content.startswith("policy denied tool:")
    assert len(previous.tool_evidence) == 1
    model = ScriptedModel(
        [
            ModelResult(action=FinalAction(content="premature")),
            ModelResult(
                action=ToolAction(call=proposal("accepted-complete", todo(TodoStatus.COMPLETED)))
            ),
            ModelResult(action=FinalAction(content="verified completion")),
        ]
    )
    trace, board = InMemoryTrace("task"), TodoBoard()

    result = await runner(model, board, store, trace).resume_running(
        previous=previous, workspace=tmp_path
    )

    assert result.status is RunStatus.SUCCEEDED and result.final_output == "verified completion"
    assert board.revision == 2 and board.snapshot().open_count == 0
    assert any(
        "revision 1; 1 open" in (message.content or "") for message in model.requests[0].messages
    )
    assert sum(event.type == "todo.final_rejected" for event in trace.events) == 1


class CountingTodos(WriteTodosTool):
    executions = 0

    async def execute(self, arguments, context):
        self.executions += 1
        return await super().execute(arguments, context)


async def test_completed_cached_write_recovery_updates_board_without_tool_redispatch(tmp_path):
    pending = proposal("pending", todo())
    complete = proposal("complete", todo(TodoStatus.COMPLETED))
    database = tmp_path / "checkpoint.sqlite"
    store = CrashAtCheckpoint(database, complete.id, after_commit=False)
    journal = SQLiteInvocationJournal(database)
    initial_board = TodoBoard()
    initial_tool = CountingTodos(initial_board)
    first = runner(
        ScriptedModel([ModelResult(action=ToolAction(call=call)) for call in (pending, complete)]),
        initial_board,
        store,
        InMemoryTrace("task"),
        journal=journal,
        tool=initial_tool,
    )
    with pytest.raises(SimulatedRestart):
        await first.run(task_id="task", task="Complete authorized work", workspace=tmp_path)
    previous = store.load("task")
    assert previous.messages[-1].tool_calls == (complete,)
    assert journal.load(run_id="task", logical_call_id=complete.id).result.ok
    assert initial_tool.executions == 2
    board, trace = TodoBoard(), InMemoryTrace("task")
    replayed_tool = CountingTodos(board)
    model = ScriptedModel([ModelResult(action=FinalAction(content="recovered completion"))])

    recovered = runner(model, board, store, trace, journal=journal, tool=replayed_tool)
    result = await recovered.resume_running(previous=previous, workspace=tmp_path)

    assert result.status is RunStatus.SUCCEEDED and result.final_output == "recovered completion"
    assert replayed_tool.executions == 0
    assert board.revision == 2 and board.snapshot().open_count == 0
    assert any(
        "revision 2; 0 open" in (message.content or "") for message in model.requests[0].messages
    )
