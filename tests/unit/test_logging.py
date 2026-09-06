"""Tests for the structured JSON logging layer."""

from __future__ import annotations

import io
import json
import logging

import pytest

from forgeharness.observability.logging import JsonFormatter, configure_logging, get_logger

_STREAMS: dict[str, io.StringIO] = {}


@pytest.fixture()
def stream() -> io.StringIO:
    buffer = io.StringIO()
    _STREAMS["buffer"] = buffer
    configure_logging(level=logging.DEBUG, stream=buffer)
    yield buffer
    logger = logging.getLogger("forgeharness")
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
    _STREAMS.clear()


def _last_record() -> dict[str, object]:
    lines = [line for line in _STREAMS["buffer"].getvalue().splitlines() if line.strip()]
    assert lines, "expected at least one log line in the captured stream"
    return json.loads(lines[-1])


def test_log_line_is_valid_json_with_utc_timestamp(stream: io.StringIO) -> None:
    get_logger("api").info("http_request", extra={"method": "GET", "path": "/health"})

    payload = _last_record()
    assert payload["message"] == "http_request"
    assert payload["level"] == "info"
    assert payload["logger"] == "forgeharness.api"
    assert payload["method"] == "GET"
    assert payload["path"] == "/health"
    assert str(payload["ts"]).endswith("+00:00")


def test_exception_includes_traceback_field(stream: io.StringIO) -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        get_logger("worker").exception("job_failed")

    payload = _last_record()
    assert payload["message"] == "job_failed"
    assert "ValueError: boom" in str(payload["exception"])


def test_extra_fields_pass_through_with_single_message(stream: io.StringIO) -> None:
    get_logger("api").warning("msg", extra={"custom_field": 1})

    payload = _last_record()
    assert payload["message"] == "msg"
    assert payload["custom_field"] == 1
    assert list(payload.keys()).count("message") == 1


def test_get_logger_namespaces_unknown_names() -> None:
    assert get_logger("api").name == "forgeharness.api"
    assert get_logger("forgeharness.runtime").name == "forgeharness.runtime"


def test_configure_logging_is_idempotent() -> None:
    buffer: io.StringIO = io.StringIO()
    configure_logging(stream=buffer)
    configure_logging(stream=buffer)

    logger = logging.getLogger("forgeharness")
    assert len(logger.handlers) == 1
    assert isinstance(logger.handlers[0].formatter, JsonFormatter)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
