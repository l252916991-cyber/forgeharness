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
from forgeharness.evaluation.omlx import run_omlx_qualification
from forgeharness.evaluation.performance import run_retrieval_benchmark
from forgeharness.evaluation.rag import run_rag_evaluation
from forgeharness.evaluation.reviewer import run_reviewer_experiment
from forgeharness.langchain_impl.cli_commands import (
    register as register_framework_commands,
)
from forgeharness.models.omlx import OMLXConfig
from forgeharness.models.openai_compatible import (
    OpenAICompatibleConfig,
    OpenAICompatibleModel,
)
from forgeharness.models.scripted import ScriptedModel
from forgeharness.observability.hash_chain import HashChainedJSONLTrace, verify_trace
from forgeharness.observability.trace import InMemoryTrace
from forgeharness.review import main as audit_repository
from forgeharness.runtime.loop import AgentRuntime
from forgeharness.state.approval import InMemoryApprovalLedger
from forgeharness.state.checkpoint import SQLiteCheckpointStore
from forgeharness.tools.builtin import EchoTool
from forgeharness.tools.dispatcher import ToolDispatcher
from forgeharness.tools.policy import RiskBasedPolicy
from forgeharness.tools.registry import ToolRegistry

app = typer.Typer(no_args_is_help=True)

# Framework-comparison commands use lazy imports so the core CLI works without
# the optional `frameworks` extra; each command degrades to exit code 3 with a
# clear message when langchain/langgraph are absent.
register_framework_commands(app)


@app.callback()
def main() -> None:
    """Run, evaluate, and inspect ForgeHarness workflows."""


@app.command("review")
def review_repository() -> None:
    """Validate all final-candidate evidence and unresolved findings."""
    audit_repository()


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


@app.command("qualify-omlx")
def qualify_omlx(
    output: Path = Path("reports/omlx-qualification.json"),
    base_url: str = "http://127.0.0.1:8000/v1",
    chat_model: str = "Qwen3.5-9B-4bit",
    embedding_model: str = "Qwen3-Embedding-4B-4bit-DWQ",
    reranker_model: str = "bge-reranker-v2-m3-mlx",
    api_key: str | None = typer.Option(None, envvar="FORGE_OMLX_API_KEY"),
    quick: bool = typer.Option(False, help="Run two cases per capability as a smoke test."),
) -> None:
    """Qualify local OMLX models and write per-case evidence."""
    report = asyncio.run(
        run_omlx_qualification(
            config=OMLXConfig(
                base_url=base_url,
                api_key=api_key,
                chat_model=chat_model,
                embedding_model=embedding_model,
                reranker_model=reranker_model,
            ),
            output_path=output,
            project_root=Path.cwd(),
            quick=quick,
        )
    )
    for name, section in report.sections.items():
        typer.echo(
            f"{name}={section.passed}/{section.total} median_ms={section.median_latency_ms:.1f}"
        )
    if report.fatal_error is not None:
        typer.echo(f"fatal_error={report.fatal_error}", err=True)
    typer.echo(f"qualified={str(report.qualified).lower()}")
    typer.echo(f"report={output.resolve()}")
    if not report.qualified:
        raise typer.Exit(code=2)


@app.command("eval-rag")
def evaluate_rag(
    manifest: Path = Path("evals/rag_cases.json"),
    output: Path = Path("reports/rag-eval.json"),
) -> None:
    """Run the fixed 60-case keyless RAG quality gate."""
    report = asyncio.run(
        run_rag_evaluation(
            manifest_path=manifest,
            output_path=output,
            project_root=Path.cwd(),
        )
    )
    typer.echo(f"cases={report.total}")
    typer.echo(f"recall_at_5={report.recall_at_5:.3f}")
    typer.echo(f"mrr_at_10={report.mrr_at_10:.3f}")
    typer.echo(f"citation_precision={report.citation_precision:.3f}")
    typer.echo(f"unsupported_answer_rate={report.unsupported_answer_rate:.3f}")
    typer.echo(f"qualified={str(report.qualified).lower()}")
    typer.echo(f"report={output.resolve()}")
    if not report.qualified:
        raise typer.Exit(code=2)


@app.command("bench-retrieval")
def benchmark_retrieval(
    output: Path = Path("reports/retrieval-benchmark.json"),
) -> None:
    """Measure a warmed 5,000-chunk, 10-concurrency retrieval workload."""
    report = asyncio.run(run_retrieval_benchmark(output_path=output, project_root=Path.cwd()))
    typer.echo(f"chunks={report.chunks} requests={report.requests}")
    typer.echo(f"retrieval_p50_ms={report.retrieval_p50_ms:.3f}")
    typer.echo(f"retrieval_p95_ms={report.retrieval_p95_ms:.3f}")
    typer.echo(f"qualified={str(report.qualified).lower()}")
    typer.echo(f"report={output.resolve()}")
    if not report.qualified:
        raise typer.Exit(code=2)


@app.command("eval-reviewer")
def evaluate_reviewer(
    manifest: Path = Path("evals/rag_cases.json"),
    output: Path = Path("reports/reviewer-experiment.json"),
) -> None:
    """Measure whether the optional Reviewer earns its latency and complexity."""
    report = asyncio.run(
        run_reviewer_experiment(
            manifest_path=manifest,
            output_path=output,
            project_root=Path.cwd(),
        )
    )
    typer.echo(f"baseline_effective={report.baseline.effective_answer_rate:.3f}")
    typer.echo(f"reviewer_effective={report.reviewer.effective_answer_rate:.3f}")
    typer.echo(f"quality_improvement_points={report.quality_improvement_points:.3f}")
    typer.echo(f"latency_ratio={report.latency_ratio:.3f}")
    typer.echo(f"reviewer_default={str(report.reviewer_enabled_by_default).lower()}")
    typer.echo(f"report={output.resolve()}")


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
    port: int = typer.Option(8001, min=1, max=65_535),
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
