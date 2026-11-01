"""Provider-neutral capability declarations."""

from typing import Literal

from forgeharness.domain.models import FrozenModel


class ModelCapability(FrozenModel):
    """Observed OMLX model inventory used by readiness checks."""

    model_id: str
    role: Literal["chat", "embedding", "reranker"]
    available: bool
