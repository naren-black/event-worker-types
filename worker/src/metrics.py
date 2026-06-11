"""Prometheus metrics for the transfer worker.

Exposed via ``src.health`` on ``GET /metrics``. A dedicated registry (rather
than the global default) keeps this importable from tests without leaking
state across test runs.
"""

from __future__ import annotations

from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Histogram, generate_latest

REGISTRY = CollectorRegistry()

EVENTS_PUBLISHED_TOTAL = Counter(
    "events_published_total",
    "Transfer events published by the watcher",
    registry=REGISTRY,
)

EVENTS_CONSUMED_TOTAL = Counter(
    "events_consumed_total",
    "Transfer events processed by the consumer, by terminal result",
    ["result"],  # success | retry | dlq | duplicate
    registry=REGISTRY,
)

UPLOAD_DURATION_SECONDS = Histogram(
    "upload_duration_seconds",
    "Upload duration per destination provider",
    ["provider"],
    registry=REGISTRY,
)

UPLOADS_TOTAL = Counter(
    "uploads_total",
    "Upload attempts per destination provider, by result",
    ["provider", "result"],  # result: success | error
    registry=REGISTRY,
)

DLQ_MESSAGES_TOTAL = Counter(
    "dlq_messages_total",
    "Messages routed to the dead-letter queue",
    registry=REGISTRY,
)

IDEMPOTENCY_HITS_TOTAL = Counter(
    "idempotency_hits_total",
    "Messages skipped because they were already fully processed",
    registry=REGISTRY,
)


def render() -> tuple[bytes, str]:
    """Return (body, content_type) for the /metrics endpoint."""
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST
