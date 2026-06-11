"""Pydantic models mirroring schemas/transfer-event.schema.json.

Keep this in sync with the JSON Schema - ``tests/test_schema.py`` round-trips
``schemas/example-event.json`` through these models to catch drift.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

SUPPORTED_SCHEMA_MAJOR = 1

_VERSION_RE = re.compile(r"^(\d+)\.(\d+)$")
_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
_IDEMPOTENCY_RE = re.compile(r"^sha256:[a-f0-9]{64}$")


class UnsupportedSchemaVersionError(ValueError):
    """Raised when an event's schemaVersion major component isn't supported."""


class Source(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Literal["sftp"]
    path: str


class FileMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    sizeBytes: int = Field(ge=0)
    checksumSha256: str
    contentType: str | None = None

    @field_validator("checksumSha256")
    @classmethod
    def _valid_sha256(cls, v: str) -> str:
        if not _SHA256_RE.match(v):
            raise ValueError("checksumSha256 must be 64 lowercase hex characters")
        return v


class Destination(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Literal["aws-s3", "gcp-gcs"]
    bucket: str
    key: str
    region: str | None = None


class RetryInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    attempt: int = Field(ge=1)
    maxAttempts: int = Field(ge=1)
    lastError: str | None = None


class TransferEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schemaVersion: str
    eventType: Literal["file.transfer.requested"]
    eventId: str
    correlationId: str
    idempotencyKey: str
    occurredAt: str
    source: Source
    file: FileMeta
    destinations: list[Destination] = Field(min_length=1)
    retry: RetryInfo | None = None

    @field_validator("schemaVersion")
    @classmethod
    def _valid_version(cls, v: str) -> str:
        if not _VERSION_RE.match(v):
            raise ValueError("schemaVersion must look like MAJOR.MINOR")
        return v

    @field_validator("eventId", "correlationId")
    @classmethod
    def _valid_uuid(cls, v: str) -> str:
        try:
            uuid.UUID(v)
        except ValueError as exc:
            raise ValueError("must be a valid UUID") from exc
        return v

    @field_validator("idempotencyKey")
    @classmethod
    def _valid_idempotency_key(cls, v: str) -> str:
        if not _IDEMPOTENCY_RE.match(v):
            raise ValueError("idempotencyKey must look like sha256:<64 hex chars>")
        return v

    @field_validator("occurredAt")
    @classmethod
    def _valid_occurred_at(cls, v: str) -> str:
        try:
            datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("occurredAt must be an RFC3339 timestamp") from exc
        return v

    @property
    def schema_major(self) -> int:
        match = _VERSION_RE.match(self.schemaVersion)
        assert match is not None  # guaranteed by _valid_version
        return int(match.group(1))

    def assert_supported(self) -> None:
        if self.schema_major != SUPPORTED_SCHEMA_MAJOR:
            raise UnsupportedSchemaVersionError(
                f"schemaVersion {self.schemaVersion} is not supported "
                f"(expected major version {SUPPORTED_SCHEMA_MAJOR})"
            )

    def with_retry(self, *, attempt: int, max_attempts: int, last_error: str) -> TransferEvent:
        return self.model_copy(
            update={"retry": RetryInfo(attempt=attempt, maxAttempts=max_attempts, lastError=last_error)}
        )

    def to_json_bytes(self) -> bytes:
        return self.model_dump_json().encode("utf-8")

    @classmethod
    def from_json_bytes(cls, data: bytes) -> TransferEvent:
        return cls.model_validate_json(data)
