"""Framework-comparison CLI commands (lazy imports keep the core CLI dependency-free)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import typer
from pydantic import SecretStr


def register(app: typer.Typer) -> None:
    """Register framework-comparison commands on the given Typer app."""

    @app.command("langchain-rag")
    def langchain_rag(
        query: str,
        data_dir: Path = Path(".forgeharness"),
        base_url: str = "http://127.0.0.1:8000/v1",
        model: str = "Qwythos-9B-v2-8bit-mlx",
    ) -> None:
        """Answer a query through the LangChain LCEL RAG implementation."""
        try:
            from forgeharness.langchain_impl.rag_chain import create_langchain_rag_app
        except ImportError as exc:
            typer.echo(f"frameworks extra not installed: {exc}", err=True)
            raise typer.Exit(code=3) from exc

        async def _run() -> None:
            chain = await create_langchain_rag_app(
                data_dir=data_dir, model_base_url=base_url, model_name=model
            )
            try:
                result = await chain.ask(query)
            finally:
                await chain.close()
            typer.echo(f"answer={result.answer}")
            typer.echo(f"sources={','.join(result.sources)}")
            typer.echo(f"chunks={result.chunks_used} latency_ms={result.retrieval_time_ms:.0f}")

        asyncio.run(_run())

    @app.command("langchain-agent")
    def langchain_agent(
        task: str,
        workspace: Path = typer.Option(  # noqa: B008 - typer idiom
            ..., help="Workspace directory for the agent."
        ),
        base_url: str = "http://127.0.0.1:8000/v1",
        model: str = "Qwythos-9B-v2-8bit-mlx",
        test_command: str = "python -m pytest -q",
    ) -> None:
        """Run a coding task through the LangGraph state-machine agent."""
        try:
            from langchain_openai import ChatOpenAI

            from forgeharness.langchain_impl.coding_agent import LangGraphCodingAgent
        except ImportError as exc:
            typer.echo(f"frameworks extra not installed: {exc}", err=True)
            raise typer.Exit(code=3) from exc

        resolved_workspace = workspace.resolve()

        async def _run() -> None:
            llm = ChatOpenAI(
                base_url=base_url,
                model=model,
                temperature=0.1,
                api_key=SecretStr("not-needed"),
            )
            agent = LangGraphCodingAgent(
                llm=llm,
                workspace=resolved_workspace,
                test_command=test_command,
            )
            result = await agent.run(task)
            typer.echo(f"status={result['status']}")
            typer.echo(f"iterations={result['iterations']} tool_calls={result['tool_calls']}")

        asyncio.run(_run())

    @app.command("framework-compare")
    def framework_compare(
        data_dir: Path = Path(".forgeharness"),
        output: Path = Path("reports/framework-comparison.json"),
        queries: int = typer.Option(5, min=1, max=50, help="Number of benchmark queries."),
        model: str = "Qwythos-9B-v2-8bit-mlx",
    ) -> None:
        """Benchmark native retrieval against the LangChain path and write a JSON report."""
        try:
            from forgeharness.langchain_impl.benchmark import run_comparison
        except ImportError as exc:
            typer.echo(f"frameworks extra not installed: {exc}", err=True)
            raise typer.Exit(code=3) from exc

        report = asyncio.run(
            run_comparison(data_dir=data_dir, output_path=output, query_count=queries, model=model)
        )
        typer.echo(f"model={model}")
        typer.echo(f"native={json.dumps(report.get('native'), ensure_ascii=False)}")
        typer.echo(f"langchain={json.dumps(report.get('langchain'), ensure_ascii=False)}")
        for note in report.get("notes", []):
            typer.echo(f"note={note}")
        typer.echo(f"report={output.resolve()}")
