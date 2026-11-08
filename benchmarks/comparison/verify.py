"""Qualify a scoped candidate without replacing historical source freezes.

Use ``--candidate-dir reports/framework-next --freeze-only`` to freeze the current
framework candidate, then use the same directory without the freeze flag to verify.
The legacy default remains available for reproducing the original P0/P1 release.
"""

import argparse
import json
import shutil
from pathlib import Path
from typing import Any

from benchmarks import business_verification as business
from benchmarks import session_verification as base
from benchmarks.comparison.host import digest
from benchmarks.saas_postgres import MIN_POSTGRES_TESTS
from forgeharness.cloud.provision import schema_digest


def valid_comparison(result: dict[str, Any], identity: str, protocol_hash: str) -> bool:
    return (
        result.get("source_identity") == identity
        and result.get("protocol_sha256") == protocol_hash
        and result.get("source_unchanged") is True
        and result.get("integration_gate_passed") is True
        and result.get("expected_results") == result.get("passed") == 33
        and result.get("failed") == result.get("not_measured") == 0
        and result.get("business_qualified") is False
        and result.get("core_advantage_proven") is False
        and result.get("native_full_fault_comparison_complete") is False
        and result.get("independent_onboarding_measured") is False
        and result.get("model_input_evidence_complete") is True
        and result.get("decision") == "insufficient_evidence_for_native_core_advantage"
    )


def valid_postgres(result: dict[str, Any], identity: str, schema_hash: str) -> bool:
    if not isinstance(result, dict):
        return False
    counts = result.get("tests", {})
    if not isinstance(counts, dict) or any(
        type(counts.get(key)) is not int for key in ("tests", "failures", "errors", "skipped")
    ):
        return False
    return (
        result.get("passed") is True
        and result.get("source_unchanged") is True
        and result.get("source_identity") == identity
        and result.get("schema_sha256") == schema_hash
        and result.get("server_version") == "18.6"
        and result.get("network") == "private Unix socket"
        and result.get("server_stopped") is True
        and result.get("business_qualified") is False
        and result.get("saas_qualified") is False
        and counts.get("tests", 0) >= MIN_POSTGRES_TESTS
        and counts.get("failures") == counts.get("errors") == counts.get("skipped") == 0
    )


