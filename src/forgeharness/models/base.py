"""Provider-neutral model protocol."""

from __future__ import annotations

from typing import Protocol

from forgeharness.domain.models import FrozenModel, Message, ModelResult
from forgeharness.tools.base import ToolSpec


class ModelRequest(FrozenModel):
    """Validated inputs available to a model for one agent step."""

    task_id: str
    messages: tuple[Message, ...]
    tools: tuple[ToolSpec, ...]


class Model(Protocol):
    """Interface implemented by real providers and deterministic doubles."""

    async def decide(self, request: ModelRequest) -> ModelResult:
        """Return one validated action for the current run state."""
        ...
