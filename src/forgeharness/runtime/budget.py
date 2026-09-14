"""Deterministic execution budgets owned by the Harness."""

from pydantic import Field

from forgeharness.domain.models import FrozenModel


class RunBudget(FrozenModel):
    """Hard resource limits for one runtime invocation."""

    max_steps: int = Field(default=12, ge=1, le=1000)
    max_tool_calls: int = Field(default=20, ge=0, le=5000)
    max_input_tokens: int = Field(default=100_000, ge=0)
    max_output_tokens: int = Field(default=20_000, ge=0)
    # Ceiling for a single assembled model request, distinct from the
    # cumulative input-token cap above. History is compacted to fit this.
    max_context_tokens: int = Field(default=32_000, ge=1)
