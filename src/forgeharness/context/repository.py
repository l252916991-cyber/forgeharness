"""Safe Python repository mapping using the standard AST."""

from __future__ import annotations

import ast
from pathlib import Path

from pydantic import Field

from forgeharness.domain.models import FrozenModel
from forgeharness.tools.paths import resolve_workspace_path


class Symbol(FrozenModel):
    """One top-level Python symbol with a source location."""

    kind: str
    name: str
    line: int = Field(ge=1)


class FileMap(FrozenModel):
    """A repository-relative Python file and its parseable symbols."""

    path: str
    symbols: tuple[Symbol, ...]
    parse_error: str | None = None


class PythonRepositoryMap:
    """Build a deterministic symbol map without importing repository code."""

    def __init__(self, *, max_files: int = 2_000, max_file_bytes: int = 1_000_000) -> None:
        if max_files < 1 or max_file_bytes < 1:
            raise ValueError("repository map limits must be positive")
        self._max_files = max_files
        self._max_file_bytes = max_file_bytes

    def build(self, workspace: Path) -> tuple[FileMap, ...]:
        """Parse bounded `.py` files and report syntax errors as data."""
        root = workspace.resolve(strict=True)
        paths = sorted(
            path
            for path in root.rglob("*.py")
            if ".git" not in path.parts and path.is_file() and not path.is_symlink()
        )[: self._max_files]
        mapped: list[FileMap] = []
        for path in paths:
            safe = resolve_workspace_path(root, str(path.relative_to(root)), must_exist=True)
            if safe.stat().st_size > self._max_file_bytes:
                mapped.append(
                    FileMap(
                        path=str(path.relative_to(root)), symbols=(), parse_error="file too large"
                    )
                )
                continue
            try:
                tree = ast.parse(safe.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError) as exc:
                mapped.append(
                    FileMap(
                        path=str(path.relative_to(root)),
                        symbols=(),
                        parse_error=f"{type(exc).__name__}: {exc}",
                    )
                )
                continue
            symbols = tuple(
                Symbol(
                    kind="class" if isinstance(node, ast.ClassDef) else "function",
                    name=node.name,
                    line=node.lineno,
                )
                for node in tree.body
                if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
            )
            mapped.append(FileMap(path=str(path.relative_to(root)), symbols=symbols))
        return tuple(mapped)
