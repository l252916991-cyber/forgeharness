"""Pre-registered effect rules that scale with the baseline's own failures.

The collaboration ablation (``reports/collaboration-ablation/REPORT.md``) fixed its
bar in tasks: a variant "removed the failure" only if it solved at least four more
tasks than the team. The team then failed only four of twelve, so a variant had to be
perfect to count, and the rule could not register any effect. The report's lesson:
set the bar as a share of the baseline's actual failures, with a floor below which
the data cannot speak.

``relative_effect`` encodes that lesson once, so new studies seal the same rule and
report "too few errors to judge" instead of a silent "no effect".
"""

from __future__ import annotations

from typing import Literal

Effect = Literal["better", "worse", "no_clear_difference", "insufficient_errors"]


def relative_effect(
    baseline_errors: int,
    candidate_errors: int,
    *,
    floor: int = 5,
    reduction: float = 0.5,
) -> Effect:
    """Compare two error counts on the same items.

    - ``better``: the baseline has at least ``floor`` errors and the candidate removes
      at least ``reduction`` of them;
    - ``worse``: the same rule with the roles swapped;
    - ``insufficient_errors``: neither arm reaches ``floor``, so no direction can be
      claimed, which is different from "no difference";
    - ``no_clear_difference``: otherwise.
    """
    if baseline_errors < 0 or candidate_errors < 0:
        raise ValueError("error counts are non-negative")
    if floor < 1 or not 0 < reduction < 1:
        raise ValueError("floor must be positive and reduction a proper fraction")
    keep = 1 - reduction
    if baseline_errors >= floor and candidate_errors <= baseline_errors * keep:
        return "better"
    if candidate_errors >= floor and baseline_errors <= candidate_errors * keep:
        return "worse"
    if baseline_errors < floor and candidate_errors < floor:
        return "insufficient_errors"
    return "no_clear_difference"


def describe(floor: int = 5, reduction: float = 0.5) -> str:
    """The rule in words, for a sealed protocol."""
    return (
        f"candidate better if the baseline has >= {floor} errors and the candidate has at "
        f"most {1 - reduction:.0%} of them; worse if the same holds with roles swapped; "
        f"insufficient_errors if both arms have < {floor}; otherwise no_clear_difference"
    )
