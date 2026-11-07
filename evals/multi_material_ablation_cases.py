"""Fresh N = 32 audit tasks for the collaboration ablation.

The scaling study's evaluation split is spent: its N = 32 failures suggested the
hypotheses this ablation tests, so testing them there would grade the explanation
on the data that produced it. These tasks come from the same generator under a new
seed and share no material with the scaling study's dev or evaluation splits.

Synthetic and author-generated, like ``evals.multi_material_cases``.
"""

from __future__ import annotations

import random

from evals.multi_material_cases import _NAMES, Case, _service

ABLATION_SEED = 3303
ABLATION_SIZE = 32
ABLATION_CASES = 12


def generate(seed: int, split: str, size: int, number: int) -> Case:
    """The scaling study's case generator with an explicit seed.

    ``generate(1101, "dev", 32, 1)`` reproduces the scaling study's dev N = 32 case;
    a test holds the two in step.
    """
    rng = random.Random(seed * 1000 + size * 10 + number)
    violators = max(1, min(size - 1, round(size * rng.uniform(0.2, 0.4))))
    flagged = set(rng.sample(range(1, size + 1), k=violators))
    names = rng.sample(_NAMES, k=size)
    services = tuple(
        _service(
            rng,
            index,
            names[index - 1],
            rng.choice(("R1", "R2", "R3")) if index in flagged else None,
        )
        for index in range(1, size + 1)
    )
    return Case(id=f"{split}-n{size:02d}-{number}", split=split, size=size, services=services)


def ablation_cases() -> tuple[Case, ...]:
    """The twelve ablation tasks, in order."""
    return tuple(
        generate(ABLATION_SEED, "ablation", ABLATION_SIZE, number)
        for number in range(1, ABLATION_CASES + 1)
    )
