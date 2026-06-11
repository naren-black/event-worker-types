"""RabbitMQ consumer.

Drives the validate -> dedupe -> upload -> ack/retry/DLQ state machine
described in docs/event-contract.md.

Not thread-safe - must run on the same thread as its channel/connection.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading

import pika
from pika.adapters.blocking_connection import BlockingChannel

from . import metrics
from .config import Settings
from .idempotency import IdempotencyStore
from .publisher import Publisher
from .schema import TransferEvent, UnsupportedSchemaVersionError
from .uploader import upload_all

logger = logging.getLogger(__name__)


def backoff_delay_ms(settings: Settings, attempt: int) -> int:
    """Exponential backoff: attempt 1 -> base^1 seconds, attempt 2 -> base^2, ..."""
    return int((settings.retry_backoff_base**attempt) * 1000)


class Consumer:
    def __init__(
        self,
        channel: BlockingChannel,
        publisher: Publisher,
        settings: Settings,
        idempotency: IdempotencyStore,
    ) -> None:
        self._channel = channel
        self._publisher = publisher
        self._settings = settings
        self._idempotency = idempotency
        self._channel.basic_qos(prefetch_count=max(1, settings.upload_max_concurrency))

    def run(self, stop_event: threading.Event) -> None:
        """Consume until ``stop_event`` is set."""
        for method, properties, body in self._channel.consume(
            self._settings.queue_name, inactivity_timeout=1
        ):
            if stop_event.is_set():
                break
            if method is None:
                continue  # inactivity timeout tick
            try:
                self._handle(method, properties, body)
            except Exception:
                logger.exception(
                    "unhandled error processing message; leaving unacked for redelivery"
                )
        self._channel.cancel()

    def _handle(self, method: pika.spec.Basic.Deliver, properties: pika.BasicProperties, body: bytes) -> None:
        try:
            event = TransferEvent.from_json_bytes(body)
        except Exception as exc:
            logger.error("invalid event payload, routing to DLQ: %s", exc)
            self._dead_letter_raw(body, properties, reason="invalid_payload")
            self._channel.basic_ack(method.delivery_tag)
            metrics.EVENTS_CONSUMED_TOTAL.labels(result="dlq").inc()
            metrics.DLQ_MESSAGES_TOTAL.inc()
            return

        log_extra = {
            "correlationId": event.correlationId,
            "eventId": event.eventId,
            "idempotencyKey": event.idempotencyKey,
        }

        try:
            event.assert_supported()
        except UnsupportedSchemaVersionError as exc:
            logger.error("unsupported schema version, routing to DLQ: %s", exc, extra=log_extra)
            self._publisher.publish_dlq(event, reason="unsupported_schema_version")
            self._channel.basic_ack(method.delivery_tag)
            metrics.EVENTS_CONSUMED_TOTAL.labels(result="dlq").inc()
            metrics.DLQ_MESSAGES_TOTAL.inc()
            return

        if self._idempotency.is_done(event.idempotencyKey):
            logger.info("duplicate event, skipping", extra=log_extra)
            self._channel.basic_ack(method.delivery_tag)
            metrics.EVENTS_CONSUMED_TOTAL.labels(result="duplicate").inc()
            metrics.IDEMPOTENCY_HITS_TOTAL.inc()
            return

        if not os.path.isfile(event.source.path):
            logger.error("source file missing, routing to DLQ", extra=log_extra)
            self._publisher.publish_dlq(event, reason="source_file_missing")
            self._channel.basic_ack(method.delivery_tag)
            metrics.EVENTS_CONSUMED_TOTAL.labels(result="dlq").inc()
            metrics.DLQ_MESSAGES_TOTAL.inc()
            return

        results = asyncio.run(upload_all(self._settings, event, event.source.path))
        failures = [r for r in results if not r.ok]

        if not failures:
            self._idempotency.mark_done(event.idempotencyKey, event.correlationId)
            self._channel.basic_ack(method.delivery_tag)
            logger.info("event processed successfully", extra=log_extra)
            metrics.EVENTS_CONSUMED_TOTAL.labels(result="success").inc()
            return

        last_error = ",".join(r.error for r in failures if r.error)
        attempt = (event.retry.attempt if event.retry else 0) + 1

        if attempt > self._settings.retry_max_attempts:
            logger.error(
                "max retries exceeded, routing to DLQ: %s", last_error, extra=log_extra
            )
            failed_event = event.with_retry(
                attempt=attempt - 1,
                max_attempts=self._settings.retry_max_attempts,
                last_error=last_error,
            )
            self._publisher.publish_dlq(failed_event, reason="max_retries_exceeded")
            self._channel.basic_ack(method.delivery_tag)
            metrics.EVENTS_CONSUMED_TOTAL.labels(result="dlq").inc()
            metrics.DLQ_MESSAGES_TOTAL.inc()
            return

        retry_event = event.with_retry(
            attempt=attempt, max_attempts=self._settings.retry_max_attempts, last_error=last_error
        )
        delay_ms = backoff_delay_ms(self._settings, attempt)
        self._publisher.publish_retry(retry_event, delay_ms)
        self._channel.basic_ack(method.delivery_tag)
        logger.warning(
            "upload failed, scheduled retry %s/%s in %sms: %s",
            attempt,
            self._settings.retry_max_attempts,
            delay_ms,
            last_error,
            extra={**log_extra, "attempt": attempt},
        )
        metrics.EVENTS_CONSUMED_TOTAL.labels(result="retry").inc()

    def _dead_letter_raw(self, body: bytes, properties: pika.BasicProperties, reason: str) -> None:
        props = pika.BasicProperties(
            content_type=properties.content_type or "application/octet-stream",
            delivery_mode=pika.DeliveryMode.Persistent,
            headers={"x-dlq-reason": reason},
        )
        self._channel.basic_publish(
            exchange="",
            routing_key=self._settings.dlq_name,
            body=body,
            properties=props,
            mandatory=True,
        )
