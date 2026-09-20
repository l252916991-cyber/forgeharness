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
    # How many model-protocol violations may be fed back for correction before the
    # run fails safely. Bounded because a model that keeps violating the protocol
    # would otherwise loop: recovery is a courtesy, not an unlimited allowance.
    # Counts *executor* violations only; the planner has its own small budget
    # because the two live in different lifecycles.
    max_protocol_violations: int = Field(default=3, ge=0, le=20)
    # Physical attempts allowed per logical tool call, including the first. Retry
    # only happens where it is provably safe (see `runtime/retry.py`); this is the
    # ceiling, not a promise.
    max_attempts_per_call: int = Field(default=3, ge=1, le=10)
    # Deterministic exponential backoff before attempt n>1: initial * multiplier for
    # each further attempt, capped at max. No jitter, so timing is reproducible.
    initial_backoff_ms: int = Field(default=100, ge=0, le=60_000)
    backoff_multiplier: float = Field(default=2.0, ge=1.0, le=10.0)
    max_backoff_ms: int = Field(default=1000, ge=0, le=600_000)
    # Soft run deadline: an **admission** ceiling, not a hard wall-clock bound.
    #
    # Checked between steps, and deliberately never enforced by cancelling an
    # in-flight tool — aborting a side effect mid-write would leave exactly the
    # ambiguous state the invocation journal exists to prevent. So it stops new work
    # and does not preempt a tool already executing. A run can therefore finish later
    # than `max_wall_seconds` by up to one tool's own timeout. That overshoot is the
    # accepted price of never leaving an unknown side effect; do not describe this as
    # a guarantee that a run cannot exceed the ceiling.
    #
    # None means unbounded (the default; both evaluation profiles leave it unset).
    max_wall_seconds: float | None = Field(default=None, gt=0)
    # Fallback ceiling for a tool that is not in the registry (an unknown-tool call
    # never reaches execution, so this only keeps the trace field well-defined).
    tool_timeout_seconds: float = Field(default=30.0, gt=0)
    # Planner correction attempts. Deliberately smaller than the executor budget:
    # one attempt plus a single correction already proves the capability, and a
    # broken planner must not become a high-cost loop.
    max_planner_retries: int = Field(default=1, ge=0, le=10)
