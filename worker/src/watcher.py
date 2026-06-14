"""inotify-based watcher for the SFTP drop directory.

``watchdog`` (backed by inotify on Linux) detects when a file write
completes (``IN_CLOSE_WRITE``, surfaced as ``on_closed``) or a temp file is
renamed into place (``on_moved`` - common for SFTP clients), then we build
and publish a ``file.transfer.requested`` event for it.

The watchdog Observer delivers callbacks on its own background thread, but
``pika.BlockingConnection`` is not thread-safe. To keep all broker I/O on a
single thread, the handler only pushes detected paths onto a ``queue.Queue``;
``run_watcher`` (running on its own thread) drains that queue and does the
actual publishing.
"""

from __future__ import annotations

import contextlib
import logging
import os
import queue
import threading

import pika
from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from . import metrics
from . import publisher as publisher_mod
from .config import Settings
from .health import ReadinessState
from .publisher import Publisher
from .schema import Destination, FileMeta, Source, TransferEvent
from .utils import compute_idempotency_key, new_uuid, sha256_file, utcnow_iso

logger = logging.getLogger(__name__)

_CONTENT_TYPES = {
    ".csv": "text/csv",
    ".json": "application/json",
    ".txt": "text/plain",
}


def guess_content_type(filename: str) -> str:
    _, ext = os.path.splitext(filename)
    return _CONTENT_TYPES.get(ext.lower(), "application/octet-stream")


def build_event(path: str, settings: Settings) -> TransferEvent:
    """Build a ``file.transfer.requested`` event for a fully-written file."""
    size_bytes = os.path.getsize(path)
    checksum = sha256_file(path)
    relative_key = os.path.relpath(path, settings.watch_dir)

    idempotency_key = compute_idempotency_key(
        secret=settings.correlation_secret,
        source_path=path,
        size_bytes=size_bytes,
        checksum_sha256=checksum,
    )

    return TransferEvent(
        schemaVersion="1.0",
        eventType="file.transfer.requested",
        eventId=new_uuid(),
        correlationId=new_uuid(),
        idempotencyKey=idempotency_key,
        occurredAt=utcnow_iso(),
        source=Source(provider="sftp", path=path),
        file=FileMeta(
            name=os.path.basename(path),
            sizeBytes=size_bytes,
            checksumSha256=checksum,
            contentType=guess_content_type(path),
        ),
        destinations=[
            Destination(
                provider="aws-s3",
                bucket=settings.s3_bucket,
                key=relative_key,
                region=settings.s3_region,
            ),
            Destination(
                provider="gcp-gcs",
                bucket=settings.gcs_bucket,
                key=relative_key,
            ),
        ],
    )


class DetectedFileHandler(FileSystemEventHandler):
    """Pushes paths of completed files onto a thread-safe queue."""

    def __init__(self, file_queue: queue.Queue[str]) -> None:
        self._queue = file_queue

    def on_closed(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        self._queue.put(event.src_path)

    def on_moved(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        self._queue.put(event.dest_path)


def _reconnect(
    connection: pika.BlockingConnection, settings: Settings
) -> tuple[pika.BlockingConnection, Publisher]:
    """Close a (presumed dead) connection and open a fresh one with topology re-declared."""
    with contextlib.suppress(Exception):
        connection.close()
    connection, _channel, publisher = publisher_mod.connect_and_setup(settings)
    return connection, publisher


def _publish_with_reconnect(
    connection: pika.BlockingConnection,
    publisher: Publisher,
    event: TransferEvent,
    settings: Settings,
) -> tuple[pika.BlockingConnection, Publisher]:
    """Publish ``event``, transparently reconnecting once if the connection/channel is dead."""
    try:
        publisher.publish_event(event)
        metrics.EVENTS_PUBLISHED_TOTAL.inc()
        return connection, publisher
    except pika.exceptions.AMQPError:
        logger.exception("publish failed for eventId=%s, reconnecting and retrying", event.eventId)

    connection, publisher = _reconnect(connection, settings)
    try:
        publisher.publish_event(event)
        metrics.EVENTS_PUBLISHED_TOTAL.inc()
    except Exception:
        logger.exception("retry after reconnect failed for eventId=%s", event.eventId)
    return connection, publisher


def _service_idle_connection(
    connection: pika.BlockingConnection, publisher: Publisher, settings: Settings
) -> tuple[pika.BlockingConnection, Publisher]:
    """Drive pika's I/O loop so heartbeats keep the connection alive while idle.

    Reconnects if the connection has already been dropped (e.g. by RabbitMQ's
    own heartbeat timeout) before this had a chance to service it.
    """
    try:
        connection.process_data_events(time_limit=0)
        return connection, publisher
    except pika.exceptions.AMQPError:
        logger.exception("watcher connection lost while idle, reconnecting")
        return _reconnect(connection, settings)


def run_watcher(settings: Settings, stop_event: threading.Event, readiness: ReadinessState) -> None:
    """Watch ``settings.watch_dir`` and publish an event per completed file.

    Owns its own RabbitMQ connection (created here, reconnected as needed,
    closed on exit) and blocks until ``stop_event`` is set.
    """
    connection, _channel, publisher = publisher_mod.connect_and_setup(settings)
    readiness.set_ready("watcher", True)

    file_queue: queue.Queue[str] = queue.Queue()
    handler = DetectedFileHandler(file_queue)

    observer = Observer()
    observer.schedule(handler, settings.watch_dir, recursive=True)
    observer.start()
    logger.info("watcher started watching %s", settings.watch_dir)

    try:
        while not stop_event.is_set():
            try:
                path = file_queue.get(timeout=settings.stable_check_interval_s)
            except queue.Empty:
                connection, publisher = _service_idle_connection(connection, publisher, settings)
                continue

            if not os.path.isfile(path):
                continue

            try:
                event = build_event(path, settings)
            except Exception:
                logger.exception("failed to build event for %s", path)
                continue

            connection, publisher = _publish_with_reconnect(connection, publisher, event, settings)
    finally:
        readiness.set_ready("watcher", False)
        observer.stop()
        observer.join()
        with contextlib.suppress(Exception):
            connection.close()
        logger.info("watcher stopped")
