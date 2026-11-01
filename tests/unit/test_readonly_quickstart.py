import os
import subprocess
import sys
from pathlib import Path


def test_documented_quickstart_runs_and_reopens_from_another_process(tmp_path):
    root = Path(__file__).resolve().parents[2]
    markdown = (root / "docs/QUICKSTART_READONLY.md").read_text()
    code = markdown.split("```python\n", 1)[1].split("```", 1)[0]
    script = tmp_path / "demo.py"
    script.write_text(code)
    for _ in range(2):
        result = subprocess.run(
            [sys.executable, str(script)],
            cwd=tmp_path,
            env={**os.environ, "PYTHONPATH": str(root / "src")},
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert "succeeded hello from a registered tool" in result.stdout
