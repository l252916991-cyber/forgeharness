"""
Comprehensive test cases for framework comparison.

Tests both Native ForgeHarness and LangChain implementations on:
1. RAG retrieval quality and latency
2. Agent task completion and tool usage
3. Edge cases and error handling
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class RAGTestCase:
    """Single RAG test case with expected behavior."""

    id: str
    query: str
    expected_keywords: list[str]  # Keywords that should appear in answer
    expected_sources: list[str]  # Document sources that should be cited
    category: str  # "factual" | "semantic" | "multi_hop" | "edge_case"
    difficulty: str  # "easy" | "medium" | "hard"


@dataclass
class AgentTestCase:
    """Single Agent coding task test case."""

    id: str
    task_description: str
    initial_files: dict[str, str]  # filename -> content
    expected_changes: list[str]  # Files that should be modified
    test_command: str
    success_criteria: str  # What defines success
    category: str  # "simple_fix" | "feature_add" | "refactor" | "test_write"
    difficulty: str  # "easy" | "medium" | "hard"


# ======================
# RAG Test Cases
# ======================

RAG_TEST_CASES = [
    # ===== Easy: Factual Questions =====
    RAGTestCase(
        id="rag_easy_01",
        query="What is ForgeHarness?",
        expected_keywords=["agent", "harness", "evaluation", "coding"],
        expected_sources=["README.md", "ARCHITECTURE.md"],
        category="factual",
        difficulty="easy",
    ),
    RAGTestCase(
        id="rag_easy_02",
        query="What programming language is ForgeHarness written in?",
        expected_keywords=["python", "3.12"],
        expected_sources=["README.md", "pyproject.toml"],
        category="factual",
        difficulty="easy",
    ),
    RAGTestCase(
        id="rag_easy_03",
        query="What databases does ForgeHarness support?",
        expected_keywords=["sqlite", "postgresql", "qdrant", "redis"],
        expected_sources=["ARCHITECTURE.md", "README.md"],
        category="factual",
        difficulty="easy",
    ),
    # ===== Medium: Semantic Understanding =====
    RAGTestCase(
        id="rag_medium_01",
        query="How does the approval mechanism work?",
        expected_keywords=["approval", "grant", "ledger", "checkpoint", "suspend"],
        expected_sources=["ARCHITECTURE.md", "REVIEW.md"],
        category="semantic",
        difficulty="medium",
    ),
    RAGTestCase(
        id="rag_medium_02",
        query="What is the difference between ReAct and Plan-Execute?",
        expected_keywords=["react", "plan", "execute", "loop", "strategy"],
        expected_sources=["ARCHITECTURE.md"],
        category="semantic",
        difficulty="medium",
    ),
    RAGTestCase(
        id="rag_medium_03",
        query="How does ForgeHarness handle memory management?",
        expected_keywords=["memory", "context", "budget", "window", "compression"],
        expected_sources=["ARCHITECTURE.md"],
        category="semantic",
        difficulty="medium",
    ),
    RAGTestCase(
        id="rag_medium_04",
        query="What evaluation metrics does ForgeHarness use?",
        expected_keywords=["evaluation", "metrics", "rag", "control", "benchmark"],
        expected_sources=["EVALUATION.md", "STATUS.md"],
        category="semantic",
        difficulty="medium",
    ),
    # ===== Hard: Multi-hop Reasoning =====
    RAGTestCase(
        id="rag_hard_01",
        query=(
            "How does ForgeHarness ensure Agent tasks don't exceed budget "
            "and what happens when they do?"
        ),
        expected_keywords=["budget", "step", "tool", "token", "exhausted", "limit"],
        expected_sources=["ARCHITECTURE.md", "domain types"],
        category="multi_hop",
        difficulty="hard",
    ),
    RAGTestCase(
        id="rag_hard_02",
        query="Compare the native implementation and LangChain version of ForgeHarness",
        expected_keywords=["native", "langchain", "lcel", "langgraph", "comparison", "trade-off"],
        expected_sources=["LANGCHAIN_COMPARISON.md", "FRAMEWORK_IMPLEMENTATION_SUMMARY.md"],
        category="multi_hop",
        difficulty="hard",
    ),
    RAGTestCase(
        id="rag_hard_03",
        query="What are the security boundaries in ForgeHarness and how are they enforced?",
        expected_keywords=["security", "trust", "boundary", "validation", "sandbox", "path"],
        expected_sources=["ARCHITECTURE.md", "REVIEW.md"],
        category="multi_hop",
        difficulty="hard",
    ),
    # ===== Edge Cases =====
    RAGTestCase(
        id="rag_edge_01",
        query="What is the meaning of life?",  # Unrelated to docs
        expected_keywords=["cannot", "answer", "documentation", "information"],
        expected_sources=[],
        category="edge_case",
        difficulty="easy",
    ),
    RAGTestCase(
        id="rag_edge_02",
        query="How to hack ForgeHarness?",  # Adversarial
        expected_keywords=["security", "boundaries", "validation"],
        expected_sources=["ARCHITECTURE.md"],
        category="edge_case",
        difficulty="medium",
    ),
    RAGTestCase(
        id="rag_edge_03",
        query="",  # Empty query
        expected_keywords=[],
        expected_sources=[],
        category="edge_case",
        difficulty="easy",
    ),
]


# ======================
# Agent Test Cases
# ======================

AGENT_TEST_CASES = [
    # ===== Easy: Simple Bug Fix =====
    AgentTestCase(
        id="agent_easy_01",
        task_description="Fix the typo in the function name 'calcualte' to 'calculate'",
        initial_files={
            "math_utils.py": """
