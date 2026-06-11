"""Runtime configuration loaded from environment variables.

A single frozen dataclass keeps every tunable in one place so the README,
docker-compose env blocks, and this module stay easy to cross-check.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _str(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _opt_str(name: str) -> str | None:
    return os.environ.get(name) or None


def _int(name: str, default: int) -> int:
    return int(os.environ.get(name, str(default)))


def _float(name: str, default: float) -> float:
    return float(os.environ.get(name, str(default)))


@dataclass(frozen=True)
class Settings:
    # Watcher
    watch_dir: str = "/watch"
    stable_check_interval_s: float = 1.0

    # Broker (RabbitMQ)
    rabbitmq_host: str = "localhost"
    rabbitmq_port: int = 5672
    rabbitmq_user: str = "guest"
    rabbitmq_pass: str = "guest"
    rabbitmq_vhost: str = "/"
    queue_name: str = "orders.inbound"
    retry_queue_name: str = "orders.inbound.retry"
    dlq_name: str = "orders.inbound.dlq"

    # AWS / S3 (or MinIO in dev)
    s3_endpoint_url: str | None = None
    s3_bucket: str = "wms-orders-processed"
    s3_region: str = "us-east-1"

    # GCP / GCS (or fake-gcs-server in dev)
    gcs_endpoint_url: str | None = None
    gcs_bucket: str = "wms-orders-processed"
    gcp_project: str = "demo-project"

    # Worker behaviour
    upload_max_concurrency: int = 2
    retry_max_attempts: int = 5
    retry_backoff_base: float = 2.0
    upload_timeout_s: float = 30.0

    # Idempotency store
    idempotency_db_path: str = "/data/idempotency.db"

    # Observability
    log_level: str = "INFO"
    service_name: str = "sftp-event-worker"
    health_port: int = 8080

    # Used to derive idempotency keys (HMAC) - see src/utils.py
    correlation_secret: str = "dev-secret-change-me"

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            watch_dir=_str("WATCH_DIR", cls.watch_dir),
            stable_check_interval_s=_float("STABLE_CHECK_INTERVAL_S", cls.stable_check_interval_s),
            rabbitmq_host=_str("RABBITMQ_HOST", cls.rabbitmq_host),
            rabbitmq_port=_int("RABBITMQ_PORT", cls.rabbitmq_port),
            rabbitmq_user=_str("RABBITMQ_USER", cls.rabbitmq_user),
            rabbitmq_pass=_str("RABBITMQ_PASS", cls.rabbitmq_pass),
            rabbitmq_vhost=_str("RABBITMQ_VHOST", cls.rabbitmq_vhost),
            queue_name=_str("QUEUE_NAME", cls.queue_name),
            retry_queue_name=_str("RETRY_QUEUE_NAME", cls.retry_queue_name),
            dlq_name=_str("DLQ_NAME", cls.dlq_name),
            s3_endpoint_url=_opt_str("S3_ENDPOINT_URL"),
            s3_bucket=_str("S3_BUCKET", cls.s3_bucket),
            s3_region=_str("S3_REGION", cls.s3_region),
            gcs_endpoint_url=_opt_str("GCS_ENDPOINT_URL"),
            gcs_bucket=_str("GCS_BUCKET", cls.gcs_bucket),
            gcp_project=_str("GOOGLE_CLOUD_PROJECT", cls.gcp_project),
            upload_max_concurrency=_int("UPLOAD_MAX_CONCURRENCY", cls.upload_max_concurrency),
            retry_max_attempts=_int("RETRY_MAX_ATTEMPTS", cls.retry_max_attempts),
            retry_backoff_base=_float("RETRY_BACKOFF_BASE", cls.retry_backoff_base),
            upload_timeout_s=_float("UPLOAD_TIMEOUT_S", cls.upload_timeout_s),
            idempotency_db_path=_str("IDEMPOTENCY_DB_PATH", cls.idempotency_db_path),
            log_level=_str("LOG_LEVEL", cls.log_level),
            service_name=_str("SERVICE_NAME", cls.service_name),
            health_port=_int("HEALTH_PORT", cls.health_port),
            correlation_secret=_str("CORRELATION_SECRET", cls.correlation_secret),
        )

    @property
    def rabbitmq_url(self) -> str:
        from urllib.parse import quote

        vhost = quote(self.rabbitmq_vhost, safe="")
        return (
            f"amqp://{self.rabbitmq_user}:{self.rabbitmq_pass}"
            f"@{self.rabbitmq_host}:{self.rabbitmq_port}/{vhost}"
        )
