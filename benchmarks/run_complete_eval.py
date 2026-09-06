"""
Complete end-to-end LLM evaluation: Generate + Execute + Verify
"""

import asyncio
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from tqdm import tqdm

sys.path.insert(0, "src")

from forgeharness.models.omlx import OMLXChatModel, OMLXClient, OMLXConfig


class CompleteEvaluator:
    """Complete LLM evaluation with code generation, execution, and verification."""

    def __init__(self):
        self.config = OMLXConfig(chat_model="Qwythos-9B-v2-4bit-mlx")
        self.client = None
        self.chat_model = None

    async def initialize(self):
        self.client = OMLXClient(self.config)
        self.chat_model = OMLXChatModel(self.client)

    async def close(self):
        if self.client:
            await self.client.close()

    async def generate_code(self, test: dict) -> str:
        """Generate code using LLM."""
        prompt = test.get("prompt") or test.get("text", "")

        system_prompt = (
            "You are an expert Python programmer. Generate only the Python code "
            "requested, without explanations or markdown."
        )
        user_prompt = (
            f"Write Python code for this task:\n\n{prompt}\n\n"
            "Requirements:\n- Complete, executable code\n"
            "- Include function definition\n- No explanations"
        )

        try:
            code = await self.chat_model.complete(system=system_prompt, user=user_prompt)
            code = code.strip()

            # Clean markdown
            if code.startswith("```python"):
                code = code[len("```python") :].strip()
            elif code.startswith("```"):
                code = code[len("```") :].strip()
            if code.endswith("```"):
                code = code[:-3].strip()

            return code
        except Exception:
            return ""

    def execute_test(self, generated_code: str, test: dict) -> tuple[bool, str]:
        """Execute generated code against test cases."""

        source = test.get("source", "unknown")
        test_code = "\n".join(test.get("test_list", []) or test.get("test", []))

        if not generated_code or not test_code:
            return False, "Empty code or test"

        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)

            # Write solution
            solution_file = tmpdir / "solution.py"
            solution_file.write_text(generated_code)

            # Write test
            test_file = tmpdir / "test_solution.py"

            if source == "humaneval":
                # HumanEval style
                test_content = f"""
import sys
sys.path.insert(0, '{tmpdir}')

from solution import *

def check(candidate):
{chr(10).join("    " + line for line in test_code.split(chr(10)) if line.strip())}

try:
    check(locals()['{self._extract_function_name(generated_code)}'])
    print("TESTS_PASSED")
except Exception as e:
    print(f"TEST_FAILED: {{e}}")
    sys.exit(1)
"""
            else:
                # MBPP style
                indented_test = "\n".join(
                    "    " + line if line.strip() else "" for line in test_code.split("\n")
                )
                test_content = f"""
import sys
sys.path.insert(0, '{tmpdir}')

from solution import *

try:
{indented_test}
    print("TESTS_PASSED")
except AssertionError as e:
    print(f"ASSERTION_FAILED: {{e}}")
    sys.exit(1)
except Exception as e:
    print(f"EXECUTION_ERROR: {{e}}")
    sys.exit(1)
"""

            test_file.write_text(test_content)

            # Execute
            try:
                result = subprocess.run(
                    [sys.executable, str(test_file)],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    cwd=tmpdir,
                )

                output = result.stdout.strip()

                if "TESTS_PASSED" in output:
                    return True, "Passed"
                else:
                    error_msg = result.stderr or result.stdout or "Unknown error"
                    return False, error_msg[:200]

            except subprocess.TimeoutExpired:
                return False, "Timeout"
            except Exception as e:
                return False, str(e)[:200]

    def _extract_function_name(self, code: str) -> str:
        """Extract function name from code."""
        for line in code.split("\n"):
            if line.strip().startswith("def "):
                name = line.split("def ")[1].split("(")[0].strip()
                return name
        return "unknown"


