"""Tests for budget selection, conversation assembly, and observation compression."""

import pytest

from forgeharness.context.assembly import ContextAssembler
from forgeharness.context.compiler import ContextBudgetError, ContextCompiler, ContextItem
from forgeharness.context.compression import ObservationCompressor
from forgeharness.domain.models import Message, MessageRole, ToolCall


def test_compiler_prioritizes_required_then_relevant_context() -> None:
    compiler = ContextCompiler(max_tokens=6)
    result = compiler.compile(
        [
            ContextItem(id="low", source="low.py", content="12345678", score=0.1),
            ContextItem(id="required", source="issue", content="1234", required=True),
            ContextItem(id="high", source="high.py", content="12345678", score=0.9),
            ContextItem(id="medium", source="mid.py", content="12345678", score=0.5),
        ]
    )

    assert result.estimated_tokens == 5
    assert "issue" in result.text
    assert "high.py" in result.text
    assert "mid.py" in result.text
    excluded = [decision.item_id for decision in result.decisions if not decision.included]
    assert excluded == ["low"]


def test_compiler_fails_when_required_context_does_not_fit() -> None:
    with pytest.raises(ContextBudgetError, match="required context item"):
        ContextCompiler(max_tokens=1).compile(
            [ContextItem(id="issue", source="issue", content="too long", required=True)]
        )


def test_compiler_rejects_duplicate_ids_and_invalid_budget() -> None:
    with pytest.raises(ValueError, match="must be positive"):
        ContextCompiler(max_tokens=0)
    item = ContextItem(id="same", source="source", content="content")
    with pytest.raises(ValueError, match="must be unique"):
        ContextCompiler(max_tokens=10).compile([item, item])


def test_compressor_preserves_errors_and_locations() -> None:
    content = "\n".join(
        ["start", *[f"noise {index}" for index in range(100)], "FAILED src/app.py:42", "end"]
    )
    compressed = ObservationCompressor(max_chars=240).compress(content)

    assert compressed.startswith("[compressed observation:")
    assert "FAILED src/app.py:42" in compressed
    assert len(compressed) <= 240


def test_compressor_returns_short_content_and_validates_limit() -> None:
    assert ObservationCompressor().compress("short") == "short"
    with pytest.raises(ValueError, match="at least 200"):
        ObservationCompressor(max_chars=199)


def _turn(index: int, size: int) -> list[Message]:
    """Build one assistant tool-call turn and its tool result."""
    return [
        Message(role=MessageRole.ASSISTANT, tool_calls=(ToolCall(id=f"c{index}", name="echo"),)),
        Message(
            role=MessageRole.TOOL,
            content="x" * size,
            tool_call_id=f"c{index}",
            tool_name="echo",
        ),
    ]


def test_assembler_keeps_a_fitting_conversation_verbatim() -> None:
    messages = [
        Message(role=MessageRole.SYSTEM, content="instructions"),
        Message(role=MessageRole.USER, content="task"),
        *_turn(1, 8),
    ]

    assembled = ContextAssembler(max_tokens=1_000).assemble(messages)

    assert assembled.messages == tuple(messages)
    assert assembled.compacted is False
    assert assembled.dropped_messages == 0
    assert all(decision.included for decision in assembled.decisions)


def test_assembler_compacts_old_turns_and_preserves_required_context() -> None:
    messages: list[Message] = [
        Message(role=MessageRole.SYSTEM, content="instructions"),
        Message(role=MessageRole.USER, content="task"),
    ]
    for index in range(6):
        messages.extend(_turn(index, 400))

    assembled = ContextAssembler(max_tokens=300, summary_chars=60).assemble(messages)

    assert assembled.compacted is True
    assert assembled.dropped_messages > 0
    assert assembled.estimated_tokens <= 300
    assert assembled.messages[0] == messages[0]
    assert assembled.messages[-1] == messages[-1]
    summaries = [
        message
        for message in assembled.messages
        if message.content and message.content.startswith("[compacted context:")
    ]
    assert len(summaries) == 1
    dropped = [decision for decision in assembled.decisions if not decision.included]
    assert all(decision.reason == "compacted into summary" for decision in dropped)
    assert dropped


def test_assembler_never_splits_a_tool_result_from_its_call() -> None:
    messages: list[Message] = [Message(role=MessageRole.USER, content="task")]
    for index in range(5):
        messages.extend(_turn(index, 400))

    assembled = ContextAssembler(max_tokens=500, summary_chars=40).assemble(messages)

    assistant_calls: set[str] = set()
    for message in assembled.messages:
        if message.role == MessageRole.ASSISTANT:
            assistant_calls.update(call.id for call in message.tool_calls)
        if message.role == MessageRole.TOOL:
            assert message.tool_call_id in assistant_calls
    assert assembled.compacted is True
    assert assembled.estimated_tokens <= 500


def test_assembler_is_deterministic() -> None:
    messages: list[Message] = [Message(role=MessageRole.USER, content="task")]
    for index in range(5):
        messages.extend(_turn(index, 300))
    assembler = ContextAssembler(max_tokens=500, summary_chars=40)

    first = assembler.assemble(messages)
    second = assembler.assemble(messages)

    assert first == second


def test_assembler_raises_when_required_context_cannot_fit() -> None:
    messages = [
        Message(role=MessageRole.SYSTEM, content="s" * 4_000),
        Message(role=MessageRole.USER, content="task"),
    ]

    with pytest.raises(ContextBudgetError, match="required"):
        ContextAssembler(max_tokens=10).assemble(messages)


def test_assembler_raises_when_system_only_context_cannot_fit() -> None:
    with pytest.raises(ContextBudgetError, match="required system context"):
        ContextAssembler(max_tokens=10).assemble(
            [Message(role=MessageRole.SYSTEM, content="s" * 4_000)]
        )


def test_assembler_keeps_system_only_context_when_it_fits() -> None:
    messages = [
        Message(role=MessageRole.SYSTEM, content="instructions"),
        Message(role=MessageRole.SYSTEM, content="skills"),
    ]

    assembled = ContextAssembler(max_tokens=100).assemble(messages)

    assert assembled.messages == tuple(messages)
    assert assembled.compacted is False
    assert [decision.reason for decision in assembled.decisions] == [
        "required system",
        "required system",
    ]


def test_assembler_handles_an_empty_conversation() -> None:
    assembled = ContextAssembler(max_tokens=10).assemble([])

    assert assembled.messages == ()
    assert assembled.estimated_tokens == 0
    assert assembled.decisions == ()


class _UnitCostEstimator:
    """Charge one token per non-empty message part, independent of length."""

    def estimate(self, text: str) -> int:
        return 1 if text else 0


def test_assembler_raises_when_no_room_is_left_for_a_summary() -> None:
    # Required context exactly fills the window, leaving no room to summarize the
    # older turns that must be compacted away.
    messages: list[Message] = [
        Message(role=MessageRole.SYSTEM, content="instructions"),
        Message(role=MessageRole.USER, content="task"),
        *_turn(0, 5),
        *_turn(1, 5),
    ]

    with pytest.raises(ContextBudgetError, match="no token budget left"):
        ContextAssembler(max_tokens=3, estimator=_UnitCostEstimator()).assemble(messages)


def test_assembler_validates_configuration() -> None:
    with pytest.raises(ValueError, match="max_tokens must be positive"):
        ContextAssembler(max_tokens=0)
    with pytest.raises(ValueError, match="min_recent_units must be positive"):
        ContextAssembler(max_tokens=10, min_recent_units=0)
    with pytest.raises(ValueError, match="summary_chars must be at least"):
        ContextAssembler(max_tokens=10, summary_chars=1)
