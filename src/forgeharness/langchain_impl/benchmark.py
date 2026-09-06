"""Measured comparison between the native retrieval path and the LangChain path.

Every number written into the report comes from an actual timed run in the same
process. When the optional `frameworks` extra is not installed, the LangChain arm
is recorded as skipped instead of being estimated.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

DEFAULT_QUERIES: tuple[str, ...] = (
    "How does the approval mechanism work?",
    "What are the agent budget limits?",
    "Explain the checkpoint and resume flow",
    "How does hybrid retrieval combine FTS5 and vector search?",
    "What security boundaries does the harness enforce?",
)


def _percentile(sorted_values: Sequence[float], fraction: float) -> float:
    if not sorted_values:
        return 0.0
    index = min(len(sorted_values) - 1, max(0, round(fraction * (len(sorted_values) - 1))))
    return sorted_values[index]


def _stats(latencies_ms: list[float], hits: list[int]) -> dict[str, Any]:
    ordered = sorted(latencies_ms)
    return {
        "count": len(latencies_ms),
        "mean_ms": round(sum(ordered) / len(ordered), 3) if ordered else 0.0,
        "p50_ms": round(_percentile(ordered, 0.50), 3),
        "p95_ms": round(_percentile(ordered, 0.95), 3),
        "mean_hits": round(sum(hits) / len(hits), 3) if hits else 0.0,
    }


def _git_revision() -> str:
    """Bind the report to the exact source revision per docs/EVALUATION.md policy."""
    import subprocess

    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown-dirty-tree"


async def run_comparison(
    *,
    data_dir: Path,
    output_path: Path,
    query_count: int = 5,
    queries: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Run both arms on identical queries and persist only measured results."""

    from forgeharness.knowledge.application import build_knowledge_application

    selected = list(queries or DEFAULT_QUERIES)[:query_count]
    report: dict[str, Any] = {
        "revision": _git_revision(),
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "queries": selected,
        "native": None,
        "langchain": None,
        "notes": [],
    }

    application = build_knowledge_application(data_dir)
    try:
        native_latency: list[float] = []
        native_hits: list[int] = []
        for query in selected:
            start = time.perf_counter()
            search_report = await application.knowledge.search(query)
            native_latency.append((time.perf_counter() - start) * 1000)
            native_hits.append(len(search_report.final))
        report["native"] = _stats(native_latency, native_hits)
    finally:
        await application.close()

    try:
        from forgeharness.langchain_impl.rag_chain import create_langchain_rag_app
    except ImportError as exc:
        report["langchain"] = {"skipped": True, "reason": f"frameworks extra not installed: {exc}"}
        report["notes"].append("LangChain arm skipped; install with `uv sync --extra frameworks`.")
    else:
        chain = await create_langchain_rag_app(data_dir)
        try:
            retriever_latency: list[float] = []
            retriever_hits: list[int] = []
            end_to_end_latency: list[float] = []
            for query in selected:
                start = time.perf_counter()
                documents = await chain.retriever.ainvoke(query)
                retriever_latency.append((time.perf_counter() - start) * 1000)
                retriever_hits.append(len(documents))

                start = time.perf_counter()
                await chain.ask(query)
                end_to_end_latency.append((time.perf_counter() - start) * 1000)
            report["langchain"] = {
                "skipped": False,
                "retrieval": _stats(retriever_latency, retriever_hits),
                "end_to_end": _stats(end_to_end_latency, retriever_hits),
            }
            report["notes"].append(
                "end_to_end includes chat-model generation; compare against native retrieval only."
            )
        finally:
            await chain.close()

    if report["native"] and report["langchain"] and not report["langchain"].get("skipped"):
        native_mean = report["native"]["mean_ms"]
        chain_mean = report["langchain"]["retrieval"]["mean_ms"]
        if native_mean > 0:
            report["retrieval_overhead_pct"] = round(
                (chain_mean - native_mean) / native_mean * 100, 2
            )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(report, indent=2, ensure_ascii=False)
    await asyncio.to_thread(output_path.write_text, payload)
    return report


async def main() -> None:
    """Run the comparison with repository defaults."""
    report = await run_comparison(
        data_dir=Path(".forgeharness"),
        output_path=Path("reports/framework-comparison.json"),
    )
    print(json.dumps(report["summary"] if "summary" in report else report, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
