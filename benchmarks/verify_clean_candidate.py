"""Verify a frozen source copy in a fresh, locked Python 3.12 environment.

This does not reuse the working .venv, Docker state, or model server. Reports
record command exit codes, exact source hash, test counts, and pure branches.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path

from forgeharness.review import CandidateManifest, snapshot_file, snapshot_tree_hash

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    manifest = CandidateManifest.model_validate_json(
        (ROOT / "reports/candidate-manifest.json").read_text()
    )
    current = tuple(snapshot_file(ROOT, item.path) for item in manifest.files)
    if current != manifest.files:
        raise SystemExit("source changed after freeze; freeze and review again")
    logs = ROOT / "reports/clean-logs"
    logs.mkdir(parents=True, exist_ok=True)
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("FORGE_") and key not in {"PYTHONPATH", "VIRTUAL_ENV"}
    }
    environment["PYTHONNOUSERSITE"] = "1"
    environment["UV_INDEX_URL"] = "https://pypi.org/simple"
    checks: dict[str, int] = {}
    with tempfile.TemporaryDirectory(prefix="forgeharness-clean-") as temporary:
        work = Path(temporary) / "candidate"
        work.mkdir()
        for item in manifest.files:
            relative = Path(item.path)
            if relative.is_absolute() or ".." in relative.parts:
                raise SystemExit("unsafe candidate path")
            target = work / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / relative, target)
        if (
            snapshot_tree_hash(tuple(snapshot_file(work, item.path) for item in manifest.files))
            != manifest.source_tree_sha256
        ):
            raise SystemExit("source copy does not match manifest")
        requirements = Path(temporary) / "requirements.txt"
        python = work / ".venv/bin/python"
        commands = [
            (
                "export",
                [
                    "uv",
                    "export",
                    "--frozen",
                    "--extra",
                    "dev",
                    "--extra",
                    "platform",
                    "--extra",
                    "benchmark",
                    "--no-emit-project",
                    "--no-hashes",
                    "--output-file",
                    str(requirements),
                ],
            ),
            ("venv", ["uv", "venv", "--python", "3.12", str(work / ".venv")]),
            (
                "install",
                [
                    "uv",
                    "pip",
                    "install",
                    "--python",
                    str(python),
                    "--index-url",
                    "https://pypi.org/simple",
                    "--no-deps",
                    "-r",
                    str(requirements),
                    "-e",
                    str(work),
                ],
            ),
            ("ruff-format", [str(python), "-m", "ruff", "format", "--check", "."]),
            ("ruff-lint", [str(python), "-m", "ruff", "check", "."]),
            ("mypy", [str(python), "-m", "mypy"]),
            (
                "pytest",
                [
                    str(python),
                    "-m",
                    "pytest",
                    "--cov=forgeharness",
                    "--cov-report=json:reports/coverage.json",
                    "--junitxml=reports/tests.xml",
                ],
            ),
            ("pure-branches", [str(python), "benchmarks/check_branch_coverage.py"]),
        ]
        for name, command in commands:
            with (logs / f"{name}.log").open("w") as log:
                completed = subprocess.run(
                    command,
                    cwd=work,
                    env=environment,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    timeout=600,
                    check=False,
                )
            checks[name] = completed.returncode
            print(f"clean {name}: exit={completed.returncode}", flush=True)
            if completed.returncode and name in {"export", "venv", "install"}:
                break
        coverage_path = work / "reports/coverage.json"
        totals = json.loads(coverage_path.read_text())["totals"] if coverage_path.exists() else {}
        xml_path = work / "reports/tests.xml"
        suite = ET.parse(xml_path).getroot().find("testsuite") if xml_path.exists() else None
        report = {
            "schema_version": 1,
            "created_at": datetime.now(UTC).isoformat(),
            "revision": manifest.revision,
            "source_tree_sha256": manifest.source_tree_sha256,
            "profile": "fresh Python 3.12; locked dev/platform/benchmark; no OMLX or Docker tests",
            "commands": checks,
            "coverage": totals,
            "tests": dict(suite.attrib) if suite is not None else {},
            "qualified": len(checks) == len(commands)
            and all(code == 0 for code in checks.values()),
        }
    (ROOT / "reports/clean-verification.json").write_text(json.dumps(report, indent=2) + "\n")
    if not report["qualified"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
