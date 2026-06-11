"""Concurrent upload of one source file to every destination in an event.

Each destination is uploaded on its own thread (via ``asyncio.to_thread``,
since boto3 / google-cloud-storage are both synchronous), bounded by
``UPLOAD_MAX_CONCURRENCY`` and a per-destination timeout. Results are
returned rather than raised so the consumer can decide retry vs DLQ.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import time

import boto3
from botocore.config import Config as BotoConfig
from google.auth.credentials import AnonymousCredentials
from google.cloud import storage as gcs_storage

from . import metrics
from .config import Settings
from .schema import Destination, TransferEvent

logger = logging.getLogger(__name__)


@dataclasses.dataclass
class UploadResult:
    provider: str
    ok: bool
    error: str | None = None


def s3_client(settings: Settings):
    return boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint_url,
        region_name=settings.s3_region,
        # The consumer owns the retry/backoff/DLQ policy (docs/event-contract.md);
        # disable boto3's own retries so failures surface immediately.
        config=BotoConfig(retries={"max_attempts": 0}),
    )


def gcs_client(settings: Settings):
    if settings.gcs_endpoint_url:
        return gcs_storage.Client(
            project=settings.gcp_project,
            credentials=AnonymousCredentials(),
            client_options={"api_endpoint": settings.gcs_endpoint_url},
        )
    return gcs_storage.Client(project=settings.gcp_project)


def _upload_to_s3(settings: Settings, destination: Destination, source_path: str) -> None:
    s3_client(settings).upload_file(source_path, destination.bucket, destination.key)


def _upload_to_gcs(settings: Settings, destination: Destination, source_path: str) -> None:
    bucket = gcs_client(settings).bucket(destination.bucket)
    bucket.blob(destination.key).upload_from_filename(source_path)


_UPLOADERS = {
    "aws-s3": _upload_to_s3,
    "gcp-gcs": _upload_to_gcs,
}


async def _upload_one(settings: Settings, destination: Destination, source_path: str) -> UploadResult:
    fn = _UPLOADERS.get(destination.provider)
    if fn is None:
        return UploadResult(
            provider=destination.provider,
            ok=False,
            error=f"unsupported_provider:{destination.provider}",
        )

    start = time.monotonic()
    try:
        await asyncio.wait_for(
            asyncio.to_thread(fn, settings, destination, source_path),
            timeout=settings.upload_timeout_s,
        )
        metrics.UPLOADS_TOTAL.labels(provider=destination.provider, result="success").inc()
        return UploadResult(provider=destination.provider, ok=True)
    except TimeoutError:
        metrics.UPLOADS_TOTAL.labels(provider=destination.provider, result="error").inc()
        error = f"{destination.provider}:upload_timeout"
        return UploadResult(provider=destination.provider, ok=False, error=error)
    except Exception as exc:  # noqa: BLE001 - any failure here is a candidate for retry
        metrics.UPLOADS_TOTAL.labels(provider=destination.provider, result="error").inc()
        return UploadResult(
            provider=destination.provider,
            ok=False,
            error=f"{destination.provider}:{exc.__class__.__name__}",
        )
    finally:
        elapsed = time.monotonic() - start
        metrics.UPLOAD_DURATION_SECONDS.labels(provider=destination.provider).observe(elapsed)


async def upload_all(settings: Settings, event: TransferEvent, source_path: str) -> list[UploadResult]:
    """Upload ``source_path`` to every destination in ``event``, concurrently."""
    semaphore = asyncio.Semaphore(settings.upload_max_concurrency)

    async def _bounded(destination: Destination) -> UploadResult:
        async with semaphore:
            return await _upload_one(settings, destination, source_path)

    return await asyncio.gather(*(_bounded(d) for d in event.destinations))
