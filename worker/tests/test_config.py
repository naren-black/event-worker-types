"""Unit tests for src.config.Settings."""

from __future__ import annotations

from src.config import Settings


def test_defaults_are_sane():
    settings = Settings()

    assert settings.queue_name == "orders.inbound"
    assert settings.retry_queue_name == "orders.inbound.retry"
    assert settings.dlq_name == "orders.inbound.dlq"
    assert settings.retry_max_attempts >= 1


def test_from_env_overrides_defaults(monkeypatch):
    monkeypatch.setenv("RABBITMQ_HOST", "broker.example")
    monkeypatch.setenv("RETRY_MAX_ATTEMPTS", "7")
    monkeypatch.setenv("S3_ENDPOINT_URL", "http://minio:9000")

    settings = Settings.from_env()

    assert settings.rabbitmq_host == "broker.example"
    assert settings.retry_max_attempts == 7
    assert settings.s3_endpoint_url == "http://minio:9000"


def test_from_env_leaves_optional_endpoints_unset_by_default(monkeypatch):
    monkeypatch.delenv("S3_ENDPOINT_URL", raising=False)
    monkeypatch.delenv("GCS_ENDPOINT_URL", raising=False)

    settings = Settings.from_env()

    assert settings.s3_endpoint_url is None
    assert settings.gcs_endpoint_url is None


def test_rabbitmq_url_encodes_vhost(monkeypatch):
    monkeypatch.setenv("RABBITMQ_USER", "guest")
    monkeypatch.setenv("RABBITMQ_PASS", "guest")
    monkeypatch.setenv("RABBITMQ_HOST", "rabbitmq")
    monkeypatch.setenv("RABBITMQ_PORT", "5672")
    monkeypatch.setenv("RABBITMQ_VHOST", "/")

    settings = Settings.from_env()

    assert settings.rabbitmq_url == "amqp://guest:guest@rabbitmq:5672/%2F"
