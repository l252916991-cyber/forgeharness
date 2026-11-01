"""Deterministic prompt-cache planning for the prepared model request.

A harness re-sends the same long prefix on every step: the system instructions, the
tool schemas, the policy description, the citation contract. On a fifty-step task
that is the dominant input cost, and providers that support prefix caching charge a
fraction of it -- *if* the request marks the prefix correctly and reproduces it byte
for byte.

Three things make this a module rather than a few lines inside one adapter:

* **The prefix has to be stable.** Tool order comes from ``ToolRegistry.specs()``,
  which sorts by name; anything that reorders or conditionally drops tools between
  steps silently invalidates every cached token, and the cost regression is invisible
  without a measurement. :func:`plan_cache` reports the prefix so a test can assert
  on it.
* **Providers disagree.** Some cache a stable prefix automatically and reject unknown
  fields (OpenAI-compatible servers); some require an explicit breakpoint marker
  (Anthropic). :class:`CacheMode` names the three behaviours instead of guessing from
  a base URL.
* **Markers must be admitted and recorded.** The semantic request stays unchanged;
  its explicit-cache prepared format validates the marked wire body before sizing,
  tracing and dispatch. No transport rewrite happens after admission.

The plan is a pure function of the request, so a replay recomputes the identical
plan and the trace stays honest.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from enum import StrEnum
from typing import Any, Literal

from pydantic import Field

from forgeharness.context.compiler import CharacterTokenEstimator, TokenEstimator
from forgeharness.domain.models import FrozenModel, Message, MessageRole
from forgeharness.models.base import ModelRequest
from forgeharness.models.prepared import PreparedModelCall, canonical_json

#: Providers that cache a stable prefix without being told (and reject unknown
#: request fields) need no directive at all; the value only drives reporting.
_DEFAULT_CACHEABLE_FRACTION_MIN = 0.05


class CacheMode(StrEnum):
    """How a provider wants a cacheable prefix expressed."""

    #: The provider has no prefix cache, or the deployment disabled it.
    OFF = "off"
    #: The provider caches a stable prefix automatically (OpenAI-style).
    PREFIX = "prefix"
    #: The provider needs an explicit breakpoint marker (Anthropic-style).
    EXPLICIT = "explicit"


class CacheDirective(FrozenModel):
    """One ``cache_control`` marker and where it goes."""

    target: Literal["system", "tools"]
    message_index: int | None = Field(default=None, ge=0)
    ttl: Literal["ephemeral", "1h"] = "ephemeral"


class CachePlan(FrozenModel):
    """What can be cached in one request, and why."""

    mode: CacheMode
    cacheable: bool
    #: Leading messages that form the stable prefix (system instructions and any
    #: pinned state that has not changed since the last turn).
    prefix_messages: int = Field(ge=0)
    prefix_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    directives: tuple[CacheDirective, ...] = ()

    @property
    def breakpoints(self) -> int:
        """How many markers the wire body will carry."""
        return len(self.directives)

    @property
    def cached_fraction(self) -> float:
        """Share of input tokens the provider may bill at the cached rate."""
        if not self.total_tokens:
            return 0.0
        return self.prefix_tokens / self.total_tokens

    @property
    def worth_marking(self) -> bool:
        """Whether the prefix is large enough that a marker is not pure overhead."""
        return self.cacheable and self.cached_fraction >= _DEFAULT_CACHEABLE_FRACTION_MIN


def estimate_message_tokens(message: Message, estimator: TokenEstimator | None = None) -> int:
    """Estimate one message the same way the context assembler does."""
    chosen = estimator or CharacterTokenEstimator()
    parts = [message.content or "", message.tool_name or ""]
    for call in message.tool_calls:
        parts.append(call.name)
        parts.append(json.dumps(call.arguments, ensure_ascii=False, sort_keys=True))
    return chosen.estimate("\n".join(parts))


def plan_cache(
    request: ModelRequest,
    *,
    mode: CacheMode = CacheMode.OFF,
    estimator: TokenEstimator | None = None,
) -> CachePlan:
    """Compute the cacheable prefix of ``request``.

    The stable prefix is the run of leading system messages. It holds the compiled
    instructions, and the runtime orders messages so that anything mutable (tool
    results, new turns) follows it, which is what makes the prefix survive a turn.
    """
    chosen = estimator or CharacterTokenEstimator()
    prefix = 0
    for message in request.messages:
        if message.role is not MessageRole.SYSTEM:
            break
        prefix += 1
    prefix_tokens = sum(estimate_message_tokens(m, chosen) for m in request.messages[:prefix])
    total_tokens = sum(estimate_message_tokens(m, chosen) for m in request.messages)
    total_tokens += sum(chosen.estimate(spec.description) for spec in request.tools)
    if mode is CacheMode.OFF:
        return CachePlan(
            mode=mode,
            cacheable=False,
            prefix_messages=prefix,
            prefix_tokens=prefix_tokens,
            total_tokens=total_tokens,
        )
    directives: list[CacheDirective] = []
    if mode is CacheMode.EXPLICIT:
        # Anthropic caches everything up to and including a marker, so two markers
        # (end of tools, end of the leading system block) cover the whole prefix.
        if request.tools:
            directives.append(CacheDirective(target="tools"))
        if prefix:
            directives.append(CacheDirective(target="system", message_index=prefix - 1))
    return CachePlan(
        mode=mode,
        cacheable=True,
        prefix_messages=prefix,
        prefix_tokens=prefix_tokens,
        total_tokens=total_tokens,
        directives=tuple(directives),
    )


def render_payload(prepared: PreparedModelCall, plan: CachePlan) -> str:
    """Render a candidate body; adapters must freeze it before budget admission.

    The source snapshot is immutable. This helper is not a post-admission
    transport hook: its result needs the explicit-cache prepared format.
    """
    if not plan.directives:
        return prepared.payload_json
    return canonical_json(apply_cache_plan(prepared.payload, plan))


def apply_cache_plan(payload: dict[str, Any], plan: CachePlan) -> dict[str, Any]:
    """Copy and mark exactly the declared tool/system breakpoints."""
    body: dict[str, Any] = json.loads(canonical_json(payload))
    for directive in plan.directives:
        if directive.target == "tools":
            tools = body.get("tools")
            if not tools:
                continue
            tools[-1]["cache_control"] = {"type": directive.ttl}
            continue
        index = directive.message_index
        if index is None or index >= len(body.get("messages", [])):
            continue
        message = body["messages"][index]
        # Anthropic accepts a marker on a message only through a content block, so
        # the text form is upgraded rather than annotated in place.
        message["content"] = [
            {
                "type": "text",
                "text": message.get("content") or "",
                "cache_control": {"type": directive.ttl},
            }
        ]
    return body


def cache_report(plan: CachePlan) -> dict[str, object]:
    """Trace-ready description of a plan; stable keys, no floats for equality."""
    return {
        "mode": plan.mode.value,
        "cacheable": plan.cacheable,
        "prefix_messages": plan.prefix_messages,
        "prefix_tokens": plan.prefix_tokens,
        "total_tokens": plan.total_tokens,
        "breakpoints": plan.breakpoints,
        "worth_marking": plan.worth_marking,
        "cached_fraction_milli": round(1000 * plan.cached_fraction),
    }


#: Signature a model implements to expose its plan to the trace.
CachePlanner = Callable[[ModelRequest], CachePlan]


def planner_for(mode: CacheMode, estimator: TokenEstimator | None = None) -> CachePlanner:
    """Bind a mode into the callable shape the runtime probes for."""

    def planner(request: ModelRequest) -> CachePlan:
        return plan_cache(request, mode=mode, estimator=estimator)

    return planner


__all__ = [
    "CacheDirective",
    "CacheMode",
    "CachePlan",
    "CachePlanner",
    "cache_report",
    "estimate_message_tokens",
    "plan_cache",
    "planner_for",
    "render_payload",
]
