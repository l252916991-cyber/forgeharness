"""Tests for source parsing and durable local application state."""

from __future__ import annotations

import hashlib
import io
from pathlib import Path

import pytest
from PIL import Image
from pydantic import ValidationError
from pypdf import PdfWriter

from forgeharness.knowledge.models import (
    ChatMessage,
    Chunk,
    ContentPart,
    ContentPartType,
    IngestJob,
    JobStatus,
)
from forgeharness.knowledge.parsers import DocumentParseError, LocalDocumentParser
from forgeharness.knowledge.storage import SQLiteApplicationStore, SQLiteSessionStore


def test_content_parts_and_chunk_locations_are_validated() -> None:
    text = ContentPart(type=ContentPartType.TEXT, text="hello")
    media = ContentPart(type=ContentPartType.FILE, media_id="a" * 64)
    assert text.text == "hello"
    assert media.media_id == "a" * 64
    with pytest.raises(ValidationError):
        ContentPart(type=ContentPartType.TEXT, media_id="a" * 64)
    with pytest.raises(ValidationError):
        ContentPart(type=ContentPartType.IMAGE, text="unsafe path")
    with pytest.raises(ValidationError):
        Chunk(
            id="a" * 64,
            document_id="b" * 64,
            source="bad.py",
            content="x",
            start_line=4,
            end_line=3,
        )


def test_parser_preserves_markdown_and_python_locations() -> None:
    parser = LocalDocumentParser()
    document, markdown = parser.parse(
        filename="guide.md",
        data=b"# Intro\nAgent loop\n## Tools\nSchema validation",
    )
    assert document.source == "guide.md"
    assert len(markdown) == 2
    assert markdown[0].start_line == 1
    assert markdown[1].start_line == 3
    assert all(chunk.kind == "markdown" for chunk in markdown)

    _, python = parser.parse(
        filename="agent.py",
        data=(
            b'"""module"""\nVALUE = 1\n\n'
            b"class Agent:\n    pass\n\n"
            b"async def run():\n    return True\n"
        ),
    )
    assert [chunk.kind for chunk in python] == [
        "python_module",
        "python_class",
        "python_function",
    ]
    assert python[-1].start_line == 7


def test_parser_handles_json_text_image_pdf_and_syntax_fallback(tmp_path: Path) -> None:
    parser = LocalDocumentParser()
    document, chunks = parser.parse(filename="data.json", data=b'{"b": 1, "a": 2}')
    assert document.mime_type == "application/json"
    assert chunks[0].content.index('"a"') < chunks[0].content.index('"b"')

    _, text = parser.parse(filename="notes.txt", data=b"one\ntwo")
    assert text[0].end_line == 2

    image_bytes = io.BytesIO()
    Image.new("RGB", (2, 2), "white").save(image_bytes, format="PNG")
    image_document, image = parser.parse(filename="screen.png", data=image_bytes.getvalue())
    assert image == ()
    derived = parser.image_chunk(image_document, "visible error text")
    assert derived.kind == "image_description"

    _, fallback = parser.parse(filename="broken.py", data=b"def bad(:\n  pass")
    assert fallback[0].kind == "text"

    pdf_path = tmp_path / "blank.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    with pdf_path.open("wb") as handle:
        writer.write(handle)
    _, pdf_chunks = parser.parse(filename="blank.pdf", data=pdf_path.read_bytes())
    assert pdf_chunks == ()
    with pytest.raises(DocumentParseError, match="unable to parse PDF"):
        parser.parse(filename="broken.pdf", data=b"not a pdf")


@pytest.mark.parametrize(
    ("filename", "data", "message"),
    [
        ("../secret.txt", b"x", "filename"),
        ("empty.txt", b"", "empty"),
        ("bad.exe", b"x", "unsupported"),
        ("bad.txt", b"\xff", "UTF-8"),
        ("bad.json", b"{", "invalid JSON"),
        ("bad.png", b"not-an-image", "unable to parse image"),
    ],
)
def test_parser_rejects_unsafe_or_invalid_uploads(filename: str, data: bytes, message: str) -> None:
    with pytest.raises(DocumentParseError, match=message):
        LocalDocumentParser().parse(filename=filename, data=data)


def test_parser_rejects_oversized_and_empty_image_description() -> None:
    parser = LocalDocumentParser()
    with pytest.raises(DocumentParseError, match="10 MiB"):
        parser.parse(filename="large.txt", data=b"x" * (10 * 1024 * 1024 + 1))
    image_bytes = io.BytesIO()
    Image.new("RGB", (2, 2), "white").save(image_bytes, format="JPEG")
    document, _ = parser.parse(filename="image.jpg", data=image_bytes.getvalue())
    with pytest.raises(DocumentParseError, match="empty description"):
        parser.image_chunk(document, "  ")


async def test_sqlite_application_store_documents_sessions_and_jobs(tmp_path: Path) -> None:
    parser = LocalDocumentParser()
    document, _ = parser.parse(filename="guide.md", data=b"# Agent\nTools")
    store = SQLiteApplicationStore(tmp_path / "app.sqlite3", tmp_path / "media")
    assert store.put(document, b"# Agent\nTools") is True
    assert store.put(document, b"# Agent\nTools") is False
    loaded = store.get(document.id)
    assert loaded is not None and loaded[0] == document
    assert loaded[1] == b"# Agent\nTools"
    assert store.get("0" * 64) is None

    sessions = SQLiteSessionStore(store)
    session = await sessions.create()
    assert await sessions.get(session.id) == session
    assert await sessions.get("missing") is None
    message = ChatMessage(
        session_id=session.id,
        role="user",
        parts=(ContentPart(type=ContentPartType.TEXT, text="hello"),),
    )
    await sessions.add_message(message)
    assert await sessions.messages(session.id) == (message,)
    with pytest.raises(ValueError, match="unknown session"):
        await sessions.add_message(message.model_copy(update={"session_id": "missing"}))
    with pytest.raises(ValueError, match="message limit"):
        await sessions.messages(session.id, limit=0)

    job = IngestJob(document_id=document.id)
    assert store.create_job(job, idempotency_key="same") == job
    assert store.create_job(IngestJob(document_id=document.id), idempotency_key="same") == job
    assert store.create_job(IngestJob(document_id=document.id), idempotency_key="new-key") == job
    other_id = hashlib.sha256(b"other").hexdigest()
    with pytest.raises(ValueError, match="different content"):
        store.create_job(IngestJob(document_id=other_id), idempotency_key="same")
    assert store.get_job("missing") is None
    finished = store.update_job(job.id, status=JobStatus.SUCCEEDED)
    assert finished.status == JobStatus.SUCCEEDED
    with pytest.raises(ValueError, match="unknown ingestion job"):
        store.update_job("missing", status=JobStatus.FAILED)
    with pytest.raises(ValueError, match="1-200"):
        store.create_job(IngestJob(document_id=other_id), idempotency_key="")
