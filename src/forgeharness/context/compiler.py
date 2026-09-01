"""Deterministic, provenance-preserving context selection."""

from __future__ import annotations

from typing import Protocol

from pydantic import Field

from forgeharness.domain.models import FrozenModel


class TokenEstimator(Protocol):
    """Estimate model tokens without binding the core to one tokenizer."""

    def estimate(self, text: str) -> int:
        """Return a deterministic non-negative estimate."""
        ...


class CharacterTokenEstimator:
    """Conservative tokenizer-independent estimate for keyless operation."""

    def estimate(self, text: str) -> int:
        """Estimate one token per four characters, with a minimum for non-empty text."""
        return 0 if not text else max(1, (len(text) + 3) // 4)


class ContextItem(FrozenModel):
    """One candidate context fragment with source provenance and relevance."""

    id: str = Field(min_length=1, max_length=200)
    source: str = Field(min_length=1, max_length=1000)
    content: str
    score: float = 0.0
    required: bool = False


class ContextDecision(FrozenModel):
    """Explain why a candidate was included or excluded."""

    item_id: str
    source: str
    estimated_tokens: int = Field(ge=0)
    included: bool
    reason: str


class CompiledContext(FrozenModel):
    """Selected context text plus an auditable selection manifest."""

    text: str
    estimated_tokens: int = Field(ge=0)
    decisions: tuple[ContextDecision, ...]


class ContextBudgetError(ValueError):
    """Required context cannot fit within the configured budget."""


class ContextCompiler:
    """Select complete fragments by requirement, relevance, and stable identity."""

    def __init__(self, *, max_tokens: int, estimator: TokenEstimator | None = None) -> None:
        if max_tokens < 1:
            raise ValueError("max_tokens must be positive")
        self._max_tokens = max_tokens
        self._estimator = estimator or CharacterTokenEstimator()

    def compile(self, items: list[ContextItem]) -> CompiledContext:
        """Fit required items first, then optional items by descending relevance."""
        if len({item.id for item in items}) != len(items):
            raise ValueError("context item ids must be unique")
        ordered = sorted(items, key=lambda item: (not item.required, -item.score, item.id))
        used = 0
        included: list[ContextItem] = []
        decisions: list[ContextDecision] = []
        for item in ordered:
            tokens = self._estimator.estimate(item.content)
            fits = used + tokens <= self._max_tokens
            if item.required and not fits:
                raise ContextBudgetError(
                    f"required context item {item.id!r} needs {tokens} tokens with {used} used "
                    f"of {self._max_tokens}"
                )
            if fits:
                included.append(item)
                used += tokens
                reason = "required" if item.required else "selected by relevance"
            else:
                reason = "token budget exhausted"
            decisions.append(
                ContextDecision(
                    item_id=item.id,
                    source=item.source,
                    estimated_tokens=tokens,
                    included=fits,
                    reason=reason,
                )
            )
        text = "\n\n".join(
            f"<context source={item.source!r}>\n{item.content}\n</context>" for item in included
        )
        return CompiledContext(text=text, estimated_tokens=used, decisions=tuple(decisions))
