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
from forgeharness.evaluation.agent import (
    Profile,
    Split,
    Strategy,
    build_freeze_manifest,
    live_eval_config,
    run_agent_evaluation,
    run_live_agent_evaluation,
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
from forgeharness.observability.steps import StepRecord, TraceProjectionError, project_trace_path
from forgeharness.observability.trace import InMemoryTrace
from forgeharness.review import main as audit_repository
from forgeharness.runtime.loop import AgentRuntime
from forgeharness.state.approval import InMemoryApprovalLedger
from forgeharness.state.checkpoint import SQLiteCheckpointStore
from forgeharness.state.invocation_journal import SQLiteInvocationJournal
from forgeharness.state.recovery import (
    RecoveryCoordinator,
    RecoveryStatus,
    RunRecoveryResult,
)
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


def _case_ids(raw: str) -> tuple[str, ...]:
    """Parse the comma-separated --only-cases option into unique case ids."""
    ids = [item.strip() for item in raw.split(",") if item.strip()]
    return tuple(dict.fromkeys(ids))


@app.command("eval-agent")
def evaluate_agent(
    split: str = typer.Option("dev", help="Case split to run: dev or holdout."),
    output: str = typer.Option(
        "", help="Report path; defaults to a profile- and split-specific name."
    ),
    manifest: Path = Path("evals/agent_cases.json"),
    react_only: bool = typer.Option(
        False, "--react-only", help="Run only the ReAct arm; skip the Plan-Execute arm."
    ),
    profile: str = typer.Option(
        "keyless",
        help="keyless (no model), equal (shared whole-run budget), or natural (generous budget).",
    ),
    model: str = typer.Option("", help="Live model name; required unless --profile keyless."),
    base_url: str = typer.Option(
        "http://127.0.0.1:8000/v1", help="OpenAI-compatible base URL for live profiles."
    ),
    api_key: str = typer.Option("omlx-local", help="API key for live profiles."),
    max_tokens: int = typer.Option(
        1024,
        min=1,
        help=(
            "Per-request output ceiling for live profiles. Without it the server "
            "reserves its full context per request, which exhausts unified memory."
        ),
    ),
    samples: int = typer.Option(
        3, min=1, help="Repeats per case; a live profile requires at least 2."
    ),
    limit: int | None = typer.Option(
        None,
        min=1,
        help=(
            "Run only the first N cases. A truncated run is marked incomplete and "
            "cannot count as a measurement; use it to probe a live server cheaply."
        ),
    ),
    only_cases: str = typer.Option(
        "",
        "--only-cases",
        help=(
            "Comma-separated case ids to run in isolation. Selective runs are also "
            "marked incomplete; use them to reproduce one failure cheaply."
        ),
    ),
) -> None:
    """Run the Agent Benchmark for one split and profile, writing a JSON report.

    `keyless` validates the grading contract, metric pipeline, and the wiring of
    both runtimes; it cannot compare strategies because a scripted model replaces
    the decision under test. `equal` and `natural` run a real model and are the
    only profiles whose numbers may support a ReAct / Plan-Execute comparison.
    `equal` shares one whole-run budget across arms; `natural` gives both arms a
    generous budget so planner overhead shows up in cost rather than truncation.
    Only the dev split runs in CI; per-case holdout results must not guide tuning.
    """
    if split not in {"dev", "holdout"}:
        raise typer.BadParameter("split must be 'dev' or 'holdout'", param_hint="--split")
    try:
        selected_profile = Profile(profile)
    except ValueError as exc:
        raise typer.BadParameter(
            "profile must be 'keyless', 'equal', or 'natural'", param_hint="--profile"
        ) from exc
    if selected_profile is not Profile.KEYLESS and not model:
        raise typer.BadParameter("--model is required for a live profile", param_hint="--model")
    selected_split: Split = "holdout" if split == "holdout" else "dev"
    suffix = "keyless" if selected_profile is Profile.KEYLESS else f"{selected_profile.value}-omlx"
    target = Path(output) if output else Path(f"reports/agent-eval-{selected_split}-{suffix}.json")
    strategies = (Strategy.REACT,) if react_only else (Strategy.REACT, Strategy.PLAN_EXECUTE)
    if selected_profile is Profile.KEYLESS:
        report = asyncio.run(
            run_agent_evaluation(
                manifest_path=manifest,
                output_path=target,
                project_root=Path.cwd(),
                split=selected_split,
                strategies=strategies,
            )
        )
    else:
        report = asyncio.run(
            run_live_agent_evaluation(
                config=live_eval_config(
                    base_url=base_url, api_key=api_key, model=model, max_tokens=max_tokens
                ),
                manifest_path=manifest,
                output_path=target,
                project_root=Path.cwd(),
                split=selected_split,
                profile=selected_profile,
                samples=samples,
                strategies=strategies,
                limit=limit,
                only_cases=_case_ids(only_cases),
            )
        )
    typer.echo(f"split={report.split} profile={report.profile.value} model={report.model_name}")
    typer.echo(f"samples_per_case={report.samples_per_case}")
    typer.echo(f"benchmark_cases={report.benchmark_cases}")
    typer.echo(f"scorer_probes={report.scorer_probes}")
    for arm in report.arms:
        typer.echo(
            f"  {arm.strategy.value}: benchmark_pass={arm.benchmark_pass}/{arm.benchmark_cases} "
            f"mean_pass_rate={arm.mean_pass_rate:.3f} "
            f"probes_detected={arm.scorer_probes_detected}/{arm.scorer_probes} "
            f"contract_ok={str(arm.contract_checks_passed).lower()} "
            f"mean_steps={arm.mean_steps:.2f}"
        )
    agreement = (
        "n/a (split has no scorer probes)"
        if report.grading_agreement is None
        else f"{report.grading_agreement:.3f}"
    )
    typer.echo(f"grading_agreement={agreement}")
    typer.echo(f"measurement_valid={str(report.measurement_valid).lower()}")
    gate = (
        "n/a (live profile)"
        if report.keyless_gate_passed is None
        else str(report.keyless_gate_passed).lower()
    )
    typer.echo(f"keyless_gate_passed={gate}")
    typer.echo(f"cost_metrics_valid={str(report.cost_metrics_valid).lower()}")
    typer.echo(f"token_source={report.token_source}")
    typer.echo(f"wall_time_ms={report.wall_time_ms}")
    typer.echo(f"revision={report.revision}")
    typer.echo(f"report={target.resolve()}")
    if not report.measurement_valid:
        raise typer.Exit(code=1)
    if report.keyless_gate_passed is False:
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


@app.command("freeze-manifest")
def freeze_manifest(
    model: str = typer.Option("Qwythos-9B-v2-8bit-mlx", help="Model the final run will use."),
    samples: int = typer.Option(5, min=1, help="Samples per case per arm for the final run."),
    output: Path = Path("reports/freeze-manifest.json"),
    manifest: Path = Path("evals/agent_cases.json"),
) -> None:
    """Write the experiment identity a final evaluation is bound to.

    Every field is read from live constants, so the manifest cannot drift from the
    code. Generate it once, before the holdout run, and reference it from then on.
    """
    frozen = asyncio.run(
        build_freeze_manifest(
            manifest_path=manifest,
            project_root=Path.cwd(),
            model_name=model,
            samples=samples,
        )
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(frozen.model_dump_json(indent=2) + "\n", encoding="utf-8")
    typer.echo(f"revision={frozen.git_revision}")
    typer.echo(
        f"versions: observation={frozen.observation_contract_version} "
        f"recovery={frozen.recovery_semantics_version} "
        f"budget={frozen.budget_profile_version} benchmark={frozen.benchmark_version}"
    )
    typer.echo(f"holdout_cases={frozen.holdout_cases} samples_per_case={frozen.samples_per_case}")
    typer.echo(f"equal_step_cap={frozen.equal_step_cap}")
    typer.echo(f"holdout_case_ids_hash={frozen.holdout_case_ids_hash[:16]}...")
    typer.echo(f"report={output.resolve()}")


@app.command("recover")
def recover_runs(
    workspace: Path,
    model: str = typer.Option(..., help="Provider model identifier."),
    api_key: str = typer.Option(..., envvar="FORGE_API_KEY", help="Provider API key."),
    base_url: str = typer.Option(
        "https://api.openai.com/v1", help="OpenAI-compatible API base URL."
    ),
    test_command: str = typer.Option("python -m pytest -q", help="Fixed test argv."),
    run_id: str = typer.Option("", "--run-id", help="Recover only this run."),
) -> None:
    """Recover runs left mid-flight, consulting the invocation journal.

    Safety is not decided here: the runtime's journal recovery matrix decides
    whether a completed call's result is reused, whether a call may be replayed, or
    whether an unconfirmable non-idempotent call must fail closed.
    """
    root = workspace.resolve()
    command = tuple(shlex.split(test_command))
    results = asyncio.run(
        _recover(
            workspace=root,
            model_name=model,
            api_key=api_key,
            base_url=base_url,
            test_command=command,
            run_id=run_id or None,
        )
    )
    for item in results:
        typer.echo(f"task_id={item.task_id} recovery={item.status.value} detail={item.detail}")
    if any(item.status is RecoveryStatus.FAILED for item in results):
        raise typer.Exit(code=2)


async def _recover(
    *,
    workspace: Path,
    model_name: str,
    api_key: str,
    base_url: str,
    test_command: tuple[str, ...],
    run_id: str | None,
) -> tuple[RunRecoveryResult, ...]:
    data_dir = workspace / ".forgeharness"
    runs_db = data_dir / "runs.sqlite3"
    checkpoints = SQLiteCheckpointStore(runs_db)
    journal = SQLiteInvocationJournal(runs_db)

    async def resume(snapshot: RunResult, target: Path) -> RunResult:
        trace = HashChainedJSONLTrace(
            data_dir / "traces" / f"{snapshot.task_id}.jsonl", snapshot.task_id
        )
        async with httpx.AsyncClient(timeout=120.0) as client:
            provider = OpenAICompatibleModel(
                OpenAICompatibleConfig(base_url=base_url, api_key=api_key, model=model_name),
                client=client,
            )
            agent = CodingAgent(
                model=provider,
                trace=trace,
                approval_ledger=InMemoryApprovalLedger(),
                checkpoint_store=checkpoints,
                invocation_journal=journal,
                test_command=test_command,
            )
            return await agent.recover(previous=snapshot, workspace=target)

    coordinator = RecoveryCoordinator(
        checkpoints=checkpoints,
        resume=resume,
        # This profile recovers runs whose workspace is the directory being scanned.
        resolve_workspace=lambda snapshot: workspace,
    )
    if run_id is not None:
        return (await coordinator.recover_run(run_id),)
    return await coordinator.recover_pending()


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


@app.command("trace-steps")
def trace_steps(
    task_id: str,
    data_dir: Path = Path(".forgeharness"),
    as_json: bool = typer.Option(False, "--json", help="Emit the projection as JSON."),
) -> None:
    """Project a persisted run trace into ordered, readable execution steps.

    Uses the same projection as the agent evaluator, so a diagnostic view and a
    benchmark verdict can never disagree about what a run did.
    """
    trace_path = data_dir / "traces" / f"{task_id}.jsonl"
    if not trace_path.is_file():
        typer.echo(f"trace not found: {trace_path}", err=True)
        raise typer.Exit(code=1)
    try:
        projection = project_trace_path(trace_path)
    except TraceProjectionError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    if as_json:
        typer.echo(projection.model_dump_json(indent=2))
        return
    typer.echo(f"task_id={task_id} status={projection.final_status.value}")
    typer.echo(
        f"steps={len(projection.steps)} model_decisions={projection.model_decisions} "
        f"tool_attempts={projection.tool_attempts} plan_events={projection.plan_events} "
        f"incomplete_calls={projection.incomplete_calls}"
    )
    for step in projection.steps:
        typer.echo(_format_step(step))


def _format_step(step: StepRecord) -> str:
    """Render one step as a single readable line."""
    parts = [f"{step.step_id:>3}. [{step.phase.value}/{step.action_kind.value}]"]
    if step.tool_name:
        parts.append(step.tool_name)
    if step.tool_args:
        rendered = json.dumps(step.tool_args, sort_keys=True)
        parts.append(rendered if len(rendered) <= 90 else rendered[:87] + "...")
    parts.append(f"-> {step.status.value}")
    if step.logical_call_id:
        parts.append(f"call={step.logical_call_id}")
    if step.tool_latency_ms is not None:
        parts.append(f"tool_ms={step.tool_latency_ms}")
    if step.model_latency_ms_derived is not None:
        parts.append(f"model_ms~{step.model_latency_ms_derived}")
    if step.model_input_tokens or step.model_output_tokens:
        parts.append(f"tokens={step.model_input_tokens}+{step.model_output_tokens}")
    if step.error_type:
        parts.append(f"error={step.error_type}")
    if step.budget_snapshot:
        remaining = step.budget_snapshot
        parts.append(f"left(steps={remaining.get('steps')},calls={remaining.get('tool_calls')})")
    return " ".join(parts)


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
        runs_db = data_dir / "runs.sqlite3"
        agent = CodingAgent(
            model=provider,
            trace=trace,
            approval_ledger=ledger,
            checkpoint_store=SQLiteCheckpointStore(runs_db),
            # The journal is what makes side-effect recovery possible: without it a
            # resumed run cannot tell whether a write already happened.
            invocation_journal=SQLiteInvocationJournal(runs_db),
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
