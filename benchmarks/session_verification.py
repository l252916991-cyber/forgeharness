"""Freeze and verify the 0.4.0 session candidate without replacing older evidence.

Run from the repository root with ``uv run --no-sync python -m
benchmarks.session_verification --freeze``, then omit ``--freeze`` to verify.
Freezing never replaces a different manifest. Review and explicitly remove an
obsolete session manifest before freezing a changed candidate.

Each attempt has independent reports. Clean source and consumer environments stay
under reports/session-verification/work, outside the snapshot input directories.
Only deterministic controls and the development evaluation split are exercised;
these gates make no real-model, legal-quality, or production-readiness claim.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tarfile
import time
import tomllib
import xml.etree.ElementTree as ET
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from benchmarks.framework_snapshot import snapshot

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "reports/session-verification"
MANIFEST = ROOT / "reports/session-candidate-source.json"
VERSION = "0.4.0"
DEMOS = ("sdk_demo.py", "business_modules_demo.py", "session_demo.py")
EXTRAS = ("dev", "cli", "platform", "benchmark", "frameworks")
ACCEPTED_ADVISORY = "PYSEC-2026-3740"


def write_json(path: Path, payload: object) -> None:
    """Replace one report atomically; completed attempts keep their own reports."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def candidate(*, freeze: bool) -> dict[str, Any]:
    """Require an explicit source freeze and reject any subsequent divergence."""
    current = snapshot(ROOT)
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    if version != VERSION:
        raise ValueError(f"expected distribution {VERSION}; found {version}")
    for name in DEMOS:
        if not (ROOT / "examples" / name).is_file():
            raise ValueError(f"required demonstration is missing: examples/{name}")
    if MANIFEST.exists():
        if json.loads(MANIFEST.read_text()) != current:
            raise ValueError("candidate differs from freeze; review and explicitly refreeze")
    elif freeze:
        write_json(MANIFEST, current)
    else:
        raise ValueError("candidate is not frozen; run with --freeze after review")
    return current


def environment() -> dict[str, str]:
    """Exclude local agent configuration and interpreter overrides from gates."""
    excluded = {
        "PYTHONPATH",
        "PYTHONHOME",
        "VIRTUAL_ENV",
        "UV_PROJECT_ENVIRONMENT",
        "PYTEST_ADDOPTS",
        "COVERAGE_FILE",
        "COVERAGE_PROCESS_START",
    }
    result = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("FORGE_") and key not in excluded
    }
    result["PYTHONNOUSERSITE"] = "1"
    return result


