"""Core must not import applications; three Coding shims are explicitly frozen.

The shims exist for API v1 compatibility and are removed at API v2. Any new
core -> coding/applications import should fail this test before it becomes a
dependency cycle. The scan covers static imports AND ``import_module("...")``
string literals, so a lazy shim cannot hide from the allowlist.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "forgeharness"
CORE_PACKAGES = (
    "domain",
    "models",
    "runtime",
    "tools",
    "context",
    "state",
    "observability",
    "sdk",
)

# (relative path, imported module) allowed only for API v1 compatibility.
ALLOWED_CODING_IMPORTS = {
    ("runtime/verification.py", "forgeharness.coding.verification"),
    ("context/__init__.py", "forgeharness.coding.repository"),
    ("context/repository.py", "forgeharness.coding.repository"),
}


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            modules.add(node.module)
        elif isinstance(node, ast.Call):
            # Dynamic imports are imports too: ``import_module("forgeharness.x")``
            # with a literal argument must not slip past the static scan.
            name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            if name in {"import_module", "__import__"} and node.args:
                first = node.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    modules.add(first.value)
    return modules


def _core_python_files() -> list[Path]:
    files: list[Path] = []
    for package in CORE_PACKAGES:
        files.extend(sorted((SRC / package).rglob("*.py")))
    return files


def test_core_does_not_import_applications() -> None:
    findings = []
    for path in _core_python_files():
        relative = path.relative_to(SRC).as_posix()
        for module in sorted(_imported_modules(path)):
            if module.startswith("forgeharness.applications"):
                findings.append((relative, module))
    assert findings == [], f"core imports an application: {findings}"


def test_only_declared_coding_shims_remain_in_core() -> None:
    findings = []
    for path in _core_python_files():
        relative = path.relative_to(SRC).as_posix()
        for module in sorted(_imported_modules(path)):
            if module.startswith("forgeharness.coding"):
                finding = (relative, module)
                if finding not in ALLOWED_CODING_IMPORTS:
                    findings.append(finding)
    assert findings == [], f"undeclared core -> coding import: {findings}"


def test_dynamic_import_strings_are_detected(tmp_path: Path) -> None:
    sample = tmp_path / "sample.py"
    sample.write_text(
        "from importlib import import_module\n"
        "\n"
        "\n"
        "def load():\n"
        '    return import_module("forgeharness.applications.legal")\n',
        encoding="utf-8",
    )
    assert "forgeharness.applications.legal" in _imported_modules(sample)


def test_importing_core_does_not_load_applications_or_coding() -> None:
    imports = "; ".join(f"import forgeharness.{package}" for package in CORE_PACKAGES)
    script = (
        f"{imports}; "
        "import sys; "
        "print('\\n'.join(sorted("
        "name for name in sys.modules "
        "if name.startswith('forgeharness.applications') or "
        "name.startswith('forgeharness.coding')"
        ")))"
    )
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "", (
        f"importing core loaded an application/coding module: {completed.stdout.strip()}"
    )