async def run_complete_evaluation(max_tests: int = 50):
    """Run complete end-to-end evaluation."""

    print("=" * 70)
    print("ForgeHarness Complete LLM Evaluation")
    print("=" * 70)
    print()
    print("Phase 1: Generate code with LLM")
    print("Phase 2: Execute generated code")
    print("Phase 3: Verify correctness")
    print()

    # Load tests
    mbpp_file = Path("data/large-scale-tests/mbpp.json")
    humaneval_file = Path("data/large-scale-tests/humaneval.json")

    tests = []
    if humaneval_file.exists():
        with open(humaneval_file) as f:
            data = json.load(f)
        tests.extend([{**t, "source": "humaneval"} for t in data[:5]])

    if mbpp_file.exists():
        with open(mbpp_file) as f:
            data = json.load(f)
        tests.extend([{**t, "source": "mbpp"} for t in data[: max_tests - 5]])

    tests = tests[:max_tests]

    print(f"📊 Loaded {len(tests)} test cases")
    print()

    evaluator = CompleteEvaluator()

    try:
        print("📡 Connecting to OMLX...")
        await evaluator.initialize()
        print(f"✅ Using model: {evaluator.config.chat_model}")
        print()

        print("🚀 Running complete evaluation...")
        print()

        results = []
        passed = 0
        failed = 0
        generation_failed = 0

        for i, test in enumerate(tqdm(tests, desc="Testing")):
            task_id = test.get("task_id", i)
            source = test.get("source", "unknown")

            # Phase 1: Generate
            generated_code = await evaluator.generate_code(test)

            if not generated_code:
                result = {
                    "task_id": task_id,
                    "source": source,
                    "passed": False,
                    "phase": "generation",
                    "error": "Failed to generate code",
                }
                generation_failed += 1
                failed += 1
            else:
                # Phase 2 & 3: Execute and verify
                test_passed, error_msg = evaluator.execute_test(generated_code, test)

                result = {
                    "task_id": task_id,
                    "source": source,
                    "passed": test_passed,
                    "phase": "execution",
                    "generated_code": generated_code[:300],
                    "error": None if test_passed else error_msg,
                }

                if test_passed:
                    passed += 1
                else:
                    failed += 1

            results.append(result)

            # Progress every 10 tests
            if (i + 1) % 10 == 0:
                current_pass_rate = (passed / (i + 1)) * 100
                print(f"\n  Progress: {i + 1}/{len(tests)} - Pass rate: {current_pass_rate:.1f}%")

        print()
        print("=" * 70)
        print("COMPLETE EVALUATION RESULTS")
        print("=" * 70)
        print()
        print(f"Model:                  {evaluator.config.chat_model}")
        print(f"Total Tests:            {len(tests)}")
        print(f"Passed (Correct):       {passed}")
        print(f"Failed (Wrong/Error):   {failed}")
        print(f"Generation Failed:      {generation_failed}")
        print()
        print(f"Pass@1:                 {(passed / len(tests) * 100):.1f}%")
        print()

        # By source
        humaneval_results = [r for r in results if r["source"] == "humaneval"]
        mbpp_results = [r for r in results if r["source"] == "mbpp"]

        if humaneval_results:
            he_passed = sum(1 for r in humaneval_results if r["passed"])
            he_total = len(humaneval_results)
            print(f"HumanEval:  {he_passed}/{he_total} ({he_passed / he_total * 100:.1f}%)")

        if mbpp_results:
            mbpp_passed = sum(1 for r in mbpp_results if r["passed"])
            mbpp_total = len(mbpp_results)
            print(f"MBPP:       {mbpp_passed}/{mbpp_total} ({mbpp_passed / mbpp_total * 100:.1f}%)")

        print()
        print("=" * 70)
        print("vs Industry Benchmarks")
        print("=" * 70)
        print()
        print("  AutoGPT pass@1:      ~10%")
        print("  MetaGPT pass@1:      ~13%")
        print("  Claude-3.5:          ~64%")
        print("  GPT-4:               ~67%")
        print()
        print(f"  Qwythos-9B (ours):   {(passed / len(tests) * 100):.1f}%")
        print()
        print("=" * 70)

        # Save report
        report_path = Path("reports/complete-llm-evaluation.json")
        report = {
            "model": evaluator.config.chat_model,
            "total_tests": len(tests),
            "passed": passed,
            "failed": failed,
            "generation_failed": generation_failed,
            "pass_rate": passed / len(tests),
            "pass_at_1": passed / len(tests),
            "results": results,
        }

        with open(report_path, "w") as f:
            json.dump(report, f, indent=2)

        print(f"📄 Report saved to: {report_path}")
        print()

    finally:
        await evaluator.close()


if __name__ == "__main__":
    asyncio.run(run_complete_evaluation(max_tests=50))
