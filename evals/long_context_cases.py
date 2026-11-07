"""Large-N audit tasks for the long-context scaling study.

The scaling study's generator draws each case's service names from forty base
names without replacement, so it stops at N = 40. This module keeps every other
part of that generator (``_service``: endpoints, distractors, changelog, bulk) and
only widens the name pool with numbered variants, so N can reach 1024.

Seeds differ from every earlier split; tests check that no material repeats.
Synthetic and author-generated, like ``evals.multi_material_cases``.
"""

from __future__ import annotations

import math
import random

from evals.multi_material_cases import _NAMES, Case, _service

SIZES = (32, 128, 512, 1024)
SEEDS = {"dev": 4401, "evaluation": 5502}
CASES_PER_SIZE = {"dev": 1, "evaluation": 4}


def _names(size: int) -> list[str]:
    """Base names first, then numbered variants (``audit-log-2``), as many as needed."""
    rounds = math.ceil(size / len(_NAMES))
    return [name if k == 0 else f"{name}-{k + 1}" for k in range(rounds) for name in _NAMES]


def generate(seed: int, split: str, size: int, number: int) -> Case:
    rng = random.Random(seed * 10_000 + size * 10 + number)
    violators = max(1, min(size - 1, round(size * rng.uniform(0.2, 0.4))))
    flagged = set(rng.sample(range(1, size + 1), k=violators))
    names = rng.sample(_names(size), k=size)
    services = tuple(
        _service(
            rng,
            index,
            names[index - 1],
            rng.choice(("R1", "R2", "R3")) if index in flagged else None,
        )
        for index in range(1, size + 1)
    )
    return Case(id=f"{split}-n{size:04d}-{number}", split=split, size=size, services=services)


def long_cases(split: str) -> tuple[Case, ...]:
    """All cases of one split, ordered by size then number."""
    if split not in SEEDS:
        raise ValueError("split must be 'dev' or 'evaluation'")
    return tuple(
        generate(SEEDS[split], split, size, number)
        for size in SIZES
        for number in range(1, CASES_PER_SIZE[split] + 1)
    )
