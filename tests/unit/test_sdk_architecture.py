"""Core dependency direction and declared compatibility shims."""

import ast
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "src" / "forgeharness"

# Optional-platform adapters may import FastAPI or an MCP client. Nothing in the
# deterministic core may import them, so the base SDK stays usable without extras.
ADAPTERS = ("mcp.py", "control.py", "session_control.py", "task_control.py")


def imports(path: Path) -> list[str]:
    modules = []
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom) and node.module:
            modules.append(node.module)
        elif isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
    return modules


def qualified(path: Path) -> str:
    return "forgeharness." + str(path.relative_to(ROOT).with_suffix("")).replace("/", ".")


def core_files() -> list[Path]:
    core = ("domain", "runtime", "tools", "context", "state", "observability", "sdk")
    return [path for directory in core for path in (ROOT / directory).glob("*.py")]


def test_core_has_no_business_or_optional_platform_imports() -> None:
    forbidden = (
        "forgeharness.coding",
        "forgeharness.applications",
        "forgeharness.knowledge",
        "langgraph",
        "langchain",
        "fastapi",
        "pypdf",
        "PIL",
        "redis",
        "qdrant",
    )
    adapters = {qualified(path) for path in core_files() if path.name in ADAPTERS}
    assert adapters == {
        "forgeharness.sdk.control",
        "forgeharness.sdk.session_control",
        "forgeharness.sdk.task_control",
        "forgeharness.tools.mcp",
    }, sorted(adapters)
    errors = []
    for path in core_files():
        if qualified(path) in adapters:
            continue
        for module in imports(path):
            if module.startswith(forbidden) or any(
                module == adapter or module.startswith(adapter + ".") for adapter in adapters
            ):
                errors.append(f"{path.relative_to(ROOT)} -> {module}")
    assert not errors, errors


def test_optional_adapters_declare_their_platform_dependency() -> None:
    """The adapter exemption above must stay a real boundary, not a dead list."""
    expected = {
        "sdk/control.py": "fastapi",
        "sdk/session_control.py": "fastapi",
        "sdk/task_control.py": "fastapi",
        "tools/mcp.py": "httpx",
    }
    for name, dependency in expected.items():
        modules = imports(ROOT / name)
        assert any(module.split(".")[0] == dependency for module in modules), (
            f"{name} no longer imports its optional platform dependency {dependency}"
        )


def test_package_import_does_not_pull_optional_platforms() -> None:
    """`import forgeharness` must stay on the core dependencies alone."""
    optional = ("fastapi", "httpx", "langgraph", "langchain", "qdrant_client", "redis")
    code = (
        "import sys, forgeharness; "
        "loaded = {name.split('.')[0] for name in sys.modules}; "
        f"leaked = sorted(set({optional!r}) & loaded); "
        "assert not leaked, leaked; "
        "print(forgeharness.__version__)"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0, completed.stderr


def test_reference_apps_use_public_sdk_contracts() -> None:
    files = list((ROOT / "applications").rglob("*.py"))
    files += [
        ROOT / "coding" / name
        for name in (
            "application.py",
            "agent.py",
            "tools.py",
            "policy.py",
            "verification.py",
            "repository.py",
        )
    ]
    allowed = (
        "forgeharness.sdk",
        "forgeharness.coding",
        "forgeharness.applications",
        "forgeharness.skills",
    )
    errors = []
    for path in files:
        for module in imports(path):
            if module.startswith("forgeharness.") and not module.startswith(allowed):
                errors.append(f"{path.relative_to(ROOT)} -> {module}")
    assert not errors, errors


def test_sdist_has_explicit_public_input_allowlist() -> None:
    import tomllib

    config = tomllib.loads((ROOT.parents[1] / "pyproject.toml").read_text())
    assert config["tool"]["hatch"]["build"]["targets"]["sdist"]["include"] == [
        "src/forgeharness",
        "pyproject.toml",
        "README.md",
        "LICENSE",
        "docs",
        "examples/sdk_demo.py",
        "examples/business_modules_demo.py",
        "examples/session_demo.py",
        "examples/import_tasks_demo.py",
        "examples/legal_pilot_demo.py",
        "examples/commerce_demo.py",
        "examples/commerce_state.py",
        "examples/commerce_workbench.py",
        "examples/workbench_smoke.py",
        "examples/commerce_readonly_demo.py",
        "examples/durable_demo.py",
        "examples/collaboration_demo.py",
        "examples/parallel_demo.py",
        "examples/fanout_demo.py",
        "examples/cloud_state_demo.py",
        "examples/cloud_hosted_demo.py",
        "data/synthetic",
    ]
    assert config["tool"]["hatch"]["build"]["targets"]["sdist"]["exclude"] == [
        "/evals",
        "/benchmarks",
        "/tests",
        "/reports",
        "/.github",
        "/deploy",
        "/data/sealed",
    ]
