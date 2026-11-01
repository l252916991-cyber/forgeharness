"""Run Cloud contracts against an owned, disposable PostgreSQL 18.6 server.

The test binary is a locked optional development extra, not a production server.
No caller-supplied database is used. Every server is Unix-socket-only and stopped
before its synthetic files are removed. See embedded-postgres upstream notices:
https://github.com/kainpl/embedded-postgres (Apache-2.0 wrapper; PostgreSQL license).
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from uuid import uuid4

from benchmarks.framework_snapshot import snapshot
from forgeharness.cloud.provision import schema_digest

ROOT = Path(__file__).resolve().parents[1]
MIN_POSTGRES_TESTS = 183


def execute(
    output: Path,
    *,
    coverage_output: Path | None = None,
    test_paths: tuple[str, ...] = ("evals/saas_postgres/tests",),
) -> bool:
    output = output.resolve()
    if not output.is_relative_to(ROOT / "reports") or output == ROOT / "reports":
        raise ValueError("PostgreSQL evidence must be inside workspace reports")
    output.mkdir(parents=True, exist_ok=False)
    before = snapshot(ROOT)["tree_digest"]
    result: dict[str, Any] = {
        "schema_version": 1,
        "source_identity": before,
        "schema_sha256": schema_digest(ROOT / "src/forgeharness/cloud"),
        "model": "no model or business network calls",
        "business_qualified": False,
        "saas_qualified": False,
        "server_stopped": False,
        "passed": False,
    }
    try:
        embedded = importlib.import_module("embedded_postgres")
        with TemporaryDirectory(prefix="state-", dir=output) as directory:
            server = embedded.get_server(Path(directory) / "pgdata", cleanup_mode="stop")
            try:
                version = server.psql("SHOW server_version").strip()
                listen = server.psql(
                    "SELECT current_setting('listen_addresses') = '' AS unix_socket_only"
                )
                if "18.6" not in version or not re.search(r"^\s*t\s*$", listen, re.MULTILINE):
                    raise RuntimeError("expected PostgreSQL 18.6 on a private Unix socket")
                result["server_version"] = "18.6"
                result["network"] = "private Unix socket"
                env = {
                    key: value
                    for key, value in os.environ.items()
                    if key in {"PATH", "HOME", "LANG", "LC_ALL", "TMPDIR"}
                }
                env.update(
                    FH_TEST_PG_DISPOSABLE="1",
                    FH_TEST_PG_ADMIN_DSN=server.get_uri(),
                    PYTHONPATH=str(ROOT),
                    PYTHONNOUSERSITE="1",
                )
                command = [
                    sys.executable,
                    "-m",
                    "pytest",
                    *test_paths,
                    "-q",
                    "--tb=short",
                    "--junitxml=" + str(output / "tests.xml"),
                ]
                if coverage_output is not None:
                    command.extend(
                        [
                            "--cov=forgeharness",
                            "--cov-branch",
                            "--cov-append",
                            "--cov-fail-under=0",
                            "--cov-report=json:" + str(coverage_output.resolve()),
                        ]
                    )
                with (output / "tests.log").open("w") as log:
                    process = subprocess.run(
                        command,
                        cwd=ROOT,
                        env=env,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        timeout=300,
                        check=False,
                    )
                result["exit_code"] = process.returncode
            finally:
                server.cleanup()
                result["server_stopped"] = True
        suites = list(ET.parse(output / "tests.xml").getroot().iter("testsuite"))
        counts = {
            key: sum(int(s.attrib.get(key, "0")) for s in suites)
            for key in ("tests", "failures", "errors", "skipped")
        }
        result["tests"] = counts
        result["passed"] = (
            result["exit_code"] == 0
            and counts["tests"] >= MIN_POSTGRES_TESTS
            and not any(counts[key] for key in ("failures", "errors", "skipped"))
        )
    except Exception as exc:
        # Deliberately omit DSNs, arbitrary subprocess stderr and environment.
        result["error_type"] = type(exc).__name__
    result["source_unchanged"] = snapshot(ROOT)["tree_digest"] == before
    result["passed"] = result["passed"] and result["source_unchanged"] and result["server_stopped"]
    (output / "summary.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"passed": result["passed"], "summary": str(output / "summary.json")}))
    return bool(result["passed"])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--coverage-output", type=Path)
    args = parser.parse_args()
    output = args.output or ROOT / "reports/saas-postgres" / (
        datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
    )
    raise SystemExit(0 if execute(output, coverage_output=args.coverage_output) else 1)


if __name__ == "__main__":
    main()
