"""Safe Python repository mapping using the standard AST."""

from __future__ import annotations

import ast
from contextlib import closing
from pathlib import Path

from pydantic import Field

from forgeharness.sdk import (
    FileTooLarge,
    FrozenModel,
    LocalBackend,
    NotRegularFile,
    WorkspacePathError,
)


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
        backend = LocalBackend(workspace)
        paths: list[str] = []
        with closing(backend.iter_files()) as files:
            for entry in files:
                if entry.path.endswith(".py"):
                    paths.append(entry.path)
                    if len(paths) >= self._max_files:
                        break
        mapped: list[FileMap] = []
        for path in paths:
            try:
                text = backend.read_text(path, max_bytes=self._max_file_bytes).text
            except FileTooLarge:
                mapped.append(FileMap(path=path, symbols=(), parse_error="file too large"))
                continue
            except (FileNotFoundError, WorkspacePathError, NotRegularFile):
                mapped.append(FileMap(path=path, symbols=(), parse_error="file changed during map"))
                continue
            except UnicodeDecodeError as exc:
                mapped.append(
                    FileMap(path=path, symbols=(), parse_error=f"{type(exc).__name__}: {exc}")
                )
                continue
            try:
                tree = ast.parse(text)
            except SyntaxError as exc:
                mapped.append(
                    FileMap(
                        path=path,
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
            mapped.append(FileMap(path=path, symbols=symbols))
        return tuple(mapped)
