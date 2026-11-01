"""Append real control-plane HTTP/PG tests on an owned Unix-socket database.

The inherited 183-test PostgreSQL floor is preserved. This gate additionally
requires nonempty control-plane evidence and records the exact executed nodes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from benchmarks import saas_postgres


def execute(output: Path, *, coverage_output: Path | None = None) -> bool:
    passed = saas_postgres.execute(
        output,
        coverage_output=coverage_output,
        test_paths=("evals/saas_postgres/tests", "evals/saas_control_plane/tests"),
    )
    summary = output / "summary.json"
    value = json.loads(summary.read_text())
    try:
        cases = [
            item
            for item in ET.parse(output / "tests.xml").getroot().iter("testcase")
            if item.attrib.get("classname", "").startswith("evals.saas_control_plane.")
        ]
        nodes = sorted(case.attrib["classname"] + "::" + case.attrib["name"] for case in cases)
        complete = bool(nodes) and all(len(case) == 0 for case in cases)
        value["control_plane_nodes"] = nodes
        value["control_plane_node_sha256"] = hashlib.sha256(
            json.dumps(nodes, separators=(",", ":")).encode()
        ).hexdigest()
    except (OSError, ValueError, ET.ParseError, KeyError):
        complete = False
    value["control_plane_suite"] = complete
    value["passed"] = passed and complete
    summary.write_text(json.dumps(value, indent=2) + "\n")
    return bool(value["passed"])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--coverage-output", type=Path)
    args = parser.parse_args()
    output = args.output or saas_postgres.ROOT / "reports/control-plane-postgres" / (
        datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
    )
    raise SystemExit(0 if execute(output, coverage_output=args.coverage_output) else 1)


if __name__ == "__main__":
    main()
