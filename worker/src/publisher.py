"""RabbitMQ connection helpers and a thin, confirmed publisher.

Topology (all via the default exchange - routing key == queue name):

    orders.inbound          main queue, consumed by src.consumer
    orders.inbound.retry    holding queue; messages carry a per-message TTL
                             (the ``expiration`` property) and dead-letter
                             back onto orders.inbound when it expires
    orders.inbound.dlq      terminal failures, inspected/replayed manually

See docs/event-contract.md for the retry/backoff/DLQ rules this implements.
"""

from __future__ import annotations

import logging

import pika
from pika.adapters.blocking_connection import BlockingChannel

from .config import Settings
from .schema import TransferEvent

logger = logging.getLogger(__name__)


def connect(settings: Settings) -> pika.BlockingConnection:
    credentials = pika.PlainCredentials(settings.rabbitmq_user, settings.rabbitmq_pass)
    parameters = pika.ConnectionParameters(
        host=settings.rabbitmq_host,
        port=settings.rabbitmq_port,
        virtual_host=settings.rabbitmq_vhost,
        credentials=credentials,
        heartbeat=30,
        blocked_connection_timeout=30,
    )
    return pika.BlockingConnection(parameters)


def declare_topology(channel: BlockingChannel, settings: Settings) -> None:
    """Idempotently declare the main, retry and dead-letter queues.

    Safe to call from both the watcher and the consumer side - RabbitMQ
    no-ops a ``queue_declare`` with identical arguments.
    """
    channel.queue_declare(queue=settings.queue_name, durable=True)
    channel.queue_declare(
        queue=settings.retry_queue_name,
        durable=True,
        arguments={
            "x-dead-letter-exchange": "",
            "x-dead-letter-routing-key": settings.queue_name,
        },
    )
    channel.queue_declare(queue=settings.dlq_name, durable=True)


class Publisher:
    """Publish-confirmed wrapper around a single BlockingChannel.

    Not thread-safe - callers must only use one Publisher (and its
    underlying channel/connection) from a single thread.
    """

    def __init__(self, channel: BlockingChannel, settings: Settings) -> None:
        self._channel = channel
        self._settings = settings
        self._channel.confirm_delivery()

    def publish_event(self, event: TransferEvent) -> None:
        """Publish a brand-new event to the main queue."""
        self._publish(self._settings.queue_name, event)
        logger.info(
            "event published",
            extra={"correlationId": event.correlationId, "eventId": event.eventId},
        )

    def publish_retry(self, event: TransferEvent, delay_ms: int) -> None:
        """Schedule ``event`` for redelivery after ``delay_ms``.

        The message sits on ``orders.inbound.retry`` until its TTL expires,
        at which point RabbitMQ dead-letters it back onto the main queue
        (see ``declare_topology``) - i.e. delayed retry without the
        delayed-message-exchange plugin.
        """
        self._publish(self._settings.retry_queue_name, event, expiration=str(delay_ms))
        attempt = event.retry.attempt if event.retry else None
        logger.info(
            "event scheduled for retry",
            extra={"correlationId": event.correlationId, "eventId": event.eventId, "attempt": attempt},
        )

    def publish_dlq(self, event: TransferEvent, reason: str) -> None:
        """Route a terminally-failed event straight to the DLQ."""
        self._publish(self._settings.dlq_name, event, headers={"x-dlq-reason": reason})
        logger.warning(
            "event routed to DLQ",
            extra={"correlationId": event.correlationId, "eventId": event.eventId},
        )

    def _publish(
        self,
        queue: str,
        event: TransferEvent,
        *,
        expiration: str | None = None,
        headers: dict | None = None,
    ) -> None:
        properties = pika.BasicProperties(
            content_type="application/json",
            delivery_mode=pika.DeliveryMode.Persistent,
            message_id=event.eventId,
            correlation_id=event.correlationId,
            expiration=expiration,
            headers=headers,
        )
        self._channel.basic_publish(
            exchange="",
            routing_key=queue,
            body=event.to_json_bytes(),
            properties=properties,
            mandatory=True,
        )


def connect_and_setup(settings: Settings) -> tuple[pika.BlockingConnection, BlockingChannel, Publisher]:
    """Open a connection, declare topology, and wrap it in a ``Publisher``.

    Used for both initial setup and reconnection after a dropped connection.
    """
    connection = connect(settings)
    channel = connection.channel()
    declare_topology(channel, settings)
    return connection, channel, Publisher(channel, settings)
