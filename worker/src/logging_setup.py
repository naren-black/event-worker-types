"""JSON logging configuration with correlation-id support.

Every log line is a single JSON object on stdout. Pass the optional fields
(``correlationId``, ``eventId``, ``idempotencyKey``, ``attempt``) via the
stdlib ``extra=`` kwarg and they'll be included automatically when present.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

_EXTRA_FIELDS = ("correlationId", "eventId", "idempotencyKey", "attempt")


class JsonFormatter(logging.Formatter):
    def __init__(self, service_name: str) -> None:
        super().__init__()
        self._service_name = service_name

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S") + "Z",
            "level": record.levelname,
            "service": self._service_name,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for field in _EXTRA_FIELDS:
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(service_name: str, level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(service_name))

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())
