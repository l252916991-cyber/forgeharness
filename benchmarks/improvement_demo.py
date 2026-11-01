"""Three-minute presentation fixture: commit, lose acknowledgement, reconcile."""

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def demo(output: Path) -> dict:
    output = output.resolve()
    if not output.is_relative_to(ROOT / "reports") or output.exists():
        raise ValueError("select a fresh directory within workspace reports")
    output.mkdir(parents=True)
    rows = []
    for action, crash, expected in (
        ("run", "effect-committed", 91),
        ("resume", "", 0),
        ("reconcile", "", 0),
        ("resume", "", 0),
    ):
        command = [
            sys.executable,
            str(ROOT / "tests/integration/r2_fixture.py"),
            str(output),
            action,
        ]
        result = subprocess.run(
            command,
            cwd=ROOT,
            env={**os.environ, "R2_CRASH_AT": crash},
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        (output / f"{len(rows)}-{action}.log").write_text(result.stdout + result.stderr)
        if result.returncode != expected:
            raise RuntimeError(f"unexpected fixture exit: {result.returncode}")
        row = json.loads(result.stdout) if expected == 0 else {"process_exit": expected}
        rows.append(row)
        print(action, row.get("status", "process exited"), row.get("error") or "", flush=True)
    with sqlite3.connect(output / "effects.sqlite") as connection:
        effects = connection.execute("SELECT COUNT(*) FROM effects").fetchone()[0]
    assert rows[1]["error"] == "needs_reconciliation:write"
    assert rows[2]["status"] == rows[3]["status"] == "succeeded" and effects == 1
    result = {
        "synthetic": True,
        "real_customer_write": False,
        "effects": effects,
        "stages": rows,
        "meaning": "receipt reconciliation, not external exactly-once",
    }
    (output / "summary.json").write_text(json.dumps(result, indent=2) + "\n")
    print("external synthetic receipt count:", effects)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    demo(args.output)


if __name__ == "__main__":
    main()
