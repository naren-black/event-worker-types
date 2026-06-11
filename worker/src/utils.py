"""Small shared helpers: hashing, timestamps, ids."""

from __future__ import annotations

import hashlib
import hmac
import uuid
from datetime import UTC, datetime

_CHUNK_SIZE = 1024 * 1024


def sha256_file(path: str) -> str:
    """Stream a file through SHA-256 without loading it fully into memory."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(_CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def utcnow_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_uuid() -> str:
    return str(uuid.uuid4())


def compute_idempotency_key(secret: str, source_path: str, size_bytes: int, checksum_sha256: str) -> str:
    """Derive a stable idempotency key for (path, size, content) under ``secret``.

    HMAC rather than a bare hash so the key can't be precomputed/forged by
    anything that doesn't hold the worker's secret, while staying stable
    across redeliveries of the same file.
    """
    message = f"{source_path}|{size_bytes}|{checksum_sha256}".encode()
    digest = hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()
    return f"sha256:{digest}"
