"""Cross-material synthesis tasks: the audit rules plus one-hop dependency risk.

Every earlier collaboration study used independent materials, whose answer is the
union of per-material judgements, so program-level splitting was always enough. Here
a service's label can depend on another material: a service that breaks none of
R1-R3 is *at risk* when a service it currently calls (``calls_upstream``) breaks one.
Splitting the materials hides some of those calls, so the task tests whether a team
that combines results can do what a plain split cannot.

Materials are the audit study's services (``evals.audit_tasks``) with two changes:

- the rule-irrelevant ``dependencies`` line (names) becomes ``calls_upstream``
  (service IDs), so there is exactly one place that defines a call;
- some services carry a changelog entry about a removed call to a violating service,
  a near miss: history is not a current call.

The expected labels live only in ``SynthesisCase`` and are read by the grader, never
by a prompt. Synthetic and author-generated: a probe, not a business benchmark.
This module is separate from ``evals.audit_tasks`` so the sealed studies that pin
that file's hash are untouched.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from evals.audit_tasks import _NAMES, RULES, Case, Service, _service

SIZES = (16, 32)
GROUP = 8
_SEEDS = {"dev": 6601, "evaluation": 7702}
_CASES_PER_SIZE = {"dev": 1, "evaluation": 6}
LABELS = ("violation", "at-risk", "compliant")

_AUDIT_TASK = "Identify every service that currently violates at least one rule."
SYNTHESIS_RULES = RULES.replace(
    _AUDIT_TASK,
    "Each service lists the service IDs it currently calls under calls_upstream.\n"
    "R4 Dependency risk: a service that violates none of R1-R3 is at risk if any service "
    "in its calls_upstream violates R1, R2 or R3. A service that violates R1-R3 is "
    "violating, not at risk. A call mentioned only in the changelog is not a current call.\n"
    "Label every service as violation, at-risk or compliant.",
)
if SYNTHESIS_RULES == RULES:  # pragma: no cover - guards a silent wording change
    raise RuntimeError("audit rule wording changed; synthesis rules not derived")


@dataclass(frozen=True)
class SynthesisCase(Case):
    """An audit case whose services also call each other."""

    upstream: tuple[tuple[str, tuple[str, ...]], ...] = ()

    @property
    def calls(self) -> dict[str, tuple[str, ...]]:
        return dict(self.upstream)

    @property
    def labels(self) -> dict[str, str]:
        """Expected label per service: the grader's answer key."""
        direct = set(self.violations)
        calls = self.calls
        return {
            service.id: (
                "violation"
                if service.id in direct
                else "at-risk"
                if direct & set(calls[service.id])
                else "compliant"
            )
            for service in self.services
        }

    @property
    def at_risk(self) -> tuple[str, ...]:
        return tuple(key for key, label in self.labels.items() if label == "at-risk")

    def groups(self, size: int = GROUP) -> list[tuple[str, ...]]:
        """Contiguous service-ID groups, as the split and team arms see them."""
        ids = [service.id for service in self.services]
        return [tuple(ids[start : start + size]) for start in range(0, len(ids), size)]

    @property
    def hidden_at_risk(self) -> tuple[str, ...]:
        """At-risk services whose every violating upstream lies outside their group.

        A split that sees one group at a time cannot label these correctly; their
        share is the manipulation check that this task needs synthesis at all.
        """
        direct = set(self.violations)
        group_of = {key: number for number, group in enumerate(self.groups()) for key in group}
        return tuple(
            key
            for key in self.at_risk
            if all(group_of[up] != group_of[key] for up in self.calls[key] if up in direct)
        )


def _rewire(service: Service, upstream: tuple[str, ...], removed: str | None) -> Service:
    lines = []
    for line in service.text.split("\n"):
        if line.startswith("dependencies: "):
            line = "calls_upstream: [" + ", ".join(upstream) + "]"
        lines.append(line)
        if line == "changelog:" and removed is not None:
            lines.append(f"  - 2026-11-02 removed synchronous call to {removed}")
    return Service(id=service.id, text="\n".join(lines), violation=service.violation)


def generate(seed: int, split: str, size: int, number: int) -> SynthesisCase:
    rng = random.Random(seed * 10_000 + size * 10 + number)
    violators = max(2, min(size - 2, round(size * rng.uniform(0.15, 0.3))))
    flagged = set(rng.sample(range(1, size + 1), k=violators))
    names = rng.sample(_NAMES, k=size)
    base = [
        _service(
            rng,
            index,
            names[index - 1],
            rng.choice(("R1", "R2", "R3")) if index in flagged else None,
        )
        for index in range(1, size + 1)
    ]
    ids = [service.id for service in base]
    direct = {service.id for service in base if service.violation is not None}
    wiring = random.Random(seed * 10_000 + size * 10 + number + 7)
    upstream: list[tuple[str, tuple[str, ...]]] = []
    services: list[Service] = []
    for service in base:
        others = [key for key in ids if key != service.id]
        calls = tuple(sorted(wiring.sample(others, k=wiring.choice((0, 1, 1, 2, 2, 3)))))
        stale = [key for key in sorted(direct) if key not in calls and key != service.id]
        removed = wiring.choice(stale) if stale and wiring.random() < 0.3 else None
        upstream.append((service.id, calls))
        services.append(_rewire(service, calls, removed))
    return SynthesisCase(
        id=f"syn-{split}-n{size:02d}-{number}",
        split=split,
        size=size,
        services=tuple(services),
        upstream=tuple(upstream),
    )


def synthesis_cases(split: str) -> tuple[SynthesisCase, ...]:
    """The synthesis study's split, ordered by size then number."""
    if split not in _SEEDS:
        raise ValueError("split must be 'dev' or 'evaluation'")
    return tuple(
        generate(_SEEDS[split], split, size, number)
        for size in SIZES
        for number in range(1, _CASES_PER_SIZE[split] + 1)
    )