def calcualte_sum(a, b):
    return a + b

def test_sum():
    assert calcualte_sum(2, 3) == 5
"""
        },
        expected_changes=["math_utils.py"],
        test_command="python -m pytest -xvs",
        success_criteria="All tests pass and typo is fixed",
        category="simple_fix",
        difficulty="easy",
    ),
    # ===== Easy: Add Simple Function =====
    AgentTestCase(
        id="agent_easy_02",
        task_description="Add a subtract function and a test for it",
        initial_files={
            "calculator.py": """
def add(a, b):
    return a + b

def test_add():
    assert add(2, 3) == 5
"""
        },
        expected_changes=["calculator.py"],
        test_command="python -m pytest -xvs",
        success_criteria="subtract function works and tests pass",
        category="feature_add",
        difficulty="easy",
    ),
    # ===== Medium: Logic Bug Fix =====
    AgentTestCase(
        id="agent_medium_01",
        task_description="Fix the off-by-one error in the range function",
        initial_files={
            "sequence.py": """
def get_range(start, end):
    '''Return list of integers from start to end (exclusive)'''
    return list(range(start, end + 1))  # Bug: should be exclusive

def test_range():
    assert get_range(1, 5) == [1, 2, 3, 4]  # Fails: returns [1,2,3,4,5]
"""
        },
        expected_changes=["sequence.py"],
        test_command="python -m pytest -xvs",
        success_criteria="Test passes with correct exclusive range",
        category="simple_fix",
        difficulty="medium",
    ),
    # ===== Medium: Feature Implementation =====
    AgentTestCase(
        id="agent_medium_02",
        task_description=(
            "Implement a function to find the longest common prefix of a list of strings"
        ),
        initial_files={
            "string_utils.py": """
def longest_common_prefix(strings):
    '''Find the longest common prefix of all strings.
    
    Example:
        >>> longest_common_prefix(['flower', 'flow', 'flight'])
        'fl'
    '''
    # TODO: Implement this function
    pass

def test_longest_common_prefix():
    assert longest_common_prefix(['flower', 'flow', 'flight']) == 'fl'
    assert longest_common_prefix(['dog', 'racecar', 'car']) == ''
    assert longest_common_prefix(['']) == ''
"""
        },
        expected_changes=["string_utils.py"],
        test_command="python -m pytest -xvs",
        success_criteria="Function implemented and all tests pass",
        category="feature_add",
        difficulty="medium",
    ),
    # ===== Hard: Refactoring =====
    AgentTestCase(
        id="agent_hard_01",
        task_description=(
            "Refactor the deeply nested conditional into separate validation functions"
        ),
        initial_files={
            "validator.py": """
def validate_user(user):
    if user is not None:
        if 'name' in user:
            if len(user['name']) > 0:
                if 'email' in user:
                    if '@' in user['email']:
                        if 'age' in user:
                            if user['age'] >= 18:
                                return True
    return False

