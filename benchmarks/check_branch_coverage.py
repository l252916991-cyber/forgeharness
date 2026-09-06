"""Fail independently on pure branch coverage, not coverage.py's combined score."""

from __future__ import annotations

import json
from pathlib import Path


def main() -> None:
    totals = json.loads(Path("reports/coverage.json").read_text())["totals"]
    branches = totals["num_branches"]
    covered = totals["covered_branches"]
    percent = covered / branches * 100 if branches else 0
    print(f"pure_branch_coverage={covered}/{branches} ({percent:.2f}%) target=85%")
    if percent < 85:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
