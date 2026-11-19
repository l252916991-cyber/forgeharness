"""The proportional effect rule that replaced fixed task-count bars."""

from __future__ import annotations

import pytest

from benchmarks.experiments.rules import describe, relative_effect


@pytest.mark.parametrize(
    ("baseline", "candidate", "expected"),
    [
        (10, 5, "better"),
        (10, 6, "no_clear_difference"),
        (5, 2, "better"),
        (4, 0, "insufficient_errors"),
        (0, 0, "insufficient_errors"),
        (2, 10, "worse"),
        (5, 10, "worse"),
        (6, 10, "no_clear_difference"),
        (4, 6, "no_clear_difference"),
    ],
)
def test_relative_effect(baseline: int, candidate: int, expected: str) -> None:
    assert relative_effect(baseline, candidate) == expected


def test_ablation_case_is_judgeable_under_the_new_rule() -> None:
    # Team 4 failed tasks, findings-first 1: the sealed ablation rule (four more tasks
    # solved) could not register this; the proportional rule calls it insufficient
    # rather than "no effect", and an 8-to-2 version would count as better.
    assert relative_effect(4, 1) == "insufficient_errors"
    assert relative_effect(8, 2) == "better"


@pytest.mark.parametrize(
    "kwargs", [{"floor": 0}, {"reduction": 0.0}, {"reduction": 1.0}, {"reduction": 1.5}]
)
def test_invalid_rule_parameters(kwargs: dict) -> None:
    with pytest.raises(ValueError):
        relative_effect(1, 1, **kwargs)


def test_negative_counts_refused() -> None:
    with pytest.raises(ValueError):
        relative_effect(-1, 0)


def test_description_states_the_thresholds() -> None:
    text = describe(6, 0.5)
    assert ">= 6 errors" in text and "50%" in text
