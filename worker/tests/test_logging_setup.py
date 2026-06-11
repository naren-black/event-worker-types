"""Unit tests for src.logging_setup.JsonFormatter."""

from __future__ import annotations

import json
import logging

from src.logging_setup import JsonFormatter


def _make_record(**extra: object) -> logging.LogRecord:
    record = logging.LogRecord(
        name="test.logger",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="hello %s",
        args=("world",),
        exc_info=None,
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_format_includes_core_fields():
    formatter = JsonFormatter("sftp-event-worker")

    payload = json.loads(formatter.format(_make_record()))

    assert payload["service"] == "sftp-event-worker"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "test.logger"
    assert payload["message"] == "hello world"
    assert "timestamp" in payload


def test_format_includes_extra_correlation_fields():
    formatter = JsonFormatter("sftp-event-worker")

    payload = json.loads(formatter.format(_make_record(correlationId="corr-1", eventId="evt-1", attempt=2)))

    assert payload["correlationId"] == "corr-1"
    assert payload["eventId"] == "evt-1"
    assert payload["attempt"] == 2


def test_format_omits_unset_extra_fields():
    formatter = JsonFormatter("sftp-event-worker")

    payload = json.loads(formatter.format(_make_record()))

    assert "correlationId" not in payload
    assert "idempotencyKey" not in payload


def test_format_includes_exception_info():
    formatter = JsonFormatter("sftp-event-worker")
    try:
        raise ValueError("boom")
    except ValueError:
        record = _make_record()
        record.exc_info = __import__("sys").exc_info()

    payload = json.loads(formatter.format(record))

    assert "ValueError: boom" in payload["exc_info"]
