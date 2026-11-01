"""Read-only artifact/API inventory; never imports host code or opens host data."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import zipfile
from pathlib import Path

from benchmarks.framework_snapshot import snapshot


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def public_names(source: str) -> set[str]:
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "__all__" for target in node.targets
        ):
            return set(ast.literal_eval(node.value))
    raise ValueError("SDK has no literal public export list")


def declared_names(source: str) -> set[str]:
    names = set()
    for node in ast.parse(source).body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
        elif isinstance(node, ast.ImportFrom):
            names.update(alias.asname or alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            names.update(alias.asname or alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.Assign):
            names.update(target.id for target in node.targets if isinstance(target, ast.Name))
    return names


def inspect_host(root: Path, binding_path: str, code: str) -> dict:
    binding_file = root / binding_path
    binding = json.loads(binding_file.read_text())
    version = binding.get("version_label", binding.get("version"))
    if not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("invalid bound distribution version")
    wheel = root / "vendor" / f"forgeharness-{version}-py3-none-any.whl"
    if wheel.is_symlink() or not wheel.resolve().is_relative_to(root.resolve()):
        raise ValueError("bound wheel must be an ordinary host-local file")
    actual = sha256(wheel)
    with zipfile.ZipFile(wheel) as archive:
        exports = public_names(archive.read("forgeharness/sdk/__init__.py").decode())
        modules = {
            name.removesuffix(".py").removesuffix("/__init__").replace("/", "."): declared_names(
                archive.read(name).decode()
            )
            for name in archive.namelist()
            if name.startswith("forgeharness/") and name.endswith(".py")
        }
    imports = []
    for path in sorted((root / code).rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("forgeharness"):
                imports.append(
                    {
                        "file": str(path.relative_to(root)),
                        "line": node.lineno,
                        "source_sha256": sha256(path),
                        "module": node.module,
                        "names": [alias.name for alias in node.names],
                        "sdk_facade": node.module == "forgeharness.sdk",
                        "missing_module_symbols": [
                            alias.name
                            for alias in node.names
                            if alias.name not in modules.get(node.module, set())
                        ],
                        "missing_exports": [
                            alias.name
                            for alias in node.names
                            if node.module == "forgeharness.sdk" and alias.name not in exports
                        ],
                    }
                )
    return {
        "version": version,
        "sdk_api": binding["sdk_api"],
        "binding_path": binding_path,
        "binding_sha256": sha256(binding_file),
        "wheel": str(wheel.relative_to(root)),
        "wheel_sha256": actual,
        "wheel_matches_binding": actual == binding["wheel_sha256"],
        "imports": imports,
        "facade_imports_valid": all(not item["missing_exports"] for item in imports),
        "module_imports_valid": all(not item["missing_module_symbols"] for item in imports),
        "dynamic_imports_checked": False,
        "host_runtime_verified": False,
        "scope": "static imports and exact wheel only; no host execution or migration",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lexvault", type=Path, required=True)
    parser.add_argument("--commerce", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = {
        "schema_version": 1,
        "source_identity": snapshot(Path(__file__).resolve().parents[1])["tree_digest"],
        "hosts": {
            "lexvault": inspect_host(args.lexvault, "vendor/FRAMEWORK_BINDING.json", "app"),
            "commerce": inspect_host(args.commerce, "docs/FRAMEWORK_BINDING.json", "src"),
        },
        "independent_onboarding_measured": False,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    if not all(
        h["wheel_matches_binding"] and h["facade_imports_valid"] and h["module_imports_valid"]
        for h in result["hosts"].values()
    ):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