class Verification:
    """Collect command outcomes and validation evidence for one frozen attempt."""

    def __init__(self, frozen: dict[str, Any]) -> None:
        identifier = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
        self.output = OUTPUT / "runs" / identifier
        self.work = OUTPUT / "work" / identifier
        self.output.mkdir(parents=True)
        self.work.mkdir(parents=True)
        self.frozen = frozen
        self.env = environment()
        self.checks: dict[str, dict[str, Any]] = {}
        self.summary: dict[str, Any] = {
            "schema_version": 1,
            "started_at_utc": datetime.now(UTC).isoformat(),
            "candidate_tree_digest": frozen["tree_digest"],
            "candidate_manifest": str(MANIFEST.relative_to(ROOT)),
            "candidate_manifest_sha256": hashlib.sha256(MANIFEST.read_bytes()).hexdigest(),
            "attempt_directory": str(self.output.relative_to(ROOT)),
            "work_directory": str(self.work.relative_to(ROOT)),
            "distribution_version": VERSION,
            "profile": "Python 3.12; local POSIX/SQLite; deterministic tests and keyless dev evals",
            "accepted_dependency_advisories": {
                "all_extras_only": [ACCEPTED_ADVISORY],
                "evidence": "docs/review/FINDINGS.json FH-019; Makefile gate",
            },
        }

    def run(self, name: str, command: list[str], cwd: Path) -> bool:
        """Run a bounded command with a separate log and no shell interpolation."""
        destination = self.output / (name + ".log")
        destination.parent.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        with destination.open("w") as log:
            try:
                result = subprocess.run(
                    command,
                    cwd=cwd,
                    env=self.env,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    timeout=600,
                    check=False,
                )
                code = result.returncode
            except subprocess.TimeoutExpired:
                log.write("\nVerification command timed out after 600 seconds.\n")
                code = 124
            except OSError as exc:
                log.write(f"\nUnable to execute command: {exc}\n")
                code = 127
        self.checks[name] = {
            "exit_code": code,
            "elapsed_seconds": round(time.monotonic() - started, 2),
            "command": command,
            "cwd": str(cwd),
            "log": str(destination.relative_to(ROOT)),
        }
        print(name, "PASS" if code == 0 else f"FAIL({code})", flush=True)
        return code == 0

    def assert_check(self, name: str, passed: bool, **details: object) -> None:
        self.checks[name] = {"exit_code": 0 if passed else 1, **details}
        print(name, "PASS" if passed else "FAIL", flush=True)

    def sync(self, work: Path, profile: str) -> bool:
        command = ["uv", "sync", "--python", "3.12", "--frozen"]
        for extra in EXTRAS:
            command.extend(("--extra", extra))
        return self.run(profile + "/sync", command, work)

    def quality(self, work: Path, profile: str) -> dict[str, Any]:
        report = self.output / profile
        report.mkdir(exist_ok=True)
        prefix = ["uv", "run", "--no-sync"]
        framework_files = sorted((work / "src/forgeharness/langchain_impl").glob("*.py"))
        commands = [
            # `--extend-exclude`, not `--exclude`: the latter replaces Ruff's
            # default excludes and would lint the whole virtual environment.
            (
                "format",
                [*prefix, "ruff", "format", "--check", ".", "--extend-exclude", "reports"],
            ),
            ("lint", [*prefix, "ruff", "check", ".", "--extend-exclude", "reports"]),
            ("types", [*prefix, "mypy"]),
            (
                "framework-types",
                prefix + ["mypy"] + [str(path.relative_to(work)) for path in framework_files],
            ),
            (
                "tests",
                [
                    *prefix,
                    "pytest",
                    "--cov=forgeharness",
                    "--cov-branch",
                    "--cov-fail-under=0",
                    "--cov-report=json:" + str(report / "coverage.json"),
                    "--junitxml=" + str(report / "tests.xml"),
                ],
            ),
            (
                "control",
                [*prefix, "forge", "eval-control", "--output", str(report / "control.json")],
            ),
            (
                "agent-dev",
                [
                    *prefix,
                    "forge",
                    "eval-agent",
                    "--split",
                    "dev",
                    "--output",
                    str(report / "agent-dev.json"),
                ],
            ),
            ("cli", [*prefix, "forge", "--help"]),
            ("comparison-cli", [*prefix, "forge", "framework-compare", "--help"]),
            ("lock", ["uv", "lock", "--check", "--offline"]),
            ("pip-check", ["uv", "pip", "check", "--python", str(work / ".venv/bin/python")]),
        ]
        commands.extend(
            (name.removesuffix(".py"), [*prefix, "python", "examples/" + name]) for name in DEMOS
        )
        for name, command in commands:
            self.run(profile + "/" + name, command, work)
        self.additional_coverage(work, profile, report)
        metrics = self.metrics(report, profile)
        self.audit_dependencies(work, profile)
        return metrics

    def additional_coverage(self, work: Path, profile: str, report: Path) -> None:
        """Optional implementation gates may append coverage before metrics."""

    def metrics(self, report: Path, profile: str) -> dict[str, Any]:
        """Parse fresh files only; missing evidence is a failed gate."""
        result: dict[str, Any] = {}
        try:
            totals = json.loads((report / "coverage.json").read_text())["totals"]
            branches = totals["num_branches"]
            pure = 100 * totals["covered_branches"] / branches if branches else 0.0
            result.update(coverage=totals, pure_branch_percent=pure)
            self.assert_check(profile + "/pure-branches", pure >= 85, percent=pure, threshold=85)
            self.assert_check(
                profile + "/combined-coverage",
                totals["percent_covered"] >= 85,
                percent=totals["percent_covered"],
                threshold=85,
            )
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self.assert_check(profile + "/pure-branches", False, error=str(exc))
            self.assert_check(profile + "/combined-coverage", False, error=str(exc))
        try:
            suites = list(ET.parse(report / "tests.xml").getroot().iter("testsuite"))
            counts = {
                key: sum(int(suite.attrib.get(key, "0")) for suite in suites)
                for key in ("tests", "failures", "errors", "skipped")
            }
            result["tests"] = counts
            self.assert_check(
                profile + "/test-evidence",
                counts["tests"] > 0 and all(counts[key] == 0 for key in counts if key != "tests"),
                **counts,
            )
        except (OSError, ValueError, ET.ParseError) as exc:
            self.assert_check(profile + "/test-evidence", False, error=str(exc))
        return result

    def audit_dependencies(self, work: Path, profile: str) -> None:
        report = self.output / profile
        for scope in ("core", "all"):
            requirements = report / (scope + "-requirements.txt")
            command = [
                "uv",
                "export",
                "--frozen",
                "--no-emit-project",
                "--format",
                "requirements-txt",
                "-o",
                str(requirements),
            ]
            command += ["--all-extras"] if scope == "all" else ["--no-dev"]
            if not self.run(profile + "/export-" + scope, command, work):
                continue
            audit = [
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
                str(report / (scope + "-dependencies.json")),
            ]
            if scope == "all":
                audit.extend(("--ignore-vuln", ACCEPTED_ADVISORY))
            self.run(profile + "/audit-" + scope, audit, work)

    def copy_candidate(self) -> Path:
        work = self.work / "candidate"
        work.mkdir()
        for name, digest in self.frozen["files"].items():
            relative = Path(name)
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("unsafe manifest path")
            source = ROOT / relative
            if not source.resolve().is_relative_to(ROOT) or source.is_symlink():
                raise ValueError("candidate source must remain within its workspace")
            target = work / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                raise ValueError(f"copy hash mismatch: {name}")
        (work / "reports").mkdir()
        if snapshot(work) != self.frozen:
            raise ValueError("clean candidate does not match the frozen source")
        self.assert_check("clean/source-identity", True, tree_digest=self.frozen["tree_digest"])
        return work

    def artifacts(self, work: Path) -> None:
        destination = self.output / "dist"
        if not self.run("build", ["uv", "build", "--out-dir", str(destination)], work):
            return
        wheel = destination / f"forgeharness-{VERSION}-py3-none-any.whl"
        sdist = destination / f"forgeharness-{VERSION}.tar.gz"
        self.check_archives(work, wheel, sdist)
        consumer = self.work / "consumer"
        consumer.mkdir()
        venv = consumer / ".venv"
        if not self.run("minimal/venv", ["uv", "venv", "--python", "3.12", str(venv)], consumer):
            return
        python = str(venv / "bin/python")
        if not self.run(
            "minimal/install", ["uv", "pip", "install", "--python", python, str(wheel)], consumer
        ):
            return
        check_import = (
            "import importlib.util, forgeharness; "
            f"assert forgeharness.__version__ == {VERSION!r}; "
            "assert importlib.util.find_spec('fastapi') is None; "
            "assert importlib.util.find_spec('langgraph') is None; "
            "import forgeharness.cloud.store; "
            "assert importlib.util.find_spec('asyncpg') is None; "
            "print(forgeharness.__file__)"
        )
        self.run("minimal/import", [python, "-c", check_import], consumer)
        for name in DEMOS:
            self.run("minimal/" + name, [python, str(work / "examples" / name)], consumer)
        self.summary["artifacts"] = {
            path.name: {
                "path": str(path.relative_to(ROOT)),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
            for path in (wheel, sdist)
        }

    def check_archives(self, work: Path, wheel: Path, sdist: Path) -> None:
        """Bind wheel sources and every non-generated sdist file to the freeze."""
        expected = {
            name.removeprefix("src/")
            for name in self.frozen["files"]
            if name.startswith("src/forgeharness/") and name.endswith(".py")
        }
        with zipfile.ZipFile(wheel) as archive:
            actual = {
                name
                for name in archive.namelist()
                if name.startswith("forgeharness/") and name.endswith(".py")
            }
            if actual != expected:
                raise ValueError("wheel source membership does not match the frozen candidate")
            for name in actual:
                if archive.read(name) != (work / "src" / name).read_bytes():
                    raise ValueError(f"wheel source mismatch: {name}")
        self.assert_check("build/wheel-source", True, matched_files=len(expected))
        # Top-level entries the sdist may contain. `.gitignore` is added by the
        # packaging tool by default and is part of the frozen identity, so it is
        # admitted here instead of being exempted from the check below.
        allowed = {
            "src",
            "docs",
            "examples",
            "README.md",
            "LICENSE",
            "pyproject.toml",
            ".gitignore",
        }
        included: set[str] = set()
        with tarfile.open(sdist) as archive:
            for member in archive.getmembers():
                name = Path(member.name)
                if (
                    name.is_absolute()
                    or ".." in name.parts
                    or name.parts[0] != f"forgeharness-{VERSION}"
                    or not (member.isfile() or member.isdir())
                ):
                    raise ValueError("unsafe sdist member")
                parts = name.parts[1:]
                if not parts or member.isdir():
                    continue
                if parts == ("PKG-INFO",):
                    continue
                relative = str(Path(*parts))
                if parts[0] not in allowed or relative not in self.frozen["files"]:
                    raise ValueError(f"sdist member is outside the frozen allowlist: {relative}")
                stream = archive.extractfile(member)
                if stream is None:
                    raise ValueError(f"sdist member is unreadable: {relative}")
                with stream:
                    digest = hashlib.sha256(stream.read()).hexdigest()
                if digest != self.frozen["files"][relative]:
                    raise ValueError(f"sdist source mismatch: {relative}")
                included.add(relative)
        required = {"examples/" + name for name in DEMOS}
        if not required.issubset(included):
            raise ValueError("sdist is missing a public demonstration")
        self.assert_check("build/sdist-allowlist", True, matched_files=len(included))

    def execute(self) -> bool:
        try:
            if self.sync(ROOT, "source"):
                self.summary["source"] = self.quality(ROOT, "source")
            work = self.copy_candidate()
            if self.sync(work, "clean"):
                self.summary["clean"] = self.quality(work, "clean")
                self.artifacts(work)
            self.assert_check("clean/source-unchanged", snapshot(work) == self.frozen)
        except Exception as exc:
            self.assert_check("driver", False, error=f"{type(exc).__name__}: {exc}")
        finally:
            try:
                unchanged = snapshot(ROOT) == self.frozen
                manifest_unchanged = json.loads(MANIFEST.read_text()) == self.frozen
            except (OSError, ValueError) as exc:
                # A tree that can no longer be read is by definition not unchanged.
                # Hashing must never discard the failure evidence it was collecting.
                detail = f"{type(exc).__name__}: {exc}"
                self.assert_check("source/source-unchanged", False, error=detail)
                self.assert_check("source/manifest-unchanged", False, error=detail)
                unchanged = manifest_unchanged = False
            else:
                self.assert_check("source/source-unchanged", unchanged)
                self.assert_check("source/manifest-unchanged", manifest_unchanged)
            self.summary.update(
                finished_at_utc=datetime.now(UTC).isoformat(),
                source_unchanged=unchanged,
                commands=self.checks,
                qualified=(
                    "source" in self.summary
                    and "clean" in self.summary
                    and "artifacts" in self.summary
                    and all(check["exit_code"] == 0 for check in self.checks.values())
                ),
            )
            write_json(self.output / "summary.json", self.summary)
            write_json(OUTPUT / "summary.json", self.summary)
        print("qualified", self.summary["qualified"], flush=True)
        return bool(self.summary["qualified"])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--freeze", action="store_true", help="freeze only; do not run gates")
    arguments = parser.parse_args()
    try:
        frozen = candidate(freeze=arguments.freeze)
    except (OSError, ValueError) as exc:
        parser.exit(2, f"Candidate rejected: {exc}\n")
    if arguments.freeze:
        print(f"candidate frozen: {frozen['tree_digest']}")
        return
    raise SystemExit(0 if Verification(frozen).execute() else 1)


if __name__ == "__main__":
    main()
