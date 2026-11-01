"""Repeat actual host entrypoints using each host's bound SDK; write only reports.

No host source/lockfile changes, production DB access, public listener or paid API.
LexVault's synthetic task driver and Commerce's HTTP ASGI entrypoint stay distinct.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import time
from pathlib import Path

from benchmarks.framework_snapshot import snapshot
from benchmarks.improvement_audit import inspect_host
from benchmarks.improvement_faults import junit_counts

ROOT = Path(__file__).resolve().parents[1]


def code_identity(root: Path, code: str) -> dict:
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for directory in (code, "tests")
        for p in sorted((root / directory).rglob("*.py"))
    }


def framework_interpreter(path: Path) -> str:
    # Resolving bin/python's symlink escapes a uv venv and loses site-packages.
    selected = path.expanduser().absolute()
    if not selected.is_file():
        raise ValueError("framework interpreter does not exist")
    return str(selected)


def command(
    args: list[str], cwd: Path, env: dict[str, str], log: Path, expected_exit: int = 0
) -> dict:
    started = time.monotonic()
    try:
        result = subprocess.run(
            args, cwd=cwd, env=env, capture_output=True, text=True, timeout=180, check=False
        )
        output, code = result.stdout + result.stderr, result.returncode
    except subprocess.TimeoutExpired:
        output, code = "bounded host command timed out\n", 124
    log.write_text(output)
    return {
        "command": args,
        "exit_code": code,
        "expected_exit": expected_exit,
        "passed": code == expected_exit,
        "seconds": time.monotonic() - started,
        "log": str(log.relative_to(ROOT)),
    }


def run(lexvault: Path, commerce: Path, framework_python: Path, output: Path) -> dict:
    output = output.resolve()
    if not output.is_relative_to(ROOT / "reports") or output == ROOT / "reports":
        raise ValueError("all generated host state must stay in workspace reports")
    if output.exists():
        raise ValueError("preserve existing host attempts; select a new output")
    output.mkdir(parents=True)
    bindings = {
        "lexvault": inspect_host(lexvault, "vendor/FRAMEWORK_BINDING.json", "app"),
        "commerce": inspect_host(commerce, "docs/FRAMEWORK_BINDING.json", "src"),
    }
    if not all(
        h["wheel_matches_binding"] and h["facade_imports_valid"] and h["module_imports_valid"]
        for h in bindings.values()
    ):
        raise ValueError("host binding mismatch")
    source = snapshot(ROOT)["tree_digest"]
    hosts_before = {
        "lexvault": code_identity(lexvault, "app"),
        "commerce": code_identity(commerce, "src"),
    }
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("FORGE", "LAW_REVIEW_", "R2_"))
        and k not in {"PYTHONPATH", "PYTEST_ADDOPTS", "COVERAGE_PROCESS_START"}
    }
    env.update(
        PYTHONDONTWRITEBYTECODE="1",
        PYTEST_ADDOPTS="",
        LAW_REVIEW_FORGE_PYTHON=framework_interpreter(framework_python),
    )
    rows = []
    for repeat in range(3):
        for host, directory in (("lexvault", lexvault), ("commerce", commerce)):
            destination = output / host / str(repeat)
            destination.mkdir(parents=True)
            (destination / "tmp").mkdir()
            current = {
                **env,
                "TMPDIR": str(destination / "tmp"),
                "LAW_REVIEW_DATA_DIR": str(destination / "data"),
                "FORGECOMMERCE_STATE": str(destination / "data"),
            }
            python = str(directory / ".venv/bin/python")
            if host == "lexvault":
                cmd = [
                    python,
                    "-B",
                    "-m",
                    "pytest",
                    "-p",
                    "no:cacheprovider",
                    "-o",
                    "addopts=",
                    "tests/test_host_integration.py",
                    "tests/test_domain_integration.py",
                    "-q",
                    "--basetemp=" + str(destination / "artifacts"),
                    "--junitxml=" + str(destination / "tests.xml"),
                ]
                row = command(cmd, directory, current, destination / "run.log")
                counts = (
                    junit_counts(destination / "tests.xml")
                    if (destination / "tests.xml").exists()
                    else {}
                )
                row["counts"] = counts
                row["passed"] &= counts.get("tests", 0) > 0 and not any(
                    counts.get(key, 0) for key in ("failures", "errors", "skipped")
                )
                rows.append(row | {"host": host, "repeat": repeat})
            else:
                for pattern in ("test_workflows.py", "test_pilot_identity.py"):
                    row = command(
                        [
                            python,
                            "-B",
                            "-m",
                            "unittest",
                            "discover",
                            "-s",
                            "tests",
                            "-p",
                            pattern,
                            "-v",
                        ],
                        directory,
                        current,
                        destination / (pattern + ".log"),
                    )
                    text = (destination / (pattern + ".log")).read_text()
                    count = re.search(r"Ran (\d+) tests?", text)
                    row["tests"] = int(count.group(1)) if count else 0
                    row["passed"] &= bool(count) and row["tests"] > 0 and "skipped=" not in text
                    rows.append(row | {"host": host, "repeat": repeat})
                current["PYTHONPATH"] = str(directory / "src")
                # A fresh host process dies after the actual SDK effect, followed
                # by two fresh HTTP host processes proving stable receipt reuse.
                for index, action in enumerate(("crash", "recover", "recover")):
                    row = command(
                        [
                            python,
                            "-B",
                            str(ROOT / "benchmarks/commerce_process_fixture.py"),
                            str(destination / "process-state"),
                            action,
                        ],
                        directory,
                        current,
                        destination / f"process-{index}-{action}.log",
                        expected_exit=91 if action == "crash" else 0,
                    )
                    rows.append(row | {"host": host, "repeat": repeat, "action": action})
            print(f"{host} round {repeat + 1}/3 complete", flush=True)
    hosts_after = {
        "lexvault": code_identity(lexvault, "app"),
        "commerce": code_identity(commerce, "src"),
    }
    artifacts = {
        str(p.relative_to(output)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(output.rglob("*"))
        if p.is_file()
    }
    result = {
        "schema_version": 1,
        "source_identity": source,
        "source_unchanged": source == snapshot(ROOT)["tree_digest"],
        "host_code_unchanged": hosts_before == hosts_after,
        "host_code_sha256": hosts_before,
        "bindings": bindings,
        "commands": rows,
        "artifact_sha256": artifacts,
        "passed": all(r["passed"] for r in rows) and hosts_before == hosts_after,
        "transport": {
            "lexvault": "actual synthetic task/sidecar and domain task entrypoints",
            "commerce": "actual HTTP ASGI entrypoint, local synthetic test tenant",
        },
        "production_http_ui_verified": False,
        "real_customer_writes": False,
        "independent_onboarding_measured": False,
    }
    (output / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lexvault", type=Path, required=True)
    parser.add_argument("--commerce", type=Path, required=True)
    parser.add_argument("--framework-python", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    raise SystemExit(
        0 if run(args.lexvault, args.commerce, args.framework_python, args.output)["passed"] else 1
    )


if __name__ == "__main__":
    main()
