"""
Generate visual comparison report with charts and tables.

Creates an HTML report comparing Native vs LangChain implementations.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def generate_html_report(comparison_data: dict[str, Any], output_path: Path) -> None:
    """Generate HTML report with visualization."""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>ForgeHarness Framework Comparison Report</title>
    <style>
        * {{
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }}
        
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            line-height: 1.6;
            color: #333;
            background: #f5f5f5;
            padding: 20px;
        }}
        
        .container {{
            max-width: 1200px;
            margin: 0 auto;
            background: white;
            padding: 40px;
            border-radius: 8px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.1);
        }}
        
        h1 {{
            font-size: 2.5em;
            margin-bottom: 10px;
            color: #2c3e50;
        }}
        
        .subtitle {{
            color: #7f8c8d;
            margin-bottom: 30px;
            font-size: 1.1em;
        }}
        
        .meta {{
            background: #ecf0f1;
            padding: 15px;
            border-radius: 5px;
            margin-bottom: 30px;
        }}
        
        h2 {{
            font-size: 1.8em;
            margin: 40px 0 20px;
            color: #34495e;
            border-bottom: 3px solid #3498db;
            padding-bottom: 10px;
        }}
        
        .summary-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
            gap: 20px;
            margin: 30px 0;
        }}
        
        .metric-card {{
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            padding: 25px;
            border-radius: 8px;
            color: white;
            box-shadow: 0 4px 6px rgba(0,0,0,0.1);
        }}
        
        .metric-card.green {{
            background: linear-gradient(135deg, #11998e 0%, #38ef7d 100%);
        }}
        
        .metric-card.orange {{
            background: linear-gradient(135deg, #f093fb 0%, #f5576c 100%);
        }}
        
        .metric-card.blue {{
            background: linear-gradient(135deg, #4facfe 0%, #00f2fe 100%);
        }}
        
        .metric-label {{
            font-size: 0.9em;
            opacity: 0.9;
            margin-bottom: 5px;
        }}
        
        .metric-value {{
            font-size: 2.5em;
            font-weight: bold;
        }}
        
        .metric-detail {{
            font-size: 0.85em;
            opacity: 0.85;
            margin-top: 5px;
        }}
        
        table {{
            width: 100%;
            border-collapse: collapse;
            margin: 20px 0;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }}
        
        thead {{
            background: #34495e;
            color: white;
        }}
        
        th, td {{
            padding: 12px 15px;
            text-align: left;
            border-bottom: 1px solid #ddd;
        }}
        
        tbody tr:hover {{
            background: #f8f9fa;
        }}
        
        .success {{
            color: #27ae60;
            font-weight: bold;
        }}
        
        .failure {{
            color: #e74c3c;
            font-weight: bold;
        }}
        
        .chart-container {{
            margin: 30px 0;
            padding: 20px;
            background: #f8f9fa;
            border-radius: 8px;
        }}
        
        .bar {{
            display: flex;
            align-items: center;
            margin: 15px 0;
        }}
        
        .bar-label {{
            width: 150px;
            font-weight: 500;
        }}
        
        .bar-fill {{
            height: 30px;
            background: linear-gradient(90deg, #3498db, #2980b9);
            border-radius: 4px;
            display: flex;
            align-items: center;
            padding: 0 10px;
            color: white;
            font-weight: bold;
            transition: width 0.3s ease;
        }}
        
        .bar-fill.langchain {{
            background: linear-gradient(90deg, #e74c3c, #c0392b);
        }}
        
        .insights {{
            background: #fff3cd;
            border-left: 4px solid #ffc107;
            padding: 20px;
            margin: 20px 0;
            border-radius: 4px;
        }}
        
        .insights h3 {{
            color: #856404;
            margin-bottom: 15px;
        }}
        
        .insights ul {{
            list-style: none;
            padding-left: 0;
        }}
        
        .insights li {{
            padding: 8px 0;
            padding-left: 25px;
            position: relative;
        }}
        
        .insights li:before {{
            content: "💡";
            position: absolute;
            left: 0;
        }}
        
        .comparison-table {{
            margin: 30px 0;
        }}
        
        .winner {{
            background: #d4edda !important;
            font-weight: bold;
        }}
        
        footer {{
            margin-top: 50px;
            padding-top: 20px;
            border-top: 1px solid #ddd;
            text-align: center;
            color: #7f8c8d;
        }}
    </style>
</head>
<body>
    <div class="container">
        <h1>🔬 ForgeHarness Framework Comparison</h1>
        <div class="subtitle">Native Runtime vs LangChain/LangGraph Implementation</div>
        
        <div class="meta">
            <strong>Report Generated:</strong> {comparison_data.get("timestamp", "N/A")}<br>
            <strong>RAG Test Cases:</strong> {len(comparison_data.get("rag_results", []) or [])} 
            (Native: {
        len(
            [
                r
                for r in comparison_data.get("rag_results", [])
                if r.get("implementation") == "native"
            ]
        )
    },
            LangChain: {
        len(
            [
                r
                for r in comparison_data.get("rag_results", [])
                if r.get("implementation") == "langchain"
                and r.get("error") != "LangChain not available"
            ]
        )
    })<br>
            <strong>Agent Test Cases:</strong> {len(comparison_data.get("agent_results", []) or [])}
        </div>
        
        <h2>📊 Executive Summary</h2>
        
        <div class="summary-grid">
            <div class="metric-card green">
                <div class="metric-label">Native Success Rate</div>
                <div class="metric-value">{
        comparison_data["summary"]["rag"]["native"]["success_rate"] * 100:.0f}%</div>
                <div class="metric-detail">RAG Tests Passed</div>
            </div>
            
            <div class="metric-card green">
                <div class="metric-label">LangChain Success Rate</div>
                <div class="metric-value">{
        comparison_data["summary"]["rag"]["langchain"]["success_rate"] * 100:.0f}%</div>
                <div class="metric-detail">RAG Tests Passed</div>
            </div>
            
            <div class="metric-card blue">
                <div class="metric-label">Native Avg Latency</div>
                <div class="metric-value">{
        comparison_data["summary"]["rag"]["native"]["avg_latency_ms"]:.0f}ms</div>
                <div class="metric-detail">RAG Retrieval Time</div>
            </div>
            
            <div class="metric-card orange">
                <div class="metric-label">LangChain Avg Latency</div>
                <div class="metric-value">{
        comparison_data["summary"]["rag"]["langchain"]["avg_latency_ms"]:.0f}ms</div>
                <div class="metric-detail">RAG Retrieval Time</div>
            </div>
        </div>
        
        <div class="insights">
            <h3>🔑 Key Insights</h3>
            <ul>
                {
        "".join(f"<li>{insight}</li>" for insight in comparison_data["summary"]["insights"])
    }
            </ul>
        </div>
        
        <h2>⚡ Performance Comparison</h2>
        
        <div class="chart-container">
            <h3>Average Latency (ms)</h3>
            <div class="bar">
                <div class="bar-label">Native</div>
                <div class="bar-fill" style="width: {
        comparison_data["summary"]["rag"]["native"]["avg_latency_ms"] / 3
    }px">
                    {comparison_data["summary"]["rag"]["native"]["avg_latency_ms"]:.0f}ms
                </div>
            </div>
            <div class="bar">
                <div class="bar-label">LangChain</div>
                <div class="bar-fill langchain" style="width: {
        comparison_data["summary"]["rag"]["langchain"]["avg_latency_ms"] / 3
    }px">
                    {comparison_data["summary"]["rag"]["langchain"]["avg_latency_ms"]:.0f}ms
                </div>
            </div>
        </div>
        
        <div class="chart-container">
            <h3>Keyword Match Rate (%)</h3>
            <div class="bar">
                <div class="bar-label">Native</div>
                <div class="bar-fill" style="width: {
        comparison_data["summary"]["rag"]["native"]["avg_keyword_match"] * 400
    }px">
                    {comparison_data["summary"]["rag"]["native"]["avg_keyword_match"] * 100:.0f}%
                </div>
            </div>
            <div class="bar">
                <div class="bar-label">LangChain</div>
                <div class="bar-fill langchain" style="width: {
        comparison_data["summary"]["rag"]["langchain"]["avg_keyword_match"] * 400
    }px">
                    {comparison_data["summary"]["rag"]["langchain"]["avg_keyword_match"] * 100:.0f}%
                </div>
            </div>
        </div>
        
        <h2>📋 Detailed RAG Test Results</h2>
        
        <table class="comparison-table">
            <thead>
                <tr>
                    <th>Test ID</th>
                    <th>Implementation</th>
                    <th>Query</th>
                    <th>Latency (ms)</th>
                    <th>Keyword Match</th>
                    <th>Sources</th>
                    <th>Status</th>
                </tr>
            </thead>
            <tbody>
                {
        "".join(
            f'''
                <tr>
                    <td>{r["test_id"]}</td>
                    <td>{r["implementation"].title()}</td>
                    <td>{r["query"][:60]}{"..." if len(r["query"]) > 60 else ""}</td>
                    <td>{r["latency_ms"]:.0f}</td>
                    <td>{r["expected_keywords_match_rate"] * 100:.0f}%</td>
                    <td>{len(r["sources"])}</td>
                    <td class="{"success" if r["success"] else "failure"}">
                        {"✅ Pass" if r["success"] else "❌ Fail"}
                    </td>
                </tr>
                '''
            for r in comparison_data.get("rag_results", [])
        )
    }
            </tbody>
        </table>
        
        <h2>🎯 Comparison Matrix</h2>
        
        <table>
            <thead>
                <tr>
                    <th>Dimension</th>
                    <th>Native ForgeHarness</th>
                    <th>LangChain/LangGraph</th>
                    <th>Winner</th>
                </tr>
            </thead>
            <tbody>
                <tr class="{
        "winner"
        if comparison_data["summary"]["rag"]["native"]["avg_latency_ms"]
        < comparison_data["summary"]["rag"]["langchain"]["avg_latency_ms"]
        else ""
    }">
                    <td>Latency</td>
                    <td>{comparison_data["summary"]["rag"]["native"]["avg_latency_ms"]:.0f}ms</td>
                    <td>{
        comparison_data["summary"]["rag"]["langchain"]["avg_latency_ms"]:.0f}ms</td>
                    <td>{
        "Native"
        if comparison_data["summary"]["rag"]["native"]["avg_latency_ms"]
        < comparison_data["summary"]["rag"]["langchain"]["avg_latency_ms"]
        else "LangChain"
    }</td>
                </tr>
                <tr class="{
        "winner"
        if comparison_data["summary"]["rag"]["native"]["success_rate"]
        > comparison_data["summary"]["rag"]["langchain"]["success_rate"]
        else ""
    }">
                    <td>Success Rate</td>
                    <td>{
        comparison_data["summary"]["rag"]["native"]["success_rate"] * 100:.0f}%</td>
                    <td>{
        comparison_data["summary"]["rag"]["langchain"]["success_rate"] * 100:.0f}%</td>
                    <td>{
        "Native"
        if comparison_data["summary"]["rag"]["native"]["success_rate"]
        > comparison_data["summary"]["rag"]["langchain"]["success_rate"]
        else "LangChain"
    }</td>
                </tr>
                <tr class="{
        "winner"
        if comparison_data["summary"]["rag"]["native"]["avg_keyword_match"]
        > comparison_data["summary"]["rag"]["langchain"]["avg_keyword_match"]
        else ""
    }">
                    <td>Keyword Match</td>
                    <td>{
        comparison_data["summary"]["rag"]["native"]["avg_keyword_match"] * 100:.0f}%</td>
                    <td>{
        comparison_data["summary"]["rag"]["langchain"]["avg_keyword_match"] * 100:.0f}%</td>
                    <td>{
        "Native"
        if comparison_data["summary"]["rag"]["native"]["avg_keyword_match"]
        > comparison_data["summary"]["rag"]["langchain"]["avg_keyword_match"]
        else "LangChain"
    }</td>
                </tr>
                <tr>
                    <td>Code Complexity</td>
                    <td>~450 lines (core)</td>
                    <td>~220 lines (-51%)</td>
                    <td>LangChain</td>
                </tr>
                <tr>
                    <td>Control Granularity</td>
                    <td>Fine-grained (state-level)</td>
                    <td>Framework-abstracted</td>
                    <td>Native</td>
                </tr>
                <tr>
                    <td>Approval Mechanism</td>
                    <td>Explicit ledger + checkpoint</td>
                    <td>Custom interrupt (requires setup)</td>
                    <td>Native</td>
                </tr>
            </tbody>
        </table>
        
        <footer>
            <p>Generated by ForgeHarness Automated Comparison Suite</p>
            <p>For more details, see <code>reports/comparison-detailed.json</code></p>
        </footer>
    </div>
</body>
</html>"""

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html)
    print(f"✅ HTML report generated: {output_path}")


def main() -> None:
    """Generate HTML report from JSON data."""
    json_path = Path("reports/comparison-detailed.json")

    if not json_path.exists():
        print(f"❌ Comparison data not found: {json_path}")
        print("   Run: python evals/run_comparison.py")
        return

    with open(json_path) as f:
        data = json.load(f)

    html_path = Path("reports/comparison-report.html")
    generate_html_report(data, html_path)

    print(f"\n🌐 Open the report: file://{html_path.absolute()}")


if __name__ == "__main__":
    main()
