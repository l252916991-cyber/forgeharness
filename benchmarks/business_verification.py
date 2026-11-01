"""Freeze and qualify the current synthetic candidate; never run the sealed set.

Uses the session release driver's clean-copy, coverage, audit and minimal-install
checks, with additional development contracts, safety selection and data assets.
Old session freezes and reports are preserved.
"""

import argparse
import hashlib
import json
import tarfile
import tomllib
import zipfile
from pathlib import Path
from typing import Any

from benchmarks import session_verification as base


class BusinessVerification(base.Verification):
    def quality(self, work: Path, profile: str) -> dict[str, Any]:
        metrics = super().quality(work, profile)
        report = self.output / profile
        self.run(
            profile + "/business-dev",
            [
                "uv",
                "run",
                "--no-sync",
                "python",
                "-m",
                "benchmarks.business_evaluation",
                "--output",
                str(report / "business-dev.json"),
            ],
            work,
        )
        try:
            result = json.loads((report / "business-dev.json").read_text())
            valid = (
                result["passed"] is True
                and result["measurement_valid"] is False
                and result["business_qualified"] is False
                and result["legal_cases"] == 40
                and result["commerce_cases"] == 80
                and result["source_identity"] == self.frozen["tree_digest"]
            )
            self.assert_check(profile + "/business-contracts", valid)
            metrics["business_contracts"] = {
                key: result[key]
                for key in (
                    "case_count",
                    "families",
                    "passed",
                    "measurement_valid",
                    "business_qualified",
                )
            }
        except (OSError, ValueError, KeyError) as exc:
            self.assert_check(profile + "/business-contracts", False, error=str(exc))
        nodes = json.loads((work / "evals/business/safety.json").read_text())["nodeids"]
        self.assert_check(profile + "/safety-selection", len(set(nodes)) == len(nodes) >= 40)
        self.run(profile + "/business-safety", ["uv", "run", "--no-sync", "pytest", *nodes], work)
        return metrics

    def check_archives(self, work: Path, wheel: Path, sdist: Path) -> None:
        expected = {
            name.removeprefix("src/")
            for name in self.frozen["files"]
            if name.startswith("src/forgeharness/")
        }
        with zipfile.ZipFile(wheel) as archive:
            actual = {
                n
                for n in archive.namelist()
                if n.startswith("forgeharness/") and not n.endswith("/")
            }
            if actual != expected:
                raise ValueError("wheel code and asset membership differs from freeze")
            for name in actual:
                if (
                    hashlib.sha256(archive.read(name)).hexdigest()
                    != self.frozen["files"]["src/" + name]
                ):
                    raise ValueError("wheel source or asset changed: " + name)
        self.assert_check("build/wheel-source", True, matched_files=len(expected))
        included = set()
        allowed = {
            "src",
            "docs",
            "examples",
            "data",
            "README.md",
            "LICENSE",
            "pyproject.toml",
            ".gitignore",
        }
        with tarfile.open(sdist) as archive:
            for member in archive.getmembers():
                path = Path(member.name)
                if (
                    path.is_absolute()
                    or ".." in path.parts
                    or path.parts[0] != f"forgeharness-{base.VERSION}"
                    or not (member.isfile() or member.isdir())
                ):
                    raise ValueError("unsafe sdist member")
                parts = path.parts[1:]
                if not parts or member.isdir() or parts == ("PKG-INFO",):
                    continue
                relative = str(Path(*parts))
                if (
                    parts[0] not in allowed
                    or relative not in self.frozen["files"]
                    or (parts[0] == "data" and parts[1] != "synthetic")
                ):
                    raise ValueError("sdist member outside frozen public inputs: " + relative)
                stream = archive.extractfile(member)
                if stream is None:
                    raise ValueError("unreadable sdist member")
                with stream:
                    if hashlib.sha256(stream.read()).hexdigest() != self.frozen["files"][relative]:
                        raise ValueError("sdist source changed: " + relative)
                included.add(relative)
        required = {f"examples/{name}" for name in base.DEMOS} | {
            n for n in self.frozen["files"] if n.startswith("data/synthetic/")
        }
        if not required.issubset(included):
            raise ValueError("sdist lacks runnable examples or their synthetic data")
        self.assert_check("build/sdist-allowlist", True, matched_files=len(included))


def configure() -> None:
    base.OUTPUT = base.ROOT / "reports/business-verification"
    base.MANIFEST = base.ROOT / "reports/business-candidate-source.json"
    base.VERSION = tomllib.loads((base.ROOT / "pyproject.toml").read_text())["project"]["version"]
    base.EXTRAS = ("dev", "cli", "platform", "benchmark", "frameworks", "postgres-test")
    base.DEMOS = (
        "sdk_demo.py",
        "business_modules_demo.py",
        "session_demo.py",
        "import_tasks_demo.py",
        "legal_pilot_demo.py",
        "commerce_demo.py",
        "commerce_readonly_demo.py",
        "durable_demo.py",
        "collaboration_demo.py",
        "parallel_demo.py",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--freeze", action="store_true")
    args = parser.parse_args()
    configure()
    frozen = base.candidate(freeze=args.freeze)
    verification = BusinessVerification(frozen)
    passed = verification.execute()
    print(
        json.dumps(
            {"qualified": passed, "summary": str(base.OUTPUT / "summary.json")}, ensure_ascii=False
        )
    )
    raise SystemExit(0 if passed else 1)


if __name__ == "__main__":
    main()
