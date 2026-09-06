"""
LLM-based code generator using OMLX.
"""

from __future__ import annotations

import asyncio

try:
    import httpx

    HTTPX_AVAILABLE = True
except ImportError:
    HTTPX_AVAILABLE = False


class OMLXCodeGenerator:
    """Generate code using OMLX local LLM."""

    def __init__(self, base_url: str = "http://127.0.0.1:8000/v1", model: str = "Qwen3.5-9B-4bit"):
        self.base_url = base_url
        self.model = model
        self.client = httpx.AsyncClient(timeout=60.0) if HTTPX_AVAILABLE else None

    async def generate_code(self, prompt: str, test_description: str = "") -> str:
        """Generate code solution using LLM."""

        if not self.client:
            raise RuntimeError("httpx not available. Install with: uv add httpx")

        # Construct system prompt
        system_prompt = (
            "You are an expert Python programmer. Generate only the requested "
            "function implementation.\n"
            "Rules:\n"
            "1. Include ONLY the function definition and implementation\n"
            "2. Do NOT include test code or examples\n"
            "3. Do NOT include explanations or comments outside the function\n"
            "4. Make sure the function matches the exact signature requested\n"
            "5. Return clean, working Python code"
        )

        # Construct user prompt
        if "HumanEval" in test_description:
            user_prompt = f"""Complete this Python function:

{prompt}

Return ONLY the complete function implementation (including the def line). No explanations."""
        else:
            # MBPP style
            user_prompt = f"""Write a Python function that: {prompt}

Return ONLY the function implementation. No explanations or test code."""

        # Call LLM
        try:
            response = await self.client.post(
                f"{self.base_url}/chat/completions",
                json={
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    "temperature": 0.1,
                    "max_tokens": 1024,
                },
            )

            if response.status_code != 200:
                raise RuntimeError(f"LLM API error: {response.status_code} {response.text}")

            result = response.json()
            generated_code = result["choices"][0]["message"]["content"]

            # Clean up the generated code
            generated_code = self._clean_code(generated_code)

            return generated_code

        except Exception as e:
            raise RuntimeError(f"Failed to generate code: {e}") from e

    def _clean_code(self, code: str) -> str:
        """Clean up generated code."""
        # Remove markdown code blocks
        if "```python" in code:
            code = code.split("```python")[1].split("```")[0]
        elif "```" in code:
            code = code.split("```")[1].split("```")[0]

        # Remove common explanations
        lines = code.split("\n")
        cleaned_lines = []

        for line in lines:
            # Skip explanation lines
            if line.strip().startswith("#") and any(
                word in line.lower() for word in ["example", "test", "note", "usage"]
            ):
                continue
            cleaned_lines.append(line)

        return "\n".join(cleaned_lines).strip()

    async def close(self):
        """Close HTTP client."""
        if self.client:
            await self.client.aclose()


async def test_generator():
    """Test the code generator."""
    generator = OMLXCodeGenerator()

    # Test with a simple problem
    prompt = """def add_numbers(a, b):
    \"\"\" Add two numbers and return the result. \"\"\"
"""

    try:
        code = await generator.generate_code(prompt, "Test")
        print("Generated code:")
        print(code)
    except Exception as e:
        print(f"Error: {e}")
    finally:
        await generator.close()


if __name__ == "__main__":
    asyncio.run(test_generator())
