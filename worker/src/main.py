"""Process entrypoint - run as ``python -m src.main``.

Wires together three concurrent pieces, each on its own thread (pika's
BlockingConnection is not thread-safe, so the watcher and consumer each get
their own connection):

  watcher thread  - inotify watcher -> publishes file.transfer.requested
  consumer thread - consumes, uploads to S3 + GCS, handles retry/DLQ
  main thread     - serves /health, /ready, /metrics until SIGTERM/SIGINT
"""

from __future__ import annotations

import logging
import signal
import threading
from types import FrameType

from . import publisher as publisher_mod
from .config import Settings
from .consumer import Consumer
from .health import ReadinessState, run_health_server
from .idempotency import IdempotencyStore
from .logging_setup import configure_logging
from .watcher import run_watcher

logger = logging.getLogger(__name__)


def _run_watcher(settings: Settings, stop_event: threading.Event, readiness: ReadinessState) -> None:
    connection = publisher_mod.connect(settings)
    try:
        channel = connection.channel()
        publisher_mod.declare_topology(channel, settings)
        publisher = publisher_mod.Publisher(channel, settings)
        readiness.set_ready("watcher", True)
        run_watcher(settings, publisher, stop_event)
    except Exception:
        logger.exception("watcher thread crashed")
        raise
    finally:
        readiness.set_ready("watcher", False)
        connection.close()


def _run_consumer(settings: Settings, stop_event: threading.Event, readiness: ReadinessState) -> None:
    connection = publisher_mod.connect(settings)
    try:
        channel = connection.channel()
        publisher_mod.declare_topology(channel, settings)
        publisher = publisher_mod.Publisher(channel, settings)
        idempotency = IdempotencyStore(settings.idempotency_db_path)
        try:
            readiness.set_ready("consumer", True)
            consumer = Consumer(channel, publisher, settings, idempotency)
            consumer.run(stop_event)
        finally:
            idempotency.close()
    except Exception:
        logger.exception("consumer thread crashed")
        raise
    finally:
        readiness.set_ready("consumer", False)
        connection.close()


def main() -> None:
    settings = Settings.from_env()
    configure_logging(settings.service_name, settings.log_level)
    logger.info("starting %s", settings.service_name)

    stop_event = threading.Event()
    readiness = ReadinessState(["watcher", "consumer"])

    def _handle_signal(signum: int, _frame: FrameType | None) -> None:
        logger.info("received signal %s, shutting down", signum)
        stop_event.set()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    watcher_thread = threading.Thread(
        target=_run_watcher, args=(settings, stop_event, readiness), name="watcher", daemon=True
    )
    consumer_thread = threading.Thread(
        target=_run_consumer, args=(settings, stop_event, readiness), name="consumer", daemon=True
    )
    watcher_thread.start()
    consumer_thread.start()

    try:
        run_health_server("0.0.0.0", settings.health_port, readiness, stop_event)  # noqa: S104
    finally:
        stop_event.set()
        watcher_thread.join(timeout=10)
        consumer_thread.join(timeout=10)

    logger.info("%s stopped", settings.service_name)


if __name__ == "__main__":
    main()
