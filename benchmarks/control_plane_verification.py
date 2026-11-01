"""Qualify invitation-only control-plane candidates without replacing old evidence.

This extends the complete comparison release suite. New PostgreSQL and installed
wheel/browser consumers are additional gates; historical counts do not qualify
new code. Browser tooling is an explicit local verification prerequisite, never a
runtime dependency. Freeze and execution are separate, reviewable operations.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
from pathlib import Path
from typing import Any

from benchmarks import session_verification as base
from benchmarks.comparison import verify as comparison
from forgeharness.cloud.provision import schema_digest

CONSUMER_CHECKS = frozenset(
    {
        "two_organizations",
        "two_applications",
        "same_role_owner_isolation",
        "forged_path_denied",
        "asynchronous_acceptance",
        "independent_approval",
        "durable_rejection",
        "revocation_prevents_send",
        "worker_process_recovery",
        "independent_business_oracle",
        "least_privilege",
        "migration_preserves_history",
        "secrets_not_exposed",
        "real_browser_flow",
    }
)


def valid_consumer(value: object, wheel_sha256: str) -> bool:
    """Reject incomplete evidence, source imports, and unsupported SaaS claims."""
    if not isinstance(value, dict):
        return False
    checks = value.get("checks")
    browser = value.get("browser")
    recoveries = value.get("worker_recoveries")
    pids = value.get("worker_pids")
    approval_evidence = value.get("approval_evidence")
    if (
        not isinstance(browser, dict)
        or browser.get("engine") != "Chromium"
        or not isinstance(browser.get("browser_version"), str)
        or not browser.get("browser_version")
        or type(browser.get("external_requests")) is not int
        or browser.get("external_requests") != 0
        or not isinstance(browser.get("reviewer_permissions"), dict)
        or browser["reviewer_permissions"].get("can_submit") is not False
        or browser["reviewer_permissions"].get("can_approve") is not True
        or not isinstance(browser.get("no_script_secret_protection"), dict)
        or browser["no_script_secret_protection"].get("disabled_js") is not True
        or browser["no_script_secret_protection"].get("blocked_script") is not True
        or not isinstance(approval_evidence, dict)
        or approval_evidence.get("dual_role_owner_self_decisions_denied") is not True
        or any(
            browser.get(key) is not True
            for key in (
                "passed",
                "invitation",
                "login",
                "management",
                "task",
                "independent_approval",
                "cancel",
                "revocation",
                "logout",
            )
        )
        or not isinstance(recoveries, list)
        or len(recoveries) != 2
        or not isinstance(pids, list)
        or len(pids) != 6
        or any(type(pid) is not int or pid <= 0 for pid in pids)
        or len(set(pids)) != 6
    ):
        return False
    modes = set()
    for recovery in recoveries:
        if not isinstance(recovery, dict) or any(
            type(recovery.get(key)) is not int
            for key in ("old_pid", "new_pid", "old_epoch", "new_epoch")
        ):
            return False
        if (
            recovery["new_epoch"] <= recovery["old_epoch"]
            or recovery["old_epoch"] < 1
            or recovery["old_pid"] not in pids
            or recovery["new_pid"] not in pids
            or recovery["old_pid"] == recovery["new_pid"]
            or recovery.get("mode") not in ("SIGSTOP/SIGCONT", "SIGKILL")
        ):
            return False
        modes.add(recovery.get("mode"))
    return (
        isinstance(checks, dict)
        and set(checks) == CONSUMER_CHECKS
        and all(checks[name] is True for name in CONSUMER_CHECKS)
        and value.get("passed") is True
        and value.get("loaded_from_installation") is True
        and value.get("wheel_sha256") == wheel_sha256
        and value.get("server_stopped") is True
        and type(value.get("external_requests")) is int
        and value.get("external_requests") == 0
        and modes == {"SIGSTOP/SIGCONT", "SIGKILL"}
        and value.get("saas_qualified") is False
        and value.get("business_qualified") is False
    )


class ControlPlaneVerification(comparison.ComparisonVerification):
    """Keep all inherited gates and append actual control-plane consumers."""

    def __init__(
        self,
        frozen: dict[str, Any],
        *,
        browser_node: str = "node",
        playwright_module: str = "playwright",
        browser_executable: str | None = None,
    ) -> None:
        super().__init__(frozen)
        self.browser_node = browser_node
        self.playwright_module = playwright_module
        self.browser_executable = browser_executable
        if Path(browser_node).is_absolute():
            self.env["PATH"] = (
                str(Path(browser_node).parent) + os.pathsep + self.env.get("PATH", "")
            )
        self.summary["browser_tooling"] = {
            "node": browser_node,
            "playwright_module": playwright_module,
            "browser_executable": browser_executable,
        }
        self.summary["qualification_scope"] = "invitation-only internal Alpha control plane"
        self.summary["saas_qualified"] = False
        self.summary["business_qualified"] = False

    def additional_coverage(self, work: Path, profile: str, report: Path) -> None:
        super().additional_coverage(work, profile, report)
        local = work / "reports" / ("control-plane-" + self.output.name)
        self.run(
            profile + "/control-plane-contracts",
            [
                "uv",
                "run",
                "--no-sync",
                "python",
                "-m",
                "benchmarks.control_plane_postgres",
                "--output",
                str(local),
                "--coverage-output",
                str(report / "coverage.json"),
            ],
            work,
        )
        if local.exists():
            shutil.copytree(local, report / "control-plane")
        try:
            evidence = json.loads((report / "control-plane/summary.json").read_text())
            valid = comparison.valid_postgres(
                evidence,
                self.frozen["tree_digest"],
                schema_digest(work / "src/forgeharness/cloud"),
            )
            # Existing PG gates keep their independent 183-test floor; this gate
            # has an exact collected-node digest and cannot pass an empty suite.
            valid = valid and evidence.get("control_plane_suite") is True
            self.assert_check(profile + "/control-plane-evidence", valid)
        except (OSError, ValueError, KeyError) as exc:
            self.assert_check(profile + "/control-plane-evidence", False, error=str(exc))

    def artifacts(self, work: Path) -> None:
        super().artifacts(work)
        name = f"forgeharness-{base.VERSION}-py3-none-any.whl"
        artifact = self.summary.get("artifacts", {}).get(name)
        if not artifact:
            self.assert_check("control-consumer/artifact", False)
            return
        wheel = self.output / "dist" / name
        consumer = self.work / "control-consumer"
        consumer.mkdir()
        venv = consumer / ".venv"
        if not self.run(
            "control-consumer/venv", ["uv", "venv", "--python", "3.12", str(venv)], consumer
        ):
            return
        python = str(venv / "bin/python")
        if not self.run(
            "control-consumer/install",
            [
                "uv",
                "pip",
                "install",
                "--python",
                python,
                str(wheel) + "[cloud,web,models,postgres-test]",
            ],
            consumer,
        ):
            return
        evidence = self.output / "control-consumer.json"
        command = [
            python,
            str(work / "evals/saas_control_plane/consumer.py"),
            "--require-installed",
            "--wheel",
            str(wheel),
            "--output",
            str(evidence),
            "--browser-node",
            self.browser_node,
            "--playwright-module",
            self.playwright_module,
        ]
        if self.browser_executable:
            command.extend(("--browser-executable", self.browser_executable))
        self.run(
            "control-consumer/end-to-end",
            command,
            consumer,
        )
        try:
            self.assert_check(
                "control-consumer/evidence",
                valid_consumer(json.loads(evidence.read_text()), artifact["sha256"]),
            )
        except (OSError, ValueError, KeyError) as exc:
            self.assert_check("control-consumer/evidence", False, error=str(exc))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    freeze = parser.add_mutually_exclusive_group()
    freeze.add_argument("--freeze", action="store_true")
    freeze.add_argument("--freeze-only", action="store_true")
    parser.add_argument(
        "--candidate-dir",
        type=Path,
        default=Path("reports/saas/20261101-control-plane/candidate-r1"),
    )
    parser.add_argument("--browser-node", default="node")
    parser.add_argument("--playwright-module", default="playwright")
    parser.add_argument("--browser-executable")
    args = parser.parse_args()
    try:
        comparison.configure_candidate(args.candidate_dir)
        frozen = base.candidate(freeze=args.freeze or args.freeze_only)
    except (OSError, ValueError) as exc:
        parser.exit(2, f"Candidate rejected: {exc}\n")
    if args.freeze_only:
        print(
            json.dumps({"source_identity": frozen["tree_digest"], "manifest": str(base.MANIFEST)})
        )
        return
    runner = ControlPlaneVerification(
        frozen,
        browser_node=args.browser_node,
        playwright_module=args.playwright_module,
        browser_executable=args.browser_executable,
    )
    passed = runner.execute()
    print(
        json.dumps(
            {
                "internal_alpha_engineering_qualified": passed,
                "saas_qualified": False,
                "business_qualified": False,
                "summary": str(base.OUTPUT / "summary.json"),
            }
        )
    )
    raise SystemExit(0 if passed else 1)


if __name__ == "__main__":
    main()
