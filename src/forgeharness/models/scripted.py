"""Deterministic model used by tests and the keyless demo."""

from __future__ import annotations

from collections import deque

from forgeharness.domain.models import ModelResult
from forgeharness.models.base import ModelRequest


class ScriptedModel:
    """Return a finite sequence of prevalidated model results."""

    def __init__(self, responses: list[ModelResult]) -> None:
        self._responses = deque(responses)
        self.requests: list[ModelRequest] = []

    async def decide(self, request: ModelRequest) -> ModelResult:
        """Record the request and return the next scripted response."""
        self.requests.append(request)
        if not self._responses:
            raise RuntimeError("scripted model has no response remaining")
        return self._responses.popleft()
