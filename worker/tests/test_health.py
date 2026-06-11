"""Unit tests for src.health - readiness state and HTTP endpoints."""

from __future__ import annotations

import http.client
import socket
import threading
import time

from src.health import ReadinessState, run_health_server


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_readiness_requires_all_components_ready():
    readiness = ReadinessState(["watcher", "consumer"])

    assert readiness.ready is False

    readiness.set_ready("watcher", True)
    assert readiness.ready is False

    readiness.set_ready("consumer", True)
    assert readiness.ready is True


def test_health_ready_and_metrics_endpoints():
    readiness = ReadinessState(["watcher"])
    stop_event = threading.Event()
    port = _free_port()

    server_thread = threading.Thread(
        target=run_health_server,
        args=("127.0.0.1", port, readiness, stop_event),
        daemon=True,
    )
    server_thread.start()
    time.sleep(0.2)  # give the server a moment to bind

    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)

        conn.request("GET", "/health")
        assert conn.getresponse().status == 200

        conn.request("GET", "/ready")
        assert conn.getresponse().status == 503

        readiness.set_ready("watcher", True)
        conn.request("GET", "/ready")
        assert conn.getresponse().status == 200

        conn.request("GET", "/metrics")
        resp = conn.getresponse()
        assert resp.status == 200
        assert b"events_published_total" in resp.read()

        conn.request("GET", "/nope")
        assert conn.getresponse().status == 404
    finally:
        stop_event.set()
        server_thread.join(timeout=5)
