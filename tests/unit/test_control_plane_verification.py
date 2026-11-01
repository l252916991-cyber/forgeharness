"""A successful legacy suite cannot substitute for new control-plane evidence."""

import json
from pathlib import Path

import pytest

from benchmarks import control_plane_postgres as postgres
from benchmarks.control_plane_verification import CONSUMER_CHECKS, valid_consumer


@pytest.fixture
def consumer_evidence():
    return {
        "checks": dict.fromkeys(CONSUMER_CHECKS, True),
        "passed": True,
        "loaded_from_installation": True,
        "wheel_sha256": "a" * 64,
        "server_stopped": True,
        "external_requests": 0,
        "saas_qualified": False,
        "business_qualified": False,
        "browser": dict.fromkeys(
            (
                "passed",
                "invitation",
                "login",
                "management",
                "task",
                "independent_approval",
                "cancel",
                "revocation",
                "logout",
            ),
            True,
        )
        | {
            "engine": "Chromium",
            "browser_version": "149.0.0.0",
            "external_requests": 0,
            "reviewer_permissions": {"can_submit": False, "can_approve": True},
            "no_script_secret_protection": {"disabled_js": True, "blocked_script": True},
        },
        "approval_evidence": {"dual_role_owner_self_decisions_denied": True},
        "worker_pids": [1, 2, 3, 4, 5, 6],
        "worker_recoveries": [
            {"old_pid": 3, "new_pid": 4, "old_epoch": 2, "new_epoch": 3, "mode": "SIGSTOP/SIGCONT"},
            {"old_pid": 5, "new_pid": 6, "old_epoch": 2, "new_epoch": 3, "mode": "SIGKILL"},
        ],
    }


@pytest.mark.parametrize("missing", sorted(CONSUMER_CHECKS))
def test_installed_consumer_cannot_omit_a_required_boundary(consumer_evidence, missing):
    assert valid_consumer(consumer_evidence, "a" * 64)
    consumer_evidence["checks"].pop(missing)
    assert not valid_consumer(consumer_evidence, "a" * 64)


@pytest.mark.parametrize(
    "changes",
    [
        {"passed": False},
        {"loaded_from_installation": False},
        {"wheel_sha256": "b" * 64},
        {"server_stopped": False},
        {"external_requests": 1},
        {"external_requests": False},
        {"saas_qualified": True},
        {"business_qualified": True},
        {"checks": []},
        {"checks": dict.fromkeys(CONSUMER_CHECKS, 1)},
        {"browser": None},
        {"worker_pids": [1, 1, 1, 1, 1, 1]},
        {"worker_recoveries": []},
        {"approval_evidence": {}},
    ],
)
def test_source_import_wrong_wheel_unstopped_server_and_claim_inflation_fail(
    consumer_evidence, changes
):
    assert not valid_consumer(consumer_evidence | changes, "a" * 64)
    assert not valid_consumer(None, "a" * 64)


@pytest.mark.parametrize(
    "change", [{"new_epoch": 2}, {"old_pid": 4}, {"old_epoch": True}, {"mode": []}]
)
def test_process_recovery_requires_a_new_epoch_and_distinct_real_processes(
    consumer_evidence, change
):
    consumer_evidence["worker_recoveries"][0].update(change)
    assert not valid_consumer(consumer_evidence, "a" * 64)


@pytest.mark.parametrize(
    "permissions",
    [
        None,
        {"can_submit": True, "can_approve": True},
        {"can_submit": False, "can_approve": False},
        {"can_submit": 0, "can_approve": 1},
    ],
)
def test_browser_gate_requires_approval_only_reviewer_evidence(consumer_evidence, permissions):
    consumer_evidence["browser"]["reviewer_permissions"] = permissions
    assert not valid_consumer(consumer_evidence, "a" * 64)


@pytest.mark.parametrize(
    "protection", [None, {}, {"disabled_js": True}, {"disabled_js": True, "blocked_script": False}]
)
def test_browser_gate_requires_both_native_form_failure_modes(consumer_evidence, protection):
    consumer_evidence["browser"]["no_script_secret_protection"] = protection
    assert not valid_consumer(consumer_evidence, "a" * 64)


@pytest.mark.parametrize("new_nodes", [False, True])
@pytest.mark.parametrize("suite_passed", [False, True])
def test_postgres_gate_requires_old_and_new_suites(tmp_path, monkeypatch, new_nodes, suite_passed):
    def old_execute(output: Path, *, coverage_output, test_paths):
        assert test_paths == ("evals/saas_postgres/tests", "evals/saas_control_plane/tests")
        output.mkdir()
        (output / "summary.json").write_text(json.dumps({"passed": suite_passed}))
        case = '<testcase classname="evals.saas_postgres.tests.test_old" name="old"/>'
        if new_nodes:
            case += '<testcase classname="evals.saas_control_plane.tests.test_http" name="new"/>'
        (output / "tests.xml").write_text(
            "<testsuites><testsuite>" + case + "</testsuite></testsuites>"
        )
        return suite_passed

    monkeypatch.setattr(postgres.saas_postgres, "execute", old_execute)
    assert postgres.execute(tmp_path / "out") is (new_nodes and suite_passed)
    value = json.loads((tmp_path / "out/summary.json").read_text())
    assert value["control_plane_suite"] is new_nodes
    assert len(value["control_plane_nodes"]) == int(new_nodes)


def test_control_postgres_error_or_skip_cannot_be_called_passed(tmp_path, monkeypatch):
    def old_execute(output, **kwargs):
        output.mkdir()
        (output / "summary.json").write_text('{"passed":true}')
        (output / "tests.xml").write_text(
            '<testsuites><testsuite><testcase classname="evals.saas_control_plane.tests.test_http" '
            'name="skipped"><skipped/></testcase></testsuite></testsuites>'
        )
        return True

    monkeypatch.setattr(postgres.saas_postgres, "execute", old_execute)
    assert not postgres.execute(tmp_path / "out")


def test_control_wheel_consumer_rejects_source_imports_before_database_creation(
    tmp_path, monkeypatch
):
    import sys

    from evals.saas_control_plane import consumer

    monkeypatch.setattr(sys, "prefix", str(tmp_path / "isolated-wheel-environment"))
    monkeypatch.setattr(sys, "argv", ["control-consumer", "--require-installed"])
    original = consumer.importlib.import_module

    def reject_server(name):
        assert name != "embedded_postgres", "source import must fail before provisioning"
        return original(name)

    monkeypatch.setattr(consumer.importlib, "import_module", reject_server)
    with pytest.raises(SystemExit) as error:
        consumer.main()
    assert error.value.code == 2
