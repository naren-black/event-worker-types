"""Shared pytest fixtures for the worker test suite."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from src.config import Settings

SCHEMAS_DIR = Path(__file__).resolve().parents[2] / "schemas"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Settings tuned for fast, hermetic unit tests."""
    return Settings(
        watch_dir=str(tmp_path),
        rabbitmq_host="rabbitmq.invalid",
        s3_bucket="test-bucket",
        s3_region="us-east-1",
        gcs_bucket="test-bucket",
        retry_max_attempts=3,
        retry_backoff_base=2.0,
        upload_timeout_s=1.0,
        upload_max_concurrency=2,
        idempotency_db_path=":memory:",
        correlation_secret="test-secret",
    )


@pytest.fixture
def write_file(tmp_path: Path) -> Callable[[str, bytes], Path]:
    """Write ``content`` to ``tmp_path/name`` (creating parent dirs) and return its path."""

    def _write(name: str, content: bytes) -> Path:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    return _write


@pytest.fixture
def example_event_dict() -> dict:
    """A fresh deep copy of schemas/example-event.json for each test."""
    return json.loads((SCHEMAS_DIR / "example-event.json").read_text())
