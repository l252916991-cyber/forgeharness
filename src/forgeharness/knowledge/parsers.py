"""Bounded, source-preserving document parsing for local knowledge bases."""

from __future__ import annotations

import ast
import hashlib
import io
import json
import re
from collections.abc import Iterable
from itertools import pairwise
from pathlib import Path, PurePath

from PIL import Image, UnidentifiedImageError

from forgeharness.knowledge.models import Chunk, Document

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
TEXT_EXTENSIONS = {
    ".md",
    ".txt",
    ".py",
    ".json",
    ".toml",
    ".yaml",
    ".yml",
    ".js",
    ".ts",
    ".tsx",
    ".java",
    ".go",
    ".rs",
}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg"}
ALLOWED_EXTENSIONS = TEXT_EXTENSIONS | IMAGE_EXTENSIONS | {".pdf"}


class DocumentParseError(ValueError):
    """An upload cannot be safely represented as source-linked chunks."""


class LocalDocumentParser:
    """Parse application-owned bytes without following user-supplied paths."""

    def parse(
        self, *, filename: str, data: bytes, mime_type: str | None = None
    ) -> tuple[Document, tuple[Chunk, ...]]:
        safe_name = self._validate_name(filename)
        if not data:
            raise DocumentParseError("uploaded document is empty")
        if len(data) > MAX_UPLOAD_BYTES:
            raise DocumentParseError("uploaded document exceeds 10 MiB")
        extension = Path(safe_name).suffix.lower()
        if extension not in ALLOWED_EXTENSIONS:
            raise DocumentParseError(f"unsupported document extension: {extension or '<none>'}")
        digest = hashlib.sha256(data).hexdigest()
        resolved_mime = mime_type or self._mime_for(extension)
        document = Document(
            id=digest,
            source=safe_name,
            mime_type=resolved_mime,
            size_bytes=len(data),
        )
        if extension in IMAGE_EXTENSIONS:
            self._validate_image(data, extension)
            return document, ()
        if extension == ".pdf":
            return document, self._parse_pdf(document, data)
        text = self._decode(data)
        if extension == ".json":
            try:
                text = json.dumps(json.loads(text), ensure_ascii=False, indent=2, sort_keys=True)
            except json.JSONDecodeError as exc:
                raise DocumentParseError(f"invalid JSON: {exc.msg}") from exc
        if extension == ".py":
            chunks = self._python_chunks(document, text)
        elif extension == ".md":
            chunks = self._markdown_chunks(document, text)
        else:
            chunks = self._text_chunks(document, text)
        if not chunks:
            raise DocumentParseError("document contains no searchable text")
        return document, chunks

    @staticmethod
    def image_chunk(document: Document, description: str) -> Chunk:
        """Turn model-observed image evidence into one citable derived chunk."""
        content = description.strip()
        if not content:
            raise DocumentParseError("image model returned an empty description")
        return _chunk(document, content, kind="image_description")

    @staticmethod
    def _validate_name(filename: str) -> str:
        if not filename or len(filename) > 500 or "\x00" in filename:
            raise DocumentParseError("invalid upload filename")
        pure = PurePath(filename)
        if pure.is_absolute() or len(pure.parts) != 1 or pure.name in {".", ".."}:
            raise DocumentParseError("filename must not contain a path")
        return pure.name

    @staticmethod
    def _decode(data: bytes) -> str:
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise DocumentParseError("text documents must be UTF-8") from exc

    @staticmethod
    def _mime_for(extension: str) -> str:
        return {
            ".md": "text/markdown",
            ".txt": "text/plain",
            ".py": "text/x-python",
            ".json": "application/json",
            ".pdf": "application/pdf",
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
        }.get(extension, "text/plain")

    @staticmethod
    def _validate_image(data: bytes, extension: str) -> None:
        expected = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG"}[extension]
        try:
            with Image.open(io.BytesIO(data)) as image:
                if image.format != expected:
                    raise DocumentParseError(
                        f"image content is {image.format or 'unknown'}, not {expected}"
                    )
                image.verify()
        except DocumentParseError:
            raise
        except (UnidentifiedImageError, OSError, ValueError) as exc:
            raise DocumentParseError("unable to parse image") from exc

    def _parse_pdf(self, document: Document, data: bytes) -> tuple[Chunk, ...]:
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise DocumentParseError("PDF support requires the 'platform' extra") from exc
        try:
            reader = PdfReader(io.BytesIO(data))
            chunks = []
            for page_number, page in enumerate(reader.pages, start=1):
                text = (page.extract_text() or "").strip()
                for content, start, end in _window_lines(text):
                    chunks.append(
                        _chunk(
                            document,
                            content,
                            start_line=start,
                            end_line=end,
                            page=page_number,
                            kind="pdf",
                        )
                    )
            return tuple(chunks)
        except Exception as exc:
            raise DocumentParseError(f"unable to parse PDF: {exc}") from exc

    def _text_chunks(self, document: Document, text: str) -> tuple[Chunk, ...]:
        return tuple(
            _chunk(document, content, start_line=start, end_line=end)
            for content, start, end in _window_lines(text)
        )

    def _markdown_chunks(self, document: Document, text: str) -> tuple[Chunk, ...]:
        lines = text.splitlines()
        boundaries = [index for index, line in enumerate(lines) if re.match(r"^#{1,6}\s+", line)]
        if not boundaries or boundaries[0] != 0:
            boundaries.insert(0, 0)
        boundaries.append(len(lines))
        chunks = []
        for begin, finish in pairwise(boundaries):
            section = "\n".join(lines[begin:finish]).strip()
            for content, offset_start, offset_end in _window_lines(section):
                chunks.append(
                    _chunk(
                        document,
                        content,
                        start_line=begin + offset_start,
                        end_line=begin + offset_end,
                        kind="markdown",
                    )
                )
        return tuple(chunks)

    def _python_chunks(self, document: Document, text: str) -> tuple[Chunk, ...]:
        lines = text.splitlines()
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return self._text_chunks(document, text)
        nodes = [
            node
            for node in tree.body
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        chunks: list[Chunk] = []
        first_symbol_line = min((node.lineno for node in nodes), default=len(lines) + 1)
        prefix = "\n".join(lines[: first_symbol_line - 1]).strip()
        if prefix:
            chunks.append(
                _chunk(
                    document,
                    prefix,
                    start_line=1,
                    end_line=first_symbol_line - 1,
                    kind="python_module",
                )
            )
        for node in nodes:
            end_line = getattr(node, "end_lineno", node.lineno)
            content = "\n".join(lines[node.lineno - 1 : end_line]).strip()
            if content:
                kind = "python_class" if isinstance(node, ast.ClassDef) else "python_function"
                chunks.append(
                    _chunk(
                        document,
                        content,
                        start_line=node.lineno,
                        end_line=end_line,
                        kind=kind,
                    )
                )
        return tuple(chunks) or self._text_chunks(document, text)


def _window_lines(text: str, *, max_chars: int = 1_600) -> Iterable[tuple[str, int, int]]:
    lines = text.splitlines()
    start = 0
    while start < len(lines):
        end = start
        size = 0
        while end < len(lines):
            candidate = len(lines[end]) + 1
            if end > start and size + candidate > max_chars:
                break
            size += candidate
            end += 1
        content = "\n".join(lines[start:end]).strip()
        if content:
            yield content, start + 1, end
        start = end


def _chunk(
    document: Document,
    content: str,
    *,
    start_line: int | None = None,
    end_line: int | None = None,
    page: int | None = None,
    kind: str = "text",
) -> Chunk:
    locator = f"{document.id}:{page}:{start_line}:{end_line}:{kind}:{content}"
    return Chunk(
        id=hashlib.sha256(locator.encode()).hexdigest(),
        document_id=document.id,
        source=document.source,
        content=content,
        start_line=start_line,
        end_line=end_line,
        page=page,
        kind=kind,
    )
