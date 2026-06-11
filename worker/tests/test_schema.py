"""Unit tests for src.schema - pydantic models mirroring
schemas/transfer-event.schema.json."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.schema import (
    SUPPORTED_SCHEMA_MAJOR,
    RetryInfo,
    TransferEvent,
    UnsupportedSchemaVersionError,
)

RETRY_EXAMPLE_PATH = Path(__file__).resolve().parents[2] / "schemas" / "example-event-retry.json"


def test_round_trips_example_event(example_event_dict):
    event = TransferEvent.model_validate(example_event_dict)

    assert event.schemaVersion == "1.0"
    assert event.schema_major == SUPPORTED_SCHEMA_MAJOR
    assert event.retry is None
    assert [d.provider for d in event.destinations] == ["aws-s3", "gcp-gcs"]

    again = TransferEvent.from_json_bytes(event.to_json_bytes())
    assert again == event


def test_round_trips_retry_example_event():
    data = json.loads(RETRY_EXAMPLE_PATH.read_text())

    event = TransferEvent.model_validate(data)

    assert event.retry == RetryInfo(attempt=2, maxAttempts=5, lastError="gcp-gcs:upload_timeout")


def test_assert_supported_accepts_current_major(example_event_dict):
    event = TransferEvent.model_validate(example_event_dict)

    event.assert_supported()  # must not raise


def test_assert_supported_rejects_future_major(example_event_dict):
    example_event_dict["schemaVersion"] = "2.0"
    event = TransferEvent.model_validate(example_event_dict)

    with pytest.raises(UnsupportedSchemaVersionError):
        event.assert_supported()


@pytest.mark.parametrize("bad_version", ["1", "v1.0", "1.0.0", ""])
def test_invalid_schema_version_rejected(example_event_dict, bad_version):
    example_event_dict["schemaVersion"] = bad_version

    with pytest.raises(ValidationError):
        TransferEvent.model_validate(example_event_dict)


def test_invalid_idempotency_key_rejected(example_event_dict):
    example_event_dict["idempotencyKey"] = "not-a-valid-key"

    with pytest.raises(ValidationError):
        TransferEvent.model_validate(example_event_dict)


def test_invalid_checksum_rejected(example_event_dict):
    example_event_dict["file"]["checksumSha256"] = "too-short"

    with pytest.raises(ValidationError):
        TransferEvent.model_validate(example_event_dict)


def test_invalid_uuid_rejected(example_event_dict):
    example_event_dict["eventId"] = "not-a-uuid"

    with pytest.raises(ValidationError):
        TransferEvent.model_validate(example_event_dict)


def test_unknown_destination_provider_rejected(example_event_dict):
    example_event_dict["destinations"][0]["provider"] = "azure-blob"

    with pytest.raises(ValidationError):
        TransferEvent.model_validate(example_event_dict)


def test_extra_fields_rejected(example_event_dict):
    example_event_dict["unexpectedField"] = "boom"

    with pytest.raises(ValidationError):
        TransferEvent.model_validate(example_event_dict)


def test_destinations_must_be_non_empty(example_event_dict):
    example_event_dict["destinations"] = []

    with pytest.raises(ValidationError):
        TransferEvent.model_validate(example_event_dict)


def test_with_retry_returns_new_event_without_mutating_original(example_event_dict):
    event = TransferEvent.model_validate(example_event_dict)

    retried = event.with_retry(attempt=1, max_attempts=5, last_error="aws-s3:Timeout")

    assert retried.retry == RetryInfo(attempt=1, maxAttempts=5, lastError="aws-s3:Timeout")
    assert event.retry is None
