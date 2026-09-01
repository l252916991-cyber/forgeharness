"""Tests for budget selection and observation compression."""

import pytest

from forgeharness.context.compiler import ContextBudgetError, ContextCompiler, ContextItem
from forgeharness.context.compression import ObservationCompressor


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
