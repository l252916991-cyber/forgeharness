"""Model adapters and deterministic test doubles."""

from forgeharness.models.base import Model, ModelRequest
from forgeharness.models.scripted import ScriptedModel

__all__ = [
    "Model",
    "ModelRequest",
    "OpenAICompatibleConfig",
    "OpenAICompatibleModel",
    "ScriptedModel",
]


def __getattr__(name: str) -> object:
    """Load HTTP model adapters only when explicitly requested."""
    if name in {"OpenAICompatibleConfig", "OpenAICompatibleModel"}:
        from importlib import import_module

        return getattr(import_module("forgeharness.models.openai_compatible"), name)
    raise AttributeError(name)
