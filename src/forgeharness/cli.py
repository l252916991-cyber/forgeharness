"""Command-line entry point for local Harness workflows."""

from __future__ import annotations

import asyncio
import json
import shlex
from pathlib import Path
from uuid import uuid4

import httpx
import typer
import uvicorn

from forgeharness.api import create_app
from forgeharness.coding.agent import CodingAgent
from forgeharness.domain.models import (
    FinalAction,
    ModelResult,
    RunResult,
    RunStatus,
    ToolAction,
    ToolCall,
)
from forgeharness.evaluation.control import run_control_evaluation
from forgeharness.models.openai_compatible import (
    OpenAICompatibleConfig,
    OpenAICompatibleModel,
)
from forgeharness.models.scripted import ScriptedModel
from forgeharness.observability.hash_chain import HashChainedJSONLTrace, verify_trace
from forgeharness.observability.trace import InMemoryTrace
from forgeharness.runtime.loop import AgentRuntime
from forgeharness.state.approval import InMemoryApprovalLedger
from forgeharness.state.checkpoint import SQLiteCheckpointStore
from forgeharness.tools.builtin import EchoTool
from forgeharness.tools.dispatcher import ToolDispatcher
from forgeharness.tools.policy import RiskBasedPolicy
from forgeharness.tools.registry import ToolRegistry

app = typer.Typer(no_args_is_help=True)


@app.callback()
def main() -> None:
    """Run, evaluate, and inspect ForgeHarness workflows."""


@app.command()
def demo(text: str = "ForgeHarness is running") -> None:
    """Run a deterministic, keyless model/tool/final-answer scenario."""
    result, events = asyncio.run(_demo(text))
    typer.echo(f"status={result.status.value}")
    typer.echo(f"output={result.final_output}")
    typer.echo(f"steps={result.usage.steps} tool_calls={result.usage.tool_calls}")
    typer.echo(f"trace_events={events}")


@app.command("eval-control")
def evaluate_control(
    manifest: Path = Path("evals/control_cases.json"),
    output: Path = Path("reports/control-eval.json"),
) -> None:
    """Run the keyless control suite and write a reproducible JSON report."""
    report = asyncio.run(
        run_control_evaluation(
            manifest_path=manifest,
            output_path=output,
            project_root=Path.cwd(),
        )
    )
    typer.echo(f"control_eval={report.passed}/{report.total}")
    typer.echo(f"revision={report.revision}")
    typer.echo(f"report={output.resolve()}")
    if report.passed != report.total:
        raise typer.Exit(code=1)


@app.command()
def repair(
    workspace: Path,
    issue: str,
    model: str = typer.Option(..., help="Provider model identifier."),
    api_key: str = typer.Option(..., envvar="FORGE_API_KEY", help="Provider API key."),
    base_url: str = typer.Option(
        "https://api.openai.com/v1", help="OpenAI-compatible API base URL."
    ),
    test_command: str = typer.Option("python -m pytest -q", help="Fixed test argv."),
    yes: bool = typer.Option(False, "--yes", help="Approve each exact workspace write."),
) -> None:
    """Run an approval-gated issue-to-tested-patch workflow in a Git repository."""
    command = tuple(shlex.split(test_command))
    if not command:
        raise typer.BadParameter("test command must not be empty", param_hint="--test-command")
    result = asyncio.run(
        _repair(
            workspace=workspace.resolve(),
            issue=issue,
            model_name=model,
            api_key=api_key,
            base_url=base_url,
            test_command=command,
            approve_all=yes,
        )
    )
    _print_result(result)
    if result.status != RunStatus.SUCCEEDED:
        raise typer.Exit(code=2)


@app.command("inspect-run")
def inspect_run(
    task_id: str,
    data_dir: Path = Path(".forgeharness"),
) -> None:
    """Print a persisted checkpoint and verify its hash-chained trace."""
    result = SQLiteCheckpointStore(data_dir / "runs.sqlite3").load(task_id)
    if result is None:
        typer.echo(f"run not found: {task_id}", err=True)
        raise typer.Exit(code=1)
    typer.echo(result.model_dump_json(indent=2))
    trace_path = data_dir / "traces" / f"{task_id}.jsonl"
    if trace_path.is_file():
        verification = verify_trace(trace_path)
        typer.echo(f"trace_valid={verification.valid} trace_events={verification.events}")


@app.command()
def serve(
    data_dir: Path = Path(".forgeharness"),
    host: str = "127.0.0.1",
    port: int = typer.Option(8000, min=1, max=65_535),
) -> None:
    """Serve the local run and trace-inspection API."""
    uvicorn.run(create_app(data_dir), host=host, port=port)


async def _demo(text: str) -> tuple[RunResult, int]:
    task_id = "demo"
    registry = ToolRegistry()
    registry.register(EchoTool())
    trace = InMemoryTrace(task_id)
    model = ScriptedModel(
        [
            ModelResult(
                action=ToolAction(
                    call=ToolCall(id="demo-1", name="echo", arguments={"text": text})
                ),
                model_name="scripted",
            ),
            ModelResult(
                action=FinalAction(content=f"Echo verified: {text}"), model_name="scripted"
            ),
        ]
    )
    runtime = AgentRuntime(
        model=model,
        registry=registry,
        dispatcher=ToolDispatcher(registry),
        policy=RiskBasedPolicy(),
        trace=trace,
    )
    result = await runtime.run(task_id=task_id, task="Verify the echo tool", workspace=Path.cwd())
    return result, len(trace.events)


async def _repair(
    *,
    workspace: Path,
    issue: str,
    model_name: str,
    api_key: str,
    base_url: str,
    test_command: tuple[str, ...],
    approve_all: bool,
) -> RunResult:
    task_id = f"repair-{uuid4().hex}"
    data_dir = workspace / ".forgeharness"
    trace = HashChainedJSONLTrace(data_dir / "traces" / f"{task_id}.jsonl", task_id)
    ledger = InMemoryApprovalLedger()
    async with httpx.AsyncClient(timeout=120.0) as client:
        provider = OpenAICompatibleModel(
            OpenAICompatibleConfig(
                base_url=base_url,
                api_key=api_key,
                model=model_name,
            ),
            client=client,
        )
        agent = CodingAgent(
            model=provider,
            trace=trace,
            approval_ledger=ledger,
            checkpoint_store=SQLiteCheckpointStore(data_dir / "runs.sqlite3"),
            test_command=test_command,
        )
        result = await agent.start(task_id=task_id, issue=issue, workspace=workspace)
        while result.status == RunStatus.AWAITING_APPROVAL:
            pending = result.pending_approval
            if pending is None:
                raise RuntimeError("approval status has no pending action")
            typer.echo(f"approval_required tool={pending.call.name}")
            typer.echo(json.dumps(pending.call.arguments, ensure_ascii=False, indent=2))
            if not approve_all and not typer.confirm("Approve this exact action?"):
                return result
            grant = agent.approve(result, granted_by="cli-user")
            result = await agent.resume(suspended=result, grant=grant, workspace=workspace)
        return result


def _print_result(result: RunResult) -> None:
    typer.echo(f"task_id={result.task_id}")
    typer.echo(f"status={result.status.value}")
    typer.echo(
        f"steps={result.usage.steps} tool_calls={result.usage.tool_calls} "
        f"input_tokens={result.usage.input_tokens} output_tokens={result.usage.output_tokens}"
    )
    if result.final_output:
        typer.echo(result.final_output)
    if result.error:
        typer.echo(f"error={result.error}", err=True)
