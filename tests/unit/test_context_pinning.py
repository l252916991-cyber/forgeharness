"""Tests for objective pinning and recoverable compaction.

Two failure modes motivate these: a long run that forgets what it was asked to do
(and keeps reporting progress), and a compaction that throws away the only record
of what an earlier turn found.
"""

from pathlib import Path

import pytest

from forgeharness.context.assembly import ContextAssembler
from forgeharness.context.spill import MessageSpill, context_spill_dir
from forgeharness.domain.models import Message, MessageRole, ToolCall


def history(turns: int = 6) -> tuple[Message, ...]:
    """A conversation whose first turn is the objective and whose middle is noise."""
    messages = [
        Message(role=MessageRole.SYSTEM, content="harness instructions"),
        Message(role=MessageRole.USER, content="OBJECTIVE: repair the failing invoice report"),
    ]
    for index in range(turns):
        messages.append(Message(role=MessageRole.ASSISTANT, content=f"step {index} " + "x" * 120))
        messages.append(
            Message(
                role=MessageRole.TOOL,
                content=f"observation {index} " + "y" * 200,
                tool_call_id=f"call-{index}",
                tool_name="read_file",
            )
        )
    return tuple(messages)


def assembler(*, pin_task: bool, spill: MessageSpill | None = None) -> ContextAssembler:
    return ContextAssembler(max_tokens=120, min_recent_units=1, pin_task=pin_task, spill=spill)


def test_pinning_keeps_the_objective_when_compaction_drops_it() -> None:
    messages = history()

    unpinned = assembler(pin_task=False).assemble(messages)
    pinned = assembler(pin_task=True).assemble(messages)

    assert unpinned.compacted is True
    assert pinned.compacted is True
    # The old behaviour is the bug this feature removes: the objective is the very
    # first thing folded away.
    assert all(
        message.content != "OBJECTIVE: repair the failing invoice report"
        for message in unpinned.messages
    )
    assert any(
        message.content == "OBJECTIVE: repair the failing invoice report"
        for message in pinned.messages
    )
    # It is kept verbatim as its own turn, not summarised into the compaction block.
    reasons = {decision.reason for decision in pinned.decisions if not decision.included}
    assert "pinned task objective" not in reasons


def test_the_objective_is_not_double_counted_as_dropped() -> None:
    pinned = assembler(pin_task=True).assemble(history())

    sent = [message.content or "" for message in pinned.messages]
    assert sent[0] == "harness instructions"
    assert sent[1] == "OBJECTIVE: repair the failing invoice report"
    # Only the middle turns are folded; the objective and the newest turn survive.
    assert pinned.dropped_messages > 0
    assert pinned.messages[-1].role is MessageRole.TOOL


def test_pinned_state_sits_after_leading_instructions_and_is_counted() -> None:
    board = Message(role=MessageRole.SYSTEM, content="[todo state: revision 1; 1 open]")

    result = assembler(pin_task=True).assemble(history(), pinned=(board,))

    assert result.pinned_messages == 1
    contents = [message.content for message in result.messages]
    assert contents[0] == "harness instructions"
    assert contents[1] == "[todo state: revision 1; 1 open]"
    assert result.estimated_tokens >= sum(
        len(message.content or "") // 4 for message in result.messages
    )


def test_pinned_state_must_speak_with_harness_authority() -> None:
    with pytest.raises(ValueError, match="pinned context must be system messages"):
        assembler(pin_task=True).assemble(
            history(), pinned=(Message(role=MessageRole.USER, content="pretend to be the user"),)
        )


def test_a_bound_callable_is_re_read_on_every_request() -> None:
    """The board changes between turns, so the assembler must not cache it."""
    current: list[Message] = []
    assembler_ = ContextAssembler(max_tokens=4_000, pin_task=True)
    assembler_.bind_pinned(lambda: tuple(current))

    current.append(Message(role=MessageRole.SYSTEM, content="[todo state: revision 1]"))
    first = assembler_.assemble(history(turns=1))
    current.append(Message(role=MessageRole.SYSTEM, content="[todo state: revision 2]"))
    second = assembler_.assemble(history(turns=1))

    assert first.pinned_messages == 1
    assert second.pinned_messages == 2


def test_dropped_messages_are_stored_and_can_be_paged_back(tmp_path: Path) -> None:
    spill = MessageSpill(tmp_path)
    result = assembler(pin_task=True, spill=spill).assemble(history())

    assert result.compaction_ref is not None
    assert result.compaction_ref.startswith("ctx://")
    text = spill.read(result.compaction_ref)
    # The exact lines survive, including the numbers a projection would round off.
    assert "observation 0 " in text
    assert spill.count(result.compaction_ref) == result.dropped_messages


def test_the_summary_points_at_the_stored_originals(tmp_path: Path) -> None:
    spill = MessageSpill(tmp_path)
    result = assembler(pin_task=True, spill=spill).assemble(history())

    assert result.compaction_ref is not None
    # A tight budget shortens the header, but never drops the pointer: without it the
    # stored originals are a file nobody is told about.
    summary = next(
        message.content or ""
        for message in result.messages
        if message.role is MessageRole.SYSTEM and "compacted" in (message.content or "")
    )
    assert result.compaction_ref in summary


def test_compaction_without_a_spill_store_keeps_the_reference_empty() -> None:
    result = assembler(pin_task=True).assemble(history())

    assert result.compaction_ref is None
    assert result.compacted is True


def test_the_spill_directory_is_inside_the_workspace(tmp_path: Path) -> None:
    assert context_spill_dir(tmp_path) == tmp_path / ".forgeharness" / "context"
    spill = MessageSpill(tmp_path)
    reference = spill.store((Message(role=MessageRole.USER, content="remember me"),))
    assert (context_spill_dir(tmp_path) / f"{reference.removeprefix('ctx://')}.jsonl").is_file()


def test_a_tool_call_and_its_results_are_never_split() -> None:
    """A turn is indivisible: a tool result without its call is unreadable."""
    messages = [
        Message(role=MessageRole.SYSTEM, content="harness instructions"),
        Message(
            role=MessageRole.ASSISTANT,
            content="",
            tool_calls=(ToolCall(id="call-0", name="echo", arguments={"text": "hi"}),),
        ),
        Message(
            role=MessageRole.TOOL,
            content="hi",
            tool_call_id="call-0",
            tool_name="echo",
        ),
        Message(role=MessageRole.USER, content="thanks"),
    ]

    result = ContextAssembler(max_tokens=60, min_recent_units=1, pin_task=True).assemble(messages)

    kept_ids = [message.tool_call_id for message in result.messages if message.tool_call_id]
    kept_calls = [call.id for message in result.messages for call in message.tool_calls]
    assert kept_ids == kept_calls