class ComparisonVerification(business.BusinessVerification):
    def additional_coverage(self, work: Path, profile: str, report: Path) -> None:
        local = work / "reports" / ("postgres-" + self.output.name)
        self.run(
            profile + "/postgres-state-contract",
            [
                "uv",
                "run",
                "--no-sync",
                "python",
                "-m",
                "benchmarks.saas_postgres",
                "--output",
                str(local),
                "--coverage-output",
                str(report / "coverage.json"),
            ],
            work,
        )
        if local.exists():
            shutil.copytree(local, report / "postgres")
        try:
            result = json.loads((report / "postgres/summary.json").read_text())
            self.assert_check(
                profile + "/postgres-evidence",
                valid_postgres(
                    result,
                    self.frozen["tree_digest"],
                    schema_digest(work / "src/forgeharness/cloud"),
                ),
            )
        except (OSError, ValueError, KeyError) as exc:
            self.assert_check(profile + "/postgres-evidence", False, error=str(exc))

    def quality(self, work: Path, profile: str) -> dict[str, Any]:
        metrics = super().quality(work, profile)
        self.run(
            profile + "/account-workbench",
            ["uv", "run", "--no-sync", "python", "examples/workbench_smoke.py"],
            work,
        )
        self.run(
            profile + "/workbench-browser-races",
            ["node", "tests/browser/workbench_races.cjs"],
            work,
        )
        self.run(
            profile + "/r2-faults",
            [
                "uv",
                "run",
                "--no-sync",
                "pytest",
                "tests/unit/test_durable_orchestration.py",
                "--junitxml=" + str(self.output / profile / "r2-faults.xml"),
            ],
            work,
        )
        self.run(
            profile + "/parallel-faults",
            [
                "uv",
                "run",
                "--no-sync",
                "pytest",
                "tests/unit/test_parallel_execution.py",
                "tests/unit/test_parallel_recovery.py",
                "--junitxml=" + str(self.output / profile / "parallel-faults.xml"),
            ],
            work,
        )
        parallel_local = work / "reports" / ("parallel-contract-" + self.output.name)
        parallel_report = self.output / profile / "parallel-contract"
        self.run(
            profile + "/parallel-contract",
            [
                "uv",
                "run",
                "--no-sync",
                "python",
                "-m",
                "benchmarks.parallel_execution",
                "--mode",
                "contract",
                "--output",
                str(parallel_local),
            ],
            work,
        )
        if parallel_local.exists():
            shutil.copytree(parallel_local, parallel_report)
        try:
            parallel = json.loads((parallel_report / "summary.json").read_text())
            self.assert_check(
                profile + "/parallel-evidence",
                parallel["complete"] is True
                and parallel["actual_runs"] == 24
                and parallel["measurement_valid"] is False
                and parallel["source_unchanged"] is True
                and all(
                    parallel["families"][family]["arms"]["2"]["peak_client_requests"] == 2
                    for family in ("fork", "uneven")
                ),
            )
        except (OSError, ValueError, KeyError) as exc:
            self.assert_check(profile + "/parallel-evidence", False, error=str(exc))
        report = self.output / profile / "framework-comparison"
        self.run(
            profile + "/framework-integration",
            [
                "uv",
                "run",
                "--no-sync",
                "python",
                "-m",
                "benchmarks.comparison.run",
                "--prepare",
                "--output",
                str(report),
            ],
            work,
        )
        try:
            result = json.loads((report / "summary.json").read_text())
            valid = valid_comparison(
                result,
                self.frozen["tree_digest"],
                digest(work / "evals/comparison/protocol-sdk0140-r1.json"),
            )
            self.assert_check(profile + "/comparison-evidence", valid)
            metrics["framework_comparison"] = {
                key: result[key]
                for key in (
                    "passed",
                    "failed",
                    "not_measured",
                    "expected_results",
                    "decision",
                    "core_advantage_proven",
                    "native_full_fault_comparison_complete",
                )
            }
        except (OSError, ValueError, KeyError) as exc:
            self.assert_check(profile + "/comparison-evidence", False, error=str(exc))
        for engine in ("deepagents", "pydantic"):
            project = work / "evals/comparison/environments" / engine
            requirements = self.output / profile / (engine + "-requirements.txt")
            self.run(
                profile + "/comparison-export-" + engine,
                [
                    "uv",
                    "export",
                    "--project",
                    str(project),
                    "--frozen",
                    "--no-emit-project",
                    "--no-dev",
                    "-o",
                    str(requirements),
                ],
                work,
            )
            self.run(
                profile + "/comparison-audit-" + engine,
                [
                    "uv",
                    "run",
                    "--no-sync",
                    "--with",
                    "pip-audit",
                    "pip-audit",
                    "-r",
                    str(requirements),
                    "--no-deps",
                    "--disable-pip",
                    "-f",
                    "json",
                    "-o",
                    str(self.output / profile / (engine + "-dependencies.json")),
                ],
                work,
            )
        return metrics

    def artifacts(self, work: Path) -> None:
        super().artifacts(work)
        artifacts = self.summary.get("artifacts", {})
        wheel_name = f"forgeharness-{base.VERSION}-py3-none-any.whl"
        if wheel_name not in artifacts:
            self.assert_check("web-consumer/artifact", False, error="wheel unavailable")
            return
        wheel = self.output / "dist" / wheel_name
        consumer = self.work / "web-consumer"
        consumer.mkdir()
        venv = consumer / ".venv"
        if not self.run(
            "web-consumer/venv", ["uv", "venv", "--python", "3.12", str(venv)], consumer
        ):
            return
        python = str(venv / "bin/python")
        if not self.run(
            "web-consumer/install",
            ["uv", "pip", "install", "--python", python, str(wheel) + "[web,models]"],
            consumer,
        ):
            return
        self.run(
            "web-consumer/workbench",
            [python, str(work / "examples/workbench_smoke.py")],
            consumer,
        )
        self.run(
            "web-consumer/cli",
            [python, str(work / "examples/commerce_workbench.py"), "--help"],
            consumer,
        )
        cloud = self.work / "cloud-consumer"
        cloud.mkdir()
        venv = cloud / ".venv"
        if not self.run(
            "cloud-consumer/venv", ["uv", "venv", "--python", "3.12", str(venv)], cloud
        ):
            return
        python = str(venv / "bin/python")
        if not self.run(
            "cloud-consumer/install",
            ["uv", "pip", "install", "--python", python, str(wheel) + "[cloud,postgres-test]"],
            cloud,
        ):
            return
        self.run(
            "cloud-consumer/state",
            [python, str(work / "examples/cloud_state_demo.py")],
            cloud,
        )
        self.run(
            "cloud-consumer/hosted",
            [python, str(work / "examples/cloud_hosted_demo.py"), "--require-installed"],
            cloud,
        )


def configure_candidate(directory: Path) -> None:
    """Keep manifests and generated clean copies inside this workspace's reports."""
    root = base.ROOT.resolve()
    destination = (root / directory).resolve()
    reports = root / "reports"
    if destination == reports or not destination.is_relative_to(reports):
        raise ValueError("candidate directory must be a subdirectory of workspace reports")
    business.configure()
    base.OUTPUT = destination / "verification"
    base.MANIFEST = destination / "source.json"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    freeze = parser.add_mutually_exclusive_group()
    freeze.add_argument("--freeze", action="store_true", help="freeze and run verification")
    freeze.add_argument("--freeze-only", action="store_true", help="freeze without running gates")
    parser.add_argument(
        "--candidate-dir",
        type=Path,
        default=Path("reports/next-candidate"),
        help="candidate evidence directory within workspace reports",
    )
    args = parser.parse_args()
    try:
        configure_candidate(args.candidate_dir)
        frozen = base.candidate(freeze=args.freeze or args.freeze_only)
    except (OSError, ValueError) as exc:
        parser.exit(2, f"Candidate rejected: {exc}\n")
    if args.freeze_only:
        print(
            json.dumps({"source_identity": frozen["tree_digest"], "manifest": str(base.MANIFEST)})
        )
        return
    runner = ComparisonVerification(frozen)
    passed = runner.execute()
    print(
        json.dumps(
            {
                "engineering_qualified": passed,
                "business_qualified": False,
                "summary": str(base.OUTPUT / "summary.json"),
            }
        )
    )
    raise SystemExit(0 if passed else 1)


if __name__ == "__main__":
    main()
