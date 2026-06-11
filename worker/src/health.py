"""Minimal stdlib HTTP server: /health, /ready, /metrics.

Deliberately dependency-light (no Flask/FastAPI) to keep the runtime image
small and the dependency surface a security scanner needs to cover minimal.
"""

from __future__ import annotations

import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import metrics

logger = logging.getLogger(__name__)


class ReadinessState:
    """Thread-safe readiness flags for each long-running component.

    ``/ready`` only returns 200 once every registered component reports
    ready (e.g. both the watcher and consumer have a broker connection).
    """

    def __init__(self, components: list[str]) -> None:
        self._lock = threading.Lock()
        self._status = {name: False for name in components}

    def set_ready(self, component: str, ready: bool) -> None:
        with self._lock:
            self._status[component] = ready

    @property
    def ready(self) -> bool:
        with self._lock:
            return all(self._status.values())

    @property
    def detail(self) -> dict[str, bool]:
        with self._lock:
            return dict(self._status)


def _make_handler(readiness: ReadinessState) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
            if self.path == "/health":
                self._respond(200, b'{"status":"ok"}', "application/json")
            elif self.path == "/ready":
                if readiness.ready:
                    self._respond(200, b'{"status":"ready"}', "application/json")
                else:
                    import json

                    body = json.dumps({"status": "not_ready", "components": readiness.detail}).encode()
                    self._respond(503, body, "application/json")
            elif self.path == "/metrics":
                body, content_type = metrics.render()
                self._respond(200, body, content_type)
            else:
                self._respond(404, b'{"error":"not_found"}', "application/json")

        def _respond(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt: str, *args: object) -> None:
            logger.debug("health server: " + fmt, *args)

    return Handler


def run_health_server(host: str, port: int, readiness: ReadinessState, stop_event: threading.Event) -> None:
    """Serve until ``stop_event`` is set. Blocks the calling thread."""
    server = ThreadingHTTPServer((host, port), _make_handler(readiness))
    thread = threading.Thread(target=server.serve_forever, name="health-http", daemon=True)
    thread.start()
    logger.info("health server listening on %s:%s", host, port)
    try:
        stop_event.wait()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        logger.info("health server stopped")
