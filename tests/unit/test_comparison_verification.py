import json
import sys
from pathlib import Path

import pytest

from benchmarks import session_verification as base
from benchmarks.comparison import verify
from benchmarks.comparison.verify import valid_comparison, valid_postgres
from benchmarks.saas_postgres import MIN_POSTGRES_TESTS


@pytest.mark.parametrize(
    "failing_step",
    [
        None,
        "web-consumer/install",
        "web-consumer/workbench",
        "web-consumer/cli",
        "cloud-consumer/venv",
        "cloud-consumer/install",
        "cloud-consumer/state",
        "cloud-consumer/hosted",
    ],
)
def test_optional_web_consumer_failure_blocks_candidate(
    candidate_workspace, monkeypatch, failing_step
):
    verify.configure_candidate(Path("reports/web-contract"))
    frozen = base.candidate(freeze=True)
    attempt = verify.ComparisonVerification(frozen)

    def base_artifacts(self, work):
        self.summary["artifacts"] = {f"forgeharness-{base.VERSION}-py3-none-any.whl": {}}

    def command(name, args, cwd):
        if name == "cloud-consumer/hosted":
            assert args == [
                str(attempt.work / "cloud-consumer/.venv/bin/python"),
                str(candidate_workspace / "examples/cloud_hosted_demo.py"),
                "--require-installed",
            ]
            assert cwd == attempt.work / "cloud-consumer"
        code = int(name == failing_step)
        attempt.checks[name] = {"exit_code": code}
        return code == 0

    monkeypatch.setattr(verify.business.BusinessVerification, "artifacts", base_artifacts)
    monkeypatch.setattr(attempt, "sync", lambda *args: True)
    monkeypatch.setattr(attempt, "quality", lambda *args: {})
    monkeypatch.setattr(attempt, "copy_candidate", lambda: candidate_workspace)
    monkeypatch.setattr(attempt, "run", command)
    assert attempt.execute() is (failing_step is None)


def test_hosted_wheel_consumer_rejects_source_import_before_provisioning(tmp_path, monkeypatch):
    from examples import cloud_hosted_demo

    monkeypatch.setattr(sys, "prefix", str(tmp_path / "isolated-venv"))
    monkeypatch.setattr(sys, "argv", ["cloud-hosted-demo", "--require-installed"])
    original = cloud_hosted_demo.importlib.import_module

    def no_server(name):
        assert name != "embedded_postgres", "source imports must fail before creating a database"
        return original(name)

    monkeypatch.setattr(cloud_hosted_demo.importlib, "import_module", no_server)
    with pytest.raises(SystemExit) as error:
        cloud_hosted_demo.main()
    assert error.value.code == 2


@pytest.mark.parametrize(
    "change",
    [
        {"source_identity": "other"},
        {"protocol_sha256": "other"},
        {"passed": 32},
        {"not_measured": 1},
        {"business_qualified": True},
        {"core_advantage_proven": True},
        {"source_unchanged": False},
        {"native_full_fault_comparison_complete": True},
        {"independent_onboarding_measured": True},
        {"decision": "custom_core_wins"},
        {"failed": 1},
        {"model_input_evidence_complete": False},
    ],
)
def test_release_rejects_drift_missing_results_and_unsupported_claims(change):
    result = {
        "source_identity": "source",
        "protocol_sha256": "protocol",
        "source_unchanged": True,
        "integration_gate_passed": True,
        "expected_results": 33,
        "passed": 33,
        "failed": 0,
        "not_measured": 0,
        "business_qualified": False,
        "core_advantage_proven": False,
        "native_full_fault_comparison_complete": False,
        "independent_onboarding_measured": False,
        "model_input_evidence_complete": True,
        "decision": "insufficient_evidence_for_native_core_advantage",
    }
    assert valid_comparison(result, "source", "protocol")
    assert not valid_comparison(result | change, "source", "protocol")


@pytest.mark.parametrize(
    "change",
    [
        {"passed": False},
        {"source_unchanged": False},
        {"source_identity": "other"},
        {"schema_sha256": "other"},
        {"server_version": "16.2"},
        {"server_stopped": False},
        {"business_qualified": True},
        {"saas_qualified": True},
        {"network": "TCP"},
        {"tests": None},
        {"tests": []},
        {"tests": {"tests": MIN_POSTGRES_TESTS, "failures": False, "errors": 0, "skipped": 0}},
        {"tests": {"tests": MIN_POSTGRES_TESTS - 1, "failures": 0, "errors": 0, "skipped": 0}},
        {"tests": {"tests": MIN_POSTGRES_TESTS, "failures": 1, "errors": 0, "skipped": 0}},
        {"tests": {"tests": MIN_POSTGRES_TESTS, "failures": 0, "errors": 1, "skipped": 0}},
        {"tests": {"tests": MIN_POSTGRES_TESTS, "failures": 0, "errors": 0, "skipped": 1}},
    ],
)
def test_postgres_gate_requires_real_complete_bound_and_stopped_server(change):
    result = {
        "passed": True,
        "source_unchanged": True,
        "source_identity": "source",
        "schema_sha256": "schema",
        "server_version": "18.6",
        "server_stopped": True,
        "business_qualified": False,
        "saas_qualified": False,
        "network": "private Unix socket",
        "tests": {"tests": MIN_POSTGRES_TESTS, "failures": 0, "errors": 0, "skipped": 0},
    }
    assert valid_postgres(result, "source", "schema")
    assert not valid_postgres(result | change, "source", "schema")


