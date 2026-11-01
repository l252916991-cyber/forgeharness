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
    """Tokenizer-independent heuristic for keyless operation, not a token ceiling."""

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
    """Selection reason and isolated rendered-fragment estimate, including its separator."""

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
        """Fit complete rendered fragments, including provenance and separators.

        The total is estimated from the complete outgoing text; independent
        fragment estimates need not add up for an injected tokenizer. This is
        the material budget, not a bound on a provider's complete model request.
        """
        if len({item.id for item in items}) != len(items):
            raise ValueError("context item ids must be unique")
        ordered = sorted(items, key=lambda item: (not item.required, -item.score, item.id))
        used = 0
        text = ""
        decisions: list[ContextDecision] = []
        for item in ordered:
            fragment = f"<context source={item.source!r}>\n{item.content}\n</context>"
            addition = ("\n\n" if text else "") + fragment
            tokens = self._estimator.estimate(addition)
            candidate_text = text + addition
            candidate_tokens = self._estimator.estimate(candidate_text)
            fits = candidate_tokens <= self._max_tokens
            if item.required and not fits:
                raise ContextBudgetError(
                    f"required context item {item.id!r} would need {candidate_tokens} tokens "
                    f"with limit {self._max_tokens}"
                )
            if fits:
                text = candidate_text
                used = candidate_tokens
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
        return CompiledContext(text=text, estimated_tokens=used, decisions=tuple(decisions))
