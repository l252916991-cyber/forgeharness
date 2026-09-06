"""
Generate comprehensive test report and statistics.
"""

import json
from collections import defaultdict
from pathlib import Path


def generate_comprehensive_report():
    """Generate detailed test report."""

    report_path = Path("reports/real-evaluation-baseline.json")

    if not report_path.exists():
        print("❌ Report not found. Run evaluation first.")
        return

    with open(report_path) as f:
        report = json.load(f)

    results = report["results"]
    metrics = report["metrics"]

    print("=" * 70)
    print(" " * 20 + "COMPREHENSIVE TEST REPORT")
    print("=" * 70)

    print(f"\n📅 Test Date: {report['timestamp']}")
    print(f"📊 Total Tests: {report['total_tests']}")

    # Overall metrics
    print("\n" + "─" * 70)
    print("OVERALL PERFORMANCE")
    print("─" * 70)

    overall = metrics["overall"]
    print(f"\n  Total Tests:    {overall['total']}")
    print(f"  Passed:         {overall['passed']}")
    print(f"  Failed:         {overall['total'] - overall['passed']}")
    print(f"  Pass Rate:      {overall['pass_rate'] * 100:.2f}%")
    print(f"  Pass@1:         {overall['pass@1'] * 100:.2f}%")

    # By source
    print("\n" + "─" * 70)
    print("PERFORMANCE BY SOURCE")
    print("─" * 70)

    for source, src_metrics in metrics["by_source"].items():
        print(f"\n  {source.upper()}:")
        print(f"    Total:      {src_metrics['total']}")
        print(f"    Passed:     {src_metrics['passed']}")
        print(f"    Pass Rate:  {src_metrics['pass_rate'] * 100:.2f}%")

    # Industry comparison
    print("\n" + "─" * 70)
    print("INDUSTRY BENCHMARK COMPARISON")
    print("─" * 70)

    benchmarks = {
        "AutoGPT": 10.0,
        "MetaGPT": 13.0,
        "GPT-Engineer": 8.0,
        "GPT-4": 67.0,
        "Claude-3.5": 64.0,
    }

    current_score = overall["pass@1"] * 100

    print(f"\n  {'System':<20} {'Pass@1':<10} {'vs Current'}")
    print("  " + "-" * 50)

    for system, score in sorted(benchmarks.items(), key=lambda x: x[1]):
        diff = current_score - score
        indicator = "✅" if diff >= 0 else "❌"
        print(f"  {system:<20} {score:>6.1f}%    {indicator} {diff:+.1f}%")

    print(f"\n  {'ForgeHarness':<20} {current_score:>6.1f}%    {'🎯 CURRENT'}")

    # Status
    print("\n" + "─" * 70)
    print("ACHIEVEMENT STATUS")
    print("─" * 70)

    if current_score >= 8.0:
        status = "✅ ACHIEVED"
        message = "Meets industry baseline (AutoGPT level)"
    else:
        status = "⏳ IN PROGRESS"
        gap = 8.0 - current_score
        message = f"Need {gap:.1f}% more to reach baseline"

    print(f"\n  Status: {status}")
    print(f"  {message}")

    # Failure analysis
    if overall["total"] - overall["passed"] > 0:
        print("\n" + "─" * 70)
        print("FAILURE ANALYSIS")
        print("─" * 70)

        failures = [r for r in results if not r["passed"]]

        # Categorize errors
        error_categories = defaultdict(list)

        for failure in failures:
            test_output = failure.get("test_output", "")

            if "IndentationError" in test_output:
                category = "IndentationError"
            elif "NameError" in test_output:
                category = "NameError"
            elif "AssertionError" in test_output or "ASSERTION_FAILED" in test_output:
                category = "AssertionError"
            elif "SyntaxError" in test_output:
                category = "SyntaxError"
            elif "TypeError" in test_output:
                category = "TypeError"
            elif not test_output.strip():
                category = "EmptyOutput"
            else:
                category = "Other"

            error_categories[category].append(failure["test_id"])

        print(f"\n  Total Failures: {len(failures)}")
        print("\n  By Error Type:")
        for category, test_ids in sorted(
            error_categories.items(), key=lambda x: len(x[1]), reverse=True
        ):
            print(f"    {category:<20} {len(test_ids):>3} tests")

    # Test coverage
    print("\n" + "─" * 70)
    print("TEST COVERAGE")
    print("─" * 70)

    print("\n  Test Sources:")
    print("    HumanEval:    Industry-standard coding benchmark")
    print("    MBPP:         Google's programming benchmark")

    print(f"\n  Total Coverage: {overall['total']} programming problems")

    # Save summary
    summary_path = Path("reports/test-summary.json")
    summary = {
        "timestamp": report["timestamp"],
        "total_tests": overall["total"],
        "passed": overall["passed"],
        "pass_rate": overall["pass_rate"],
        "pass@1": overall["pass@1"],
        "by_source": metrics["by_source"],
        "industry_comparison": {
            "current": current_score,
            "target": 8.0,
            "status": "achieved" if current_score >= 8.0 else "in_progress",
        },
    }

    with open(summary_path, "w") as f:
        json.dump(summary, f, indent=2)

    print("\n" + "=" * 70)
    print(f"📄 Detailed report: {report_path}")
    print(f"📊 Summary: {summary_path}")
    print("=" * 70)


if __name__ == "__main__":
    generate_comprehensive_report()