def test_validator():
    assert validate_user({'name': 'Alice', 'email': 'alice@example.com', 'age': 25}) == True
    assert validate_user({'name': '', 'email': 'alice@example.com', 'age': 25}) == False
    assert validate_user({'name': 'Bob', 'email': 'invalid', 'age': 25}) == False
    assert validate_user({'name': 'Charlie', 'email': 'charlie@example.com', 'age': 17}) == False
"""
        },
        expected_changes=["validator.py"],
        test_command="python -m pytest -xvs",
        success_criteria="Code refactored into separate functions, all tests pass",
        category="refactor",
        difficulty="hard",
    ),
    # ===== Hard: Write Comprehensive Tests =====
    AgentTestCase(
        id="agent_hard_02",
        task_description=(
            "Write comprehensive tests for the binary search function including edge cases"
        ),
        initial_files={
            "search.py": """
def binary_search(arr, target):
    '''Binary search in sorted array.
    
    Returns: index of target, or -1 if not found
    '''
    left, right = 0, len(arr) - 1
    
    while left <= right:
        mid = (left + right) // 2
        if arr[mid] == target:
            return mid
        elif arr[mid] < target:
            left = mid + 1
        else:
            right = mid - 1
    
    return -1

# TODO: Write comprehensive tests covering:
# - Normal cases (found, not found)
# - Edge cases (empty array, single element, target at boundaries)
# - Large arrays
"""
        },
        expected_changes=["search.py"],
        test_command="python -m pytest -xvs",
        success_criteria="Comprehensive test suite written and passes",
        category="test_write",
        difficulty="hard",
    ),
]


def export_test_cases(output_dir: Path) -> None:
    """Export test cases to JSON files."""
    output_dir.mkdir(parents=True, exist_ok=True)

    # Export RAG test cases
    rag_output = output_dir / "rag_test_cases.json"
    with open(rag_output, "w") as f:
        json.dump(
            {
                "meta": {
                    "total": len(RAG_TEST_CASES),
                    "categories": {
                        "factual": len([t for t in RAG_TEST_CASES if t.category == "factual"]),
                        "semantic": len([t for t in RAG_TEST_CASES if t.category == "semantic"]),
                        "multi_hop": len([t for t in RAG_TEST_CASES if t.category == "multi_hop"]),
                        "edge_case": len([t for t in RAG_TEST_CASES if t.category == "edge_case"]),
                    },
                    "difficulties": {
                        "easy": len([t for t in RAG_TEST_CASES if t.difficulty == "easy"]),
                        "medium": len([t for t in RAG_TEST_CASES if t.difficulty == "medium"]),
                        "hard": len([t for t in RAG_TEST_CASES if t.difficulty == "hard"]),
                    },
                },
                "test_cases": [asdict(t) for t in RAG_TEST_CASES],
            },
            f,
            indent=2,
        )

    # Export Agent test cases
    agent_output = output_dir / "agent_test_cases.json"
    with open(agent_output, "w") as f:
        json.dump(
            {
                "meta": {
                    "total": len(AGENT_TEST_CASES),
                    "categories": {
                        "simple_fix": len(
                            [t for t in AGENT_TEST_CASES if t.category == "simple_fix"]
                        ),
                        "feature_add": len(
                            [t for t in AGENT_TEST_CASES if t.category == "feature_add"]
                        ),
                        "refactor": len([t for t in AGENT_TEST_CASES if t.category == "refactor"]),
                        "test_write": len(
                            [t for t in AGENT_TEST_CASES if t.category == "test_write"]
                        ),
                    },
                    "difficulties": {
                        "easy": len([t for t in AGENT_TEST_CASES if t.difficulty == "easy"]),
                        "medium": len([t for t in AGENT_TEST_CASES if t.difficulty == "medium"]),
                        "hard": len([t for t in AGENT_TEST_CASES if t.difficulty == "hard"]),
                    },
                },
                "test_cases": [asdict(t) for t in AGENT_TEST_CASES],
            },
            f,
            indent=2,
        )

    print(f"✅ Exported {len(RAG_TEST_CASES)} RAG test cases to {rag_output}")
    print(f"✅ Exported {len(AGENT_TEST_CASES)} Agent test cases to {agent_output}")


if __name__ == "__main__":
    export_test_cases(Path("evals"))
