"""
Automated framework comparison runner.

Runs both Native and LangChain implementations on the same test cases
and generates a comprehensive comparison report.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from evals.test_cases import RAG_TEST_CASES


@dataclass
class RAGTestResult:
    """Result of a single RAG test."""

    test_id: str
    implementation: str  # "native" | "langchain"
    query: str
    answer: str
    sources: list[str]
    latency_ms: float
    success: bool
    error: str | None
    keywords_found: list[str]
    expected_keywords_match_rate: float


@dataclass
class AgentTestResult:
    """Result of a single Agent test."""

    test_id: str
    implementation: str
    task: str
    status: str
    iterations: int
    tool_calls: int
    duration_ms: float
    success: bool
    error: str | None
    files_changed: list[str]


@dataclass
class ComparisonReport:
    """Complete comparison report."""

    timestamp: str
    rag_results: list[RAGTestResult]
    agent_results: list[AgentTestResult]
    summary: dict[str, Any]


class AutomatedComparisonRunner:
    """Run comprehensive comparison between implementations."""

    def __init__(self, data_dir: Path, output_dir: Path):
        self.data_dir = data_dir
        self.output_dir = output_dir
        self.native_app: Any = None
        self.langchain_app: Any = None

    async def setup(self) -> None:
        """Initialize both implementations."""
        print("🔧 Setting up implementations...")

        # Import here to avoid dependency issues if frameworks not installed
        from forgeharness.knowledge.application import build_knowledge_application

        self.native_app = build_knowledge_application(self.data_dir)
        print("  ✅ Native implementation ready")

        try:
            from forgeharness.langchain_impl.rag_chain import create_langchain_rag_app

            self.langchain_app = await create_langchain_rag_app(self.data_dir)
            print("  ✅ LangChain implementation ready")
        except ImportError as e:
            print(f"  ⚠️  LangChain not available: {e}")
            print("     Install with: uv sync --extra frameworks")
            self.langchain_app = None

    async def run_rag_test_native(self, test_case: Any) -> RAGTestResult:
        """Run a single RAG test on native implementation."""
        try:
            start = time.perf_counter()
            report = await self.native_app.knowledge.search(test_case.query)
            latency = (time.perf_counter() - start) * 1000

            sources = list({hit.chunk.source for hit in report.final})

            # Check keyword matches (simplified - just check if in chunks)
            all_content = " ".join(hit.chunk.content for hit in report.final).lower()
            keywords_found = [kw for kw in test_case.expected_keywords if kw.lower() in all_content]
            match_rate = (
                len(keywords_found) / len(test_case.expected_keywords)
                if test_case.expected_keywords
                else 1.0
            )

            return RAGTestResult(
                test_id=test_case.id,
                implementation="native",
                query=test_case.query,
                answer="[Native returns retrieval report, not generated answer]",
                sources=sources,
                latency_ms=latency,
                success=len(report.final) > 0 or test_case.category == "edge_case",
                error=None,
                keywords_found=keywords_found,
                expected_keywords_match_rate=match_rate,
            )

        except Exception as e:
            return RAGTestResult(
                test_id=test_case.id,
                implementation="native",
                query=test_case.query,
                answer="",
                sources=[],
                latency_ms=0.0,
                success=False,
                error=str(e),
                keywords_found=[],
                expected_keywords_match_rate=0.0,
            )

    async def run_rag_test_langchain(self, test_case: Any) -> RAGTestResult:
        """Run a single RAG test on LangChain implementation."""
        if not self.langchain_app:
            return RAGTestResult(
                test_id=test_case.id,
                implementation="langchain",
                query=test_case.query,
                answer="",
                sources=[],
                latency_ms=0.0,
                success=False,
                error="LangChain not available",
                keywords_found=[],
                expected_keywords_match_rate=0.0,
            )

        try:
            result = await self.langchain_app.ask(test_case.query)

            # Check keyword matches in answer
            answer_lower = result.answer.lower()
            keywords_found = [
                kw for kw in test_case.expected_keywords if kw.lower() in answer_lower
            ]
            match_rate = (
                len(keywords_found) / len(test_case.expected_keywords)
                if test_case.expected_keywords
                else 1.0
            )

            return RAGTestResult(
                test_id=test_case.id,
                implementation="langchain",
                query=test_case.query,
                answer=result.answer[:500] + "..." if len(result.answer) > 500 else result.answer,
                sources=result.sources,
                latency_ms=result.retrieval_time_ms,
                success=len(result.sources) > 0 or test_case.category == "edge_case",
                error=None,
                keywords_found=keywords_found,
                expected_keywords_match_rate=match_rate,
            )

        except Exception as e:
            return RAGTestResult(
                test_id=test_case.id,
                implementation="langchain",
                query=test_case.query,
                answer="",
                sources=[],
                latency_ms=0.0,
                success=False,
                error=str(e),
                keywords_found=[],
                expected_keywords_match_rate=0.0,
            )

    async def run_all_rag_tests(self) -> list[RAGTestResult]:
        """Run all RAG tests on both implementations."""
        print("\n📊 Running RAG Tests...")
        print(f"   Total: {len(RAG_TEST_CASES)} test cases")

        results = []
        for i, test_case in enumerate(RAG_TEST_CASES, 1):
            print(f"   [{i}/{len(RAG_TEST_CASES)}] {test_case.id}: {test_case.query[:50]}...")

            # Run native
            native_result = await self.run_rag_test_native(test_case)
            results.append(native_result)
            mark = "✅" if native_result.success else "❌"
            print(f"      Native: {mark} {native_result.latency_ms:.0f}ms")

            # Run LangChain
            lc_result = await self.run_rag_test_langchain(test_case)
            results.append(lc_result)
            if lc_result.error != "LangChain not available":
                mark = "✅" if lc_result.success else "❌"
                print(f"      LangChain: {mark} {lc_result.latency_ms:.0f}ms")

        return results

    def generate_summary(
        self,
        rag_results: list[RAGTestResult],
        agent_results: list[AgentTestResult],
    ) -> dict[str, Any]:
        """Generate comparison summary."""

        # RAG summary
        native_rag = [r for r in rag_results if r.implementation == "native" and not r.error]
        lc_rag = [
            r
            for r in rag_results
            if r.implementation == "langchain" and r.error != "LangChain not available"
        ]

        rag_summary = {
            "native": {
                "total_tests": len(native_rag),
                "success_rate": sum(1 for r in native_rag if r.success) / len(native_rag)
                if native_rag
                else 0,
                "avg_latency_ms": sum(r.latency_ms for r in native_rag) / len(native_rag)
                if native_rag
                else 0,
                "avg_keyword_match": sum(r.expected_keywords_match_rate for r in native_rag)
                / len(native_rag)
                if native_rag
                else 0,
            },
            "langchain": {
                "total_tests": len(lc_rag),
                "success_rate": sum(1 for r in lc_rag if r.success) / len(lc_rag) if lc_rag else 0,
                "avg_latency_ms": sum(r.latency_ms for r in lc_rag) / len(lc_rag) if lc_rag else 0,
                "avg_keyword_match": sum(r.expected_keywords_match_rate for r in lc_rag)
                / len(lc_rag)
                if lc_rag
                else 0,
            },
        }

        # Agent summary (placeholder - would need actual agent runs)
        agent_summary = {
            "native": {
                "total_tests": 0,
                "success_rate": 0.0,
                "avg_iterations": 0.0,
                "avg_tool_calls": 0.0,
            },
            "langchain": {
                "total_tests": 0,
                "success_rate": 0.0,
                "avg_iterations": 0.0,
                "avg_tool_calls": 0.0,
            },
        }

        # Generate insights
        insights = []
        if native_rag and lc_rag:
            lat_diff = (
                (
                    rag_summary["langchain"]["avg_latency_ms"]
                    - rag_summary["native"]["avg_latency_ms"]
                )
                / rag_summary["native"]["avg_latency_ms"]
                * 100
            )
            insights.append(f"LangChain latency overhead: {lat_diff:+.1f}%")

            success_native = rag_summary["native"]["success_rate"] * 100
            success_lc = rag_summary["langchain"]["success_rate"] * 100
            insights.append(
                f"Success rates: Native {success_native:.0f}%, LangChain {success_lc:.0f}%"
            )

            kw_native = rag_summary["native"]["avg_keyword_match"] * 100
            kw_lc = rag_summary["langchain"]["avg_keyword_match"] * 100
            insights.append(f"Keyword match: Native {kw_native:.0f}%, LangChain {kw_lc:.0f}%")

        return {
            "rag": rag_summary,
            "agent": agent_summary,
            "insights": insights,
        }

    async def run_full_comparison(self) -> ComparisonReport:
        """Run complete comparison suite."""
        await self.setup()

        # Run RAG tests
        rag_results = await self.run_all_rag_tests()

        # Agent tests would be run here (requires workspace setup)
        agent_results: list[AgentTestResult] = []

        # Generate summary
        summary = self.generate_summary(rag_results, agent_results)

        report = ComparisonReport(
            timestamp=time.strftime("%Y-%m-%d %H:%M:%S"),
            rag_results=rag_results,
            agent_results=agent_results,
            summary=summary,
        )

        # Save report
        self.output_dir.mkdir(parents=True, exist_ok=True)
        report_path = self.output_dir / "comparison-detailed.json"

        with open(report_path, "w") as f:
            json.dump(
                {
                    "timestamp": report.timestamp,
                    "rag_results": [asdict(r) for r in rag_results],
                    "agent_results": [asdict(r) for r in agent_results],
                    "summary": summary,
                },
                f,
                indent=2,
            )

        print(f"\n✅ Report saved to {report_path}")

        # Print summary
        print("\n" + "=" * 60)
        print("📊 COMPARISON SUMMARY")
        print("=" * 60)
        for insight in summary["insights"]:
            print(f"  • {insight}")
        print()

        return report

    async def cleanup(self) -> None:
        """Close connections."""
        if self.native_app:
            await self.native_app.close()


async def main() -> None:
    """Entry point for automated comparison."""
    runner = AutomatedComparisonRunner(
        data_dir=Path(".forgeharness"),
        output_dir=Path("reports"),
    )

    try:
        await runner.run_full_comparison()
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