@pytest.fixture
def candidate_workspace(tmp_path, monkeypatch):
    for name in ("OUTPUT", "MANIFEST", "VERSION", "DEMOS", "EXTRAS"):
        monkeypatch.setattr(base, name, getattr(base, name))
    monkeypatch.setattr(base, "ROOT", tmp_path)
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "0.6.0"\n')
    examples = tmp_path / "examples"
    examples.mkdir()
    for name in (
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
    ):
        (examples / name).write_text('print("fixture")\n')
    return tmp_path


def test_current_candidate_preserves_old_manifest_and_rejects_drift(candidate_workspace):
    old = candidate_workspace / "reports/next-candidate/source.json"
    old.parent.mkdir(parents=True)
    old.write_text('{"old": true}\n')
    verify.configure_candidate(Path("reports/framework-next"))
    frozen = base.candidate(freeze=True)
    assert old.read_text() == '{"old": true}\n'
    assert base.OUTPUT == candidate_workspace / "reports/framework-next/verification"
    assert base.candidate(freeze=False) == frozen
    manifest = base.MANIFEST.read_bytes()
    (candidate_workspace / "README.md").write_text("changed source\n")
    for freeze in (False, True):
        with pytest.raises(ValueError, match="differs from freeze"):
            base.candidate(freeze=freeze)
    assert base.MANIFEST.read_bytes() == manifest


@pytest.mark.parametrize("directory", ["reports", ".", "../outside", "/tmp/outside"])
def test_candidate_output_cannot_escape_reports(candidate_workspace, directory):
    with pytest.raises(ValueError, match="subdirectory of workspace reports"):
        verify.configure_candidate(Path(directory))


def test_candidate_output_cannot_escape_through_symlink(candidate_workspace):
    reports = candidate_workspace / "reports"
    reports.mkdir()
    (reports / "escape").symlink_to(candidate_workspace, target_is_directory=True)
    with pytest.raises(ValueError, match="subdirectory of workspace reports"):
        verify.configure_candidate(Path("reports/escape/output"))


def test_freeze_only_never_starts_verification(candidate_workspace, monkeypatch, capsys):
    monkeypatch.setattr(
        sys, "argv", ["verify", "--candidate-dir", "reports/framework-next", "--freeze-only"]
    )

    def forbidden(frozen):
        pytest.fail("freeze-only must not create a verification attempt")

    monkeypatch.setattr(verify, "ComparisonVerification", forbidden)
    verify.main()
    result = json.loads(capsys.readouterr().out)
    assert result["source_identity"]
    assert Path(result["manifest"]) == candidate_workspace / "reports/framework-next/source.json"
    assert not base.OUTPUT.exists()


def test_verification_requires_explicit_freeze(candidate_workspace, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["verify", "--candidate-dir", "reports/framework-next"])
    with pytest.raises(SystemExit) as error:
        verify.main()
    assert error.value.code == 2
    assert not base.MANIFEST.exists()


@pytest.mark.parametrize("passed", [True, False])
def test_cli_verifies_frozen_candidate_and_propagates_failure(
    candidate_workspace, monkeypatch, capsys, passed
):
    verify.configure_candidate(Path("reports/framework-next"))
    frozen = base.candidate(freeze=True)

    class Attempt:
        def __init__(self, candidate):
            assert candidate == frozen

        def execute(self):
            return passed

    monkeypatch.setattr(verify, "ComparisonVerification", Attempt)
    monkeypatch.setattr(sys, "argv", ["verify", "--candidate-dir", "reports/framework-next"])
    with pytest.raises(SystemExit) as error:
        verify.main()
    assert error.value.code == (0 if passed else 1)
    result = json.loads(capsys.readouterr().out)
    assert result["engineering_qualified"] is passed
    assert result["business_qualified"] is False


def test_legacy_default_still_freezes_in_its_original_directory(
    candidate_workspace, monkeypatch, capsys
):
    monkeypatch.setattr(sys, "argv", ["verify", "--freeze-only"])
    verify.main()
    capsys.readouterr()
    assert base.MANIFEST == candidate_workspace / "reports/next-candidate/source.json"
    assert base.MANIFEST.exists()
