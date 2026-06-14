"""Unit tests for src.watcher.

Uses synthetic watchdog event objects rather than a real Observer so this
runs identically on Linux (inotify) and macOS (FSEvents) dev machines.
"""

from __future__ import annotations

import hashlib
import queue
from types import SimpleNamespace
from unittest.mock import MagicMock

import pika
import pytest
from watchdog.events import FileClosedEvent, FileMovedEvent

from src import watcher as watcher_mod
from src.schema import TransferEvent
from src.utils import compute_idempotency_key
from src.watcher import (
    DetectedFileHandler,
    _publish_with_reconnect,
    _service_idle_connection,
    build_event,
    guess_content_type,
)


def test_build_event_for_csv(settings, write_file):
    content = b"order_id,sku,qty\n1,ABC,2\n"
    path = write_file("orders/ORD-1.csv", content)

    event = build_event(str(path), settings)

    assert event.schemaVersion == "1.0"
    assert event.eventType == "file.transfer.requested"
    assert event.source.path == str(path)
    assert event.file.name == "ORD-1.csv"
    assert event.file.sizeBytes == len(content)
    assert event.file.checksumSha256 == hashlib.sha256(content).hexdigest()
    assert event.file.contentType == "text/csv"

    expected_key = compute_idempotency_key(
        secret=settings.correlation_secret,
        source_path=str(path),
        size_bytes=len(content),
        checksum_sha256=hashlib.sha256(content).hexdigest(),
    )
    assert event.idempotencyKey == expected_key

    by_provider = {d.provider: d for d in event.destinations}
    assert by_provider["aws-s3"].bucket == settings.s3_bucket
    assert by_provider["aws-s3"].key == "orders/ORD-1.csv"
    assert by_provider["aws-s3"].region == settings.s3_region
    assert by_provider["gcp-gcs"].bucket == settings.gcs_bucket
    assert by_provider["gcp-gcs"].key == "orders/ORD-1.csv"
    assert by_provider["gcp-gcs"].region is None


def test_build_event_idempotency_key_differs_by_path_same_content(settings, write_file):
    content = b"same bytes"
    path_a = write_file("a.csv", content)
    path_b = write_file("b.csv", content)

    event_a = build_event(str(path_a), settings)
    event_b = build_event(str(path_b), settings)

    assert event_a.idempotencyKey != event_b.idempotencyKey
    # stable across repeated detections of the same file
    assert build_event(str(path_a), settings).idempotencyKey == event_a.idempotencyKey


def test_guess_content_type():
    assert guess_content_type("orders/ORD-1.csv") == "text/csv"
    assert guess_content_type("data.json") == "application/json"
    assert guess_content_type("notes.txt") == "text/plain"
    assert guess_content_type("archive.bin") == "application/octet-stream"


def test_detected_file_handler_on_closed_enqueues_path():
    file_queue: queue.Queue[str] = queue.Queue()
    handler = DetectedFileHandler(file_queue)

    handler.on_closed(FileClosedEvent("/watch/orders/ORD-1.csv"))

    assert file_queue.get_nowait() == "/watch/orders/ORD-1.csv"


def test_detected_file_handler_on_moved_enqueues_dest_path():
    file_queue: queue.Queue[str] = queue.Queue()
    handler = DetectedFileHandler(file_queue)

    handler.on_moved(FileMovedEvent("/watch/.tmp123", "/watch/orders/ORD-1.csv"))

    assert file_queue.get_nowait() == "/watch/orders/ORD-1.csv"


def test_detected_file_handler_ignores_directory_events():
    file_queue: queue.Queue[str] = queue.Queue()
    handler = DetectedFileHandler(file_queue)

    handler.on_closed(SimpleNamespace(is_directory=True, src_path="/watch/orders"))
    handler.on_moved(SimpleNamespace(is_directory=True, dest_path="/watch/orders"))

    assert file_queue.empty()


@pytest.fixture
def connection() -> MagicMock:
    return MagicMock()


@pytest.fixture
def publisher() -> MagicMock:
    return MagicMock()


@pytest.fixture
def event(example_event_dict) -> TransferEvent:
    return TransferEvent.model_validate(example_event_dict)


def test_publish_with_reconnect_happy_path(connection, publisher, event, settings):
    result_connection, result_publisher = _publish_with_reconnect(connection, publisher, event, settings)

    publisher.publish_event.assert_called_once_with(event)
    assert result_connection is connection
    assert result_publisher is publisher


def test_publish_with_reconnect_reconnects_and_retries_on_amqp_error(
    monkeypatch, connection, publisher, event, settings
):
    publisher.publish_event.side_effect = pika.exceptions.StreamLostError("boom")

    new_connection = MagicMock()
    new_publisher = MagicMock()
    monkeypatch.setattr(
        watcher_mod.publisher_mod,
        "connect_and_setup",
        MagicMock(return_value=(new_connection, MagicMock(), new_publisher)),
    )

    result_connection, result_publisher = _publish_with_reconnect(connection, publisher, event, settings)

    connection.close.assert_called_once()
    publisher.publish_event.assert_called_once_with(event)
    new_publisher.publish_event.assert_called_once_with(event)
    assert result_connection is new_connection
    assert result_publisher is new_publisher


def test_publish_with_reconnect_retry_failure_does_not_raise(
    monkeypatch, connection, publisher, event, settings
):
    publisher.publish_event.side_effect = pika.exceptions.StreamLostError("boom")

    new_connection = MagicMock()
    new_publisher = MagicMock()
    new_publisher.publish_event.side_effect = RuntimeError("still broken")
    monkeypatch.setattr(
        watcher_mod.publisher_mod,
        "connect_and_setup",
        MagicMock(return_value=(new_connection, MagicMock(), new_publisher)),
    )

    result_connection, result_publisher = _publish_with_reconnect(connection, publisher, event, settings)

    assert result_connection is new_connection
    assert result_publisher is new_publisher


def test_service_idle_connection_drives_io_loop(connection, publisher, settings):
    result_connection, result_publisher = _service_idle_connection(connection, publisher, settings)

    connection.process_data_events.assert_called_once_with(time_limit=0)
    assert result_connection is connection
    assert result_publisher is publisher


def test_service_idle_connection_reconnects_on_amqp_error(monkeypatch, connection, publisher, settings):
    connection.process_data_events.side_effect = pika.exceptions.StreamLostError("boom")

    new_connection = MagicMock()
    new_publisher = MagicMock()
    monkeypatch.setattr(
        watcher_mod.publisher_mod,
        "connect_and_setup",
        MagicMock(return_value=(new_connection, MagicMock(), new_publisher)),
    )

    result_connection, result_publisher = _service_idle_connection(connection, publisher, settings)

    connection.close.assert_called_once()
    assert result_connection is new_connection
    assert result_publisher is new_publisher
