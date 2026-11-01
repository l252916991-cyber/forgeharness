"""Release gates reject missing evidence, source drift, and incomplete artifacts."""

import hashlib
import io
import json
import tarfile
import zipfile
from pathlib import Path

import pytest

from benchmarks import session_verification as gate


@pytest.fixture
def verifier(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> gate.Verification:
    for name in (
        "AGENTS.md",
        "LICENSE",
        "README.md",
        "uv.lock",
        "Makefile",
        ".python-version",
        ".env.example",
        "CHANGELOG.md",
    ):
        (tmp_path / name).write_text(name + "\n")
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "0.4.0"\n')
    for name in gate.DEMOS:
        destination = tmp_path / "examples" / name
        destination.parent.mkdir(exist_ok=True)
        destination.write_text('print("synthetic example")\n')
    source = tmp_path / "src/forgeharness"
    source.mkdir(parents=True)
    (source / "__init__.py").write_text('__version__ = "0.4.0"\n')
    (source / "session.py").write_text('"""Synthetic package member."""\n')
    monkeypatch.setattr(gate, "ROOT", tmp_path)
    monkeypatch.setattr(gate, "OUTPUT", tmp_path / "reports/session-verification")
    monkeypatch.setattr(gate, "MANIFEST", tmp_path / "reports/session-candidate-source.json")
    return gate.Verification(gate.candidate(freeze=True))


def passing_metrics(path: Path, *, covered_branches: int = 100) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "coverage.json").write_text(
        json.dumps(
            {
                "totals": {
                    "num_branches": 100,
                    "covered_branches": covered_branches,
                    "percent_covered": 99.8,
                }
            }
        )
    )
    (path / "tests.xml").write_text(
        '<testsuites><testsuite tests="5" failures="0" errors="0" skipped="0"/></testsuites>'
    )


def test_changed_candidate_is_rejected_without_replacing_freeze(
    verifier: gate.Verification,
) -> None:
    original = gate.MANIFEST.read_bytes()
    (gate.ROOT / "README.md").write_text("changed after freeze\n")
    for freeze in (False, True):
        with pytest.raises(ValueError, match="differs from freeze"):
            gate.candidate(freeze=freeze)
    assert gate.MANIFEST.read_bytes() == original
    assert hashlib.sha256(original).hexdigest() == verifier.summary["candidate_manifest_sha256"]


def test_new_attempt_does_not_borrow_previous_success(verifier: gate.Verification) -> None:
    passing_metrics(verifier.output / "source")
    verifier.metrics(verifier.output / "source", "source")
    assert all(check["exit_code"] == 0 for check in verifier.checks.values())
    later = gate.Verification(verifier.frozen)

    metrics = later.metrics(later.output / "source", "source")

    assert later.output != verifier.output
    assert metrics == {}
    assert later.checks["source/pure-branches"]["exit_code"] == 1
    assert later.checks["source/test-evidence"]["exit_code"] == 1


def test_high_combined_coverage_does_not_replace_pure_branch_gate(
    verifier: gate.Verification,
) -> None:
    passing_metrics(verifier.output / "source", covered_branches=84)

    metrics = verifier.metrics(verifier.output / "source", "source")

    assert metrics["coverage"]["percent_covered"] == 99.8
    assert verifier.checks["source/pure-branches"]["exit_code"] == 1
    assert verifier.checks["source/test-evidence"]["exit_code"] == 0


def test_passing_branches_do_not_replace_combined_coverage_gate(verifier):
    report = verifier.output / "source"
    passing_metrics(report)
    value = json.loads((report / "coverage.json").read_text())
    value["totals"]["percent_covered"] = 84.9
    (report / "coverage.json").write_text(json.dumps(value))
    verifier.metrics(report, "source")
    assert verifier.checks["source/pure-branches"]["exit_code"] == 0
    assert verifier.checks["source/combined-coverage"]["exit_code"] == 1


def make_wheel(path: Path, *, omit: str | None = None) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        for source in (gate.ROOT / "src/forgeharness").glob("*.py"):
            name = "forgeharness/" + source.name
            if name != omit:
                archive.writestr(name, source.read_bytes())


def test_wheel_cannot_omit_frozen_package_source(verifier: gate.Verification) -> None:
    wheel = verifier.output / "missing.whl"
    make_wheel(wheel, omit="forgeharness/session.py")

    with pytest.raises(ValueError, match="source membership"):
        verifier.check_archives(gate.ROOT, wheel, verifier.output / "unused.tar.gz")


def test_sdist_cannot_include_an_unfrozen_file(verifier: gate.Verification) -> None:
    wheel = verifier.output / "complete.whl"
    make_wheel(wheel)
    sdist = verifier.output / "extra.tar.gz"
    with tarfile.open(sdist, "w:gz") as archive:
        data = b"unreviewed source\n"
        member = tarfile.TarInfo("forgeharness-0.4.0/docs/unfrozen.md")
        member.size = len(data)
        archive.addfile(member, io.BytesIO(data))

    with pytest.raises(ValueError, match="outside the frozen allowlist"):
        verifier.check_archives(gate.ROOT, wheel, sdist)


@pytest.mark.parametrize("remove_source", [False, True])
def test_driver_exception_persists_failed_result(
    verifier: gate.Verification,
    monkeypatch: pytest.MonkeyPatch,
    remove_source: bool,
) -> None:
    def fail_sync(work: Path, profile: str) -> bool:
        if remove_source:
            (work / "README.md").unlink()
        raise RuntimeError(f"{profile} unavailable")

    monkeypatch.setattr(verifier, "sync", fail_sync)
    gate.write_json(gate.OUTPUT / "summary.json", {"qualified": True, "old": True})

    assert verifier.execute() is False

    persisted = json.loads((gate.OUTPUT / "summary.json").read_text())
    assert persisted["qualified"] is False
    assert persisted["commands"]["driver"]["exit_code"] == 1
    assert "RuntimeError" in persisted["commands"]["driver"]["error"]
    assert persisted["candidate_tree_digest"] == verifier.frozen["tree_digest"]
    assert "old" not in persisted
