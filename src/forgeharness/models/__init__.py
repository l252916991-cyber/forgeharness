"""Model adapters and deterministic test doubles."""

from forgeharness.models.base import Model, ModelRequest
from forgeharness.models.openai_compatible import OpenAICompatibleConfig, OpenAICompatibleModel
from forgeharness.models.scripted import ScriptedModel

__all__ = [
    "Model",
    "ModelRequest",
    "OpenAICompatibleConfig",
    "OpenAICompatibleModel",
    "ScriptedModel",
]
