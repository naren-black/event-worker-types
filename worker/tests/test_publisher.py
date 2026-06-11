"""Unit tests for src.publisher - topology declaration and confirmed publish."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pika
import pytest

from src.publisher import Publisher, declare_topology
from src.schema import TransferEvent


@pytest.fixture
def channel() -> MagicMock:
    return MagicMock()


@pytest.fixture
def event(example_event_dict) -> TransferEvent:
    return TransferEvent.model_validate(example_event_dict)


def test_declare_topology_declares_main_retry_and_dlq_queues(channel, settings):
    declare_topology(channel, settings)

    declared = {call.kwargs["queue"]: call.kwargs for call in channel.queue_declare.call_args_list}

    assert set(declared) == {settings.queue_name, settings.retry_queue_name, settings.dlq_name}
    assert all(kwargs["durable"] is True for kwargs in declared.values())

    retry_args = declared[settings.retry_queue_name]["arguments"]
    assert retry_args["x-dead-letter-exchange"] == ""
    assert retry_args["x-dead-letter-routing-key"] == settings.queue_name


def test_publish_event_sends_persistent_confirmed_message(channel, settings, event):
    publisher = Publisher(channel, settings)

    publisher.publish_event(event)

    channel.confirm_delivery.assert_called_once()
    _, kwargs = channel.basic_publish.call_args
    assert kwargs["routing_key"] == settings.queue_name
    assert kwargs["mandatory"] is True
    assert kwargs["properties"].delivery_mode == pika.DeliveryMode.Persistent.value
    assert kwargs["properties"].message_id == event.eventId
    assert kwargs["properties"].correlation_id == event.correlationId
    assert json.loads(kwargs["body"]) == json.loads(event.to_json_bytes())


def test_publish_retry_sets_per_message_ttl_on_retry_queue(channel, settings, event):
    publisher = Publisher(channel, settings)
    retried = event.with_retry(attempt=1, max_attempts=5, last_error="aws-s3:Timeout")

    publisher.publish_retry(retried, delay_ms=2000)

    _, kwargs = channel.basic_publish.call_args
    assert kwargs["routing_key"] == settings.retry_queue_name
    assert kwargs["properties"].expiration == "2000"
    body = json.loads(kwargs["body"])
    assert body["retry"] == {"attempt": 1, "maxAttempts": 5, "lastError": "aws-s3:Timeout"}


def test_publish_dlq_sets_reason_header(channel, settings, event):
    publisher = Publisher(channel, settings)

    publisher.publish_dlq(event, reason="max_retries_exceeded")

    _, kwargs = channel.basic_publish.call_args
    assert kwargs["routing_key"] == settings.dlq_name
    assert kwargs["properties"].headers == {"x-dlq-reason": "max_retries_exceeded"}
    assert json.loads(kwargs["body"])["eventId"] == event.eventId
