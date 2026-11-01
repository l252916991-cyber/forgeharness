import sys

import pytest

from benchmarks import improvement_hosts as hosts
from benchmarks.improvement_demo import demo


def test_host_and_demo_state_cannot_escape_workspace(tmp_path):
    with pytest.raises(ValueError, match="workspace reports"):
        hosts.run(tmp_path, tmp_path, tmp_path / "python", tmp_path / "outside")
    with pytest.raises(ValueError, match="workspace reports"):
        demo(tmp_path / "outside")


def test_process_exit_is_checked_against_expected_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(hosts, "ROOT", tmp_path)
    log = tmp_path / "run.log"
    row = hosts.command([sys.executable, "-c", "import os; os._exit(91)"], tmp_path, {}, log, 91)
    assert row["passed"] and row["exit_code"] == 91
    wrong = hosts.command([sys.executable, "-c", "raise SystemExit(0)"], tmp_path, {}, log, 91)
    assert not wrong["passed"]


def test_host_code_identity_detects_changes_without_copying_contents(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "tests").mkdir()
    file = tmp_path / "app/demo.py"
    file.write_text("print('first')")
    before = hosts.code_identity(tmp_path, "app")
    file.write_text("print('second')")
    assert hosts.code_identity(tmp_path, "app") != before
    assert len(before["app/demo.py"]) == 64


def test_framework_python_keeps_venv_symlink_instead_of_base_interpreter(tmp_path):
    python = tmp_path / "venv/bin/python"
    python.parent.mkdir(parents=True)
    python.symlink_to(sys.executable)
    selected = hosts.framework_interpreter(python)
    assert selected == str(python.absolute())
    assert selected != str(python.resolve())
