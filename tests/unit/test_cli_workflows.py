"""CLI contracts, failure exit codes, persisted inspection, and scoped repair."""

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from forgeharness import cli
from forgeharness.domain.models import (
    FinalAction,
    ModelResult,
    RunResult,
    RunStatus,
    ToolAction,
    ToolCall,
    Usage,
)
from forgeharness.models.scripted import ScriptedModel
from forgeharness.observability.hash_chain import HashChainedJSONLTrace
from forgeharness.state.checkpoint import SQLiteCheckpointStore


@pytest.mark.parametrize("qualified", [True, False])
@pytest.mark.parametrize(
    ("command", "function"),
    [
        ("qualify-omlx", "run_omlx_qualification"),
        ("eval-rag", "run_rag_evaluation"),
        ("bench-retrieval", "run_retrieval_benchmark"),
        ("eval-control", "run_control_evaluation"),
    ],
)
def test_eval_commands_propagate_failure_exit_codes(
    qualified: bool, command: str, function: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []

    async def evaluate(**kwargs: Any) -> SimpleNamespace:
        calls.append(kwargs)
        return SimpleNamespace(
            qualified=qualified,
            total=60,
            passed=60 if qualified else 0,
            revision="test",
            sections={"tool": SimpleNamespace(passed=20, total=20, median_latency_ms=1)},
            fatal_error=None if qualified else "test failure",
            recall_at_5=1,
            mrr_at_10=1,
            citation_precision=1,
            unsupported_answer_rate=0,
            chunks=5000,
            requests=100,
            retrieval_p50_ms=1,
            retrieval_p95_ms=2,
        )

    monkeypatch.setattr(cli, function, evaluate)
    result = CliRunner().invoke(cli.app, [command, "--output", str(tmp_path / "report.json")])
    assert result.exit_code == (0 if qualified else (1 if command == "eval-control" else 2))
    assert calls[0]["output_path"] == tmp_path / "report.json"


def test_reviewer_and_serve_commands(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def evaluate(**kwargs: Any) -> SimpleNamespace:
        arm = SimpleNamespace(effective_answer_rate=1)
        return SimpleNamespace(
            baseline=arm,
            reviewer=arm,
            quality_improvement_points=0,
            latency_ratio=1,
            reviewer_enabled_by_default=False,
        )

    monkeypatch.setattr(cli, "run_reviewer_experiment", evaluate)
    runner = CliRunner()
    assert "reviewer_default=false" in runner.invoke(cli.app, ["eval-reviewer"]).stdout
    seen: dict[str, Any] = {}
    monkeypatch.setattr(cli.uvicorn, "run", lambda app, **kwargs: seen.update(kwargs))
    assert runner.invoke(cli.app, ["serve", "--data-dir", str(tmp_path)]).exit_code == 0
    assert seen == {"host": "127.0.0.1", "port": 8001}


def test_inspection_reports_missing_and_existing_traces(tmp_path: Path) -> None:
    runner = CliRunner()
    arguments = ["inspect-run", "sample", "--data-dir", str(tmp_path)]
    assert runner.invoke(cli.app, arguments).exit_code == 1
    result = RunResult(
        task_id="sample",
        status=RunStatus.SUCCEEDED,
        messages=(),
        usage=Usage(),
        final_output="done",
    )
    SQLiteCheckpointStore(tmp_path / "runs.sqlite3").save(result)
    assert "trace_valid" not in runner.invoke(cli.app, arguments).stdout
    HashChainedJSONLTrace(tmp_path / "traces/sample.jsonl", "sample").append("run.finished")
    assert "trace_valid=True" in runner.invoke(cli.app, arguments).stdout


@pytest.mark.parametrize("success", [True, False])
def test_repair_command_preserves_result_and_exit_status(
    success: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def repair(**kwargs: Any) -> RunResult:
        return RunResult(
            task_id="run",
            status=RunStatus.SUCCEEDED if success else RunStatus.FAILED,
            messages=(),
            usage=Usage(),
            final_output="done" if success else None,
            error=None if success else "failed",
        )

    monkeypatch.setattr(cli, "_repair", repair)
    command = ["repair", str(tmp_path), "fix", "--model", "fake", "--api-key", "local-test"]
    result = CliRunner().invoke(cli.app, command)
    assert result.exit_code == (0 if success else 2)
    assert CliRunner().invoke(cli.app, [*command, "--test-command", ""]).exit_code != 0


@pytest.mark.parametrize(
    ("approve_all", "confirmation"), [(True, False), (False, False), (False, True)]
)
async def test_cli_repair_requires_confirmation_before_each_write(
    approve_all: bool, confirmation: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".git").mkdir()
    model = ScriptedModel(
        [
            ModelResult(
                action=ToolAction(
                    call=ToolCall(
                        id="write",
                        name="write_file",
                        arguments={"path": "new.txt", "content": "ok"},
                    )
                )
            ),
            ModelResult(
                action=ToolAction(call=ToolCall(id="tests", name="run_tests", arguments={}))
            ),
            ModelResult(action=FinalAction(content="done")),
        ]
    )
    monkeypatch.setattr(cli, "OpenAICompatibleModel", lambda *args, **kwargs: model)
    monkeypatch.setattr(cli.typer, "confirm", lambda message: confirmation)
    result = await cli._repair(
        workspace=tmp_path,
        issue="write",
        model_name="fake",
        api_key="local-test",
        base_url="http://127.0.0.1:1/v1",
        test_command=("true",),
        approve_all=approve_all,
    )
    assert (tmp_path / "new.txt").exists() == (approve_all or confirmation)
    assert result.status == (
        RunStatus.SUCCEEDED if approve_all or confirmation else RunStatus.AWAITING_APPROVAL
    )
