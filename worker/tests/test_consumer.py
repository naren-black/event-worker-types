"""Unit tests for src.consumer - validate/dedupe/upload/retry/DLQ state machine."""

from __future__ import annotations

import hashlib
import json
from unittest.mock import MagicMock

import pika
import pika.spec
import pytest

from src import consumer as consumer_module
from src.consumer import Consumer, backoff_delay_ms
from src.idempotency import IdempotencyStore
from src.publisher import Publisher
from src.schema import TransferEvent
from src.uploader import UploadResult


def _method(delivery_tag: int) -> pika.spec.Basic.Deliver:
    return pika.spec.Basic.Deliver(delivery_tag=delivery_tag)


def _properties(content_type: str = "application/json") -> pika.BasicProperties:
    return pika.BasicProperties(content_type=content_type)


@pytest.fixture
def channel() -> MagicMock:
    return MagicMock()


@pytest.fixture
def publisher(channel, settings) -> Publisher:
    return Publisher(channel, settings)


@pytest.fixture
def store():
    s = IdempotencyStore(":memory:")
    yield s
    s.close()


@pytest.fixture
def consumer(channel, publisher, settings, store) -> Consumer:
    return Consumer(channel, publisher, settings, store)


@pytest.fixture
def event_with_existing_file(example_event_dict, write_file):
    """example_event_dict, but pointing at a real file on disk with matching
    size/checksum so the "source file missing" branch isn't hit."""
    content = b"order_id,sku,qty\n1,ABC,2\n"
    path = write_file("orders/ORD-1.csv", content)
    example_event_dict["source"]["path"] = str(path)
    example_event_dict["file"]["sizeBytes"] = len(content)
    example_event_dict["file"]["checksumSha256"] = hashlib.sha256(content).hexdigest()
    return example_event_dict


def test_happy_path_acks_and_marks_idempotent(
    monkeypatch, consumer, channel, store, event_with_existing_file
):
    event = TransferEvent.model_validate(event_with_existing_file)

    async def fake_upload_all(_settings, _event, _source_path):
        return [UploadResult(provider="aws-s3", ok=True), UploadResult(provider="gcp-gcs", ok=True)]

    monkeypatch.setattr(consumer_module, "upload_all", fake_upload_all)

    consumer._handle(_method(1), _properties(), event.to_json_bytes())

    channel.basic_ack.assert_called_once_with(1)
    assert store.is_done(event.idempotencyKey) is True
    channel.basic_publish.assert_not_called()


def test_duplicate_event_is_acked_without_uploading(
    monkeypatch, consumer, channel, store, event_with_existing_file
):
    event = TransferEvent.model_validate(event_with_existing_file)
    store.mark_done(event.idempotencyKey, event.correlationId)

    async def must_not_be_called(*_args, **_kwargs):
        raise AssertionError("upload_all should not be called for a duplicate event")

    monkeypatch.setattr(consumer_module, "upload_all", must_not_be_called)

    consumer._handle(_method(2), _properties(), event.to_json_bytes())

    channel.basic_ack.assert_called_once_with(2)
    channel.basic_publish.assert_not_called()


def test_invalid_payload_routed_to_dlq(consumer, channel, settings):
    consumer._handle(_method(3), _properties(), b"not json")

    channel.basic_ack.assert_called_once_with(3)
    _, kwargs = channel.basic_publish.call_args
    assert kwargs["routing_key"] == settings.dlq_name
    assert kwargs["properties"].headers == {"x-dlq-reason": "invalid_payload"}
    assert kwargs["body"] == b"not json"


def test_unsupported_schema_version_routed_to_dlq(consumer, channel, settings, event_with_existing_file):
    event_with_existing_file["schemaVersion"] = "2.0"
    event = TransferEvent.model_validate(event_with_existing_file)

    consumer._handle(_method(4), _properties(), event.to_json_bytes())

    channel.basic_ack.assert_called_once_with(4)
    _, kwargs = channel.basic_publish.call_args
    assert kwargs["routing_key"] == settings.dlq_name
    assert kwargs["properties"].headers == {"x-dlq-reason": "unsupported_schema_version"}
    assert json.loads(kwargs["body"])["eventId"] == event.eventId


def test_missing_source_file_routed_to_dlq(consumer, channel, settings, example_event_dict, tmp_path):
    example_event_dict["source"]["path"] = str(tmp_path / "missing.csv")
    event = TransferEvent.model_validate(example_event_dict)

    consumer._handle(_method(5), _properties(), event.to_json_bytes())

    channel.basic_ack.assert_called_once_with(5)
    _, kwargs = channel.basic_publish.call_args
    assert kwargs["routing_key"] == settings.dlq_name
    assert kwargs["properties"].headers == {"x-dlq-reason": "source_file_missing"}


def test_transient_failure_schedules_retry_with_backoff(
    monkeypatch, consumer, channel, settings, event_with_existing_file
):
    event = TransferEvent.model_validate(event_with_existing_file)

    async def fake_upload_all(_settings, _event, _source_path):
        return [
            UploadResult(provider="aws-s3", ok=True),
            UploadResult(provider="gcp-gcs", ok=False, error="gcp-gcs:upload_timeout"),
        ]

    monkeypatch.setattr(consumer_module, "upload_all", fake_upload_all)

    consumer._handle(_method(6), _properties(), event.to_json_bytes())

    channel.basic_ack.assert_called_once_with(6)
    _, kwargs = channel.basic_publish.call_args
    assert kwargs["routing_key"] == settings.retry_queue_name
    assert kwargs["properties"].expiration == str(backoff_delay_ms(settings, 1))
    body = json.loads(kwargs["body"])
    assert body["retry"] == {
        "attempt": 1,
        "maxAttempts": settings.retry_max_attempts,
        "lastError": "gcp-gcs:upload_timeout",
    }


def test_max_retries_exceeded_routes_to_dlq(
    monkeypatch, consumer, channel, settings, event_with_existing_file
):
    event_with_existing_file["retry"] = {
        "attempt": settings.retry_max_attempts,
        "maxAttempts": settings.retry_max_attempts,
        "lastError": "gcp-gcs:upload_timeout",
    }
    event = TransferEvent.model_validate(event_with_existing_file)

    async def fake_upload_all(_settings, _event, _source_path):
        return [
            UploadResult(provider="aws-s3", ok=True),
            UploadResult(provider="gcp-gcs", ok=False, error="gcp-gcs:upload_timeout"),
        ]

    monkeypatch.setattr(consumer_module, "upload_all", fake_upload_all)

    consumer._handle(_method(7), _properties(), event.to_json_bytes())

    channel.basic_ack.assert_called_once_with(7)
    _, kwargs = channel.basic_publish.call_args
    assert kwargs["routing_key"] == settings.dlq_name
    assert kwargs["properties"].headers == {"x-dlq-reason": "max_retries_exceeded"}
    body = json.loads(kwargs["body"])
    assert body["retry"]["attempt"] == settings.retry_max_attempts


def test_backoff_delay_grows_exponentially(settings):
    delays = [backoff_delay_ms(settings, attempt) for attempt in (1, 2, 3, 4)]

    assert delays == [2000, 4000, 8000, 16000]
    assert delays == sorted(delays)
