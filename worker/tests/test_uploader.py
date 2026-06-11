"""Unit tests for src.uploader - concurrent multi-cloud upload."""

from __future__ import annotations

import asyncio
import dataclasses
import time
from types import SimpleNamespace

from src import uploader
from src.schema import TransferEvent
from src.uploader import UploadResult, upload_all


def test_upload_all_happy_path_calls_every_destination(monkeypatch, settings, example_event_dict, write_file):
    path = write_file("orders/ORD-1.csv", b"hello")
    event = TransferEvent.model_validate(example_event_dict)

    calls: list[str] = []

    def fake_upload(_settings, destination, _source_path):
        calls.append(destination.provider)

    monkeypatch.setitem(uploader._UPLOADERS, "aws-s3", fake_upload)
    monkeypatch.setitem(uploader._UPLOADERS, "gcp-gcs", fake_upload)

    results = asyncio.run(upload_all(settings, event, str(path)))

    assert all(r.ok for r in results)
    assert sorted(calls) == ["aws-s3", "gcp-gcs"]


def test_upload_one_times_out(monkeypatch, settings, example_event_dict, write_file):
    path = write_file("orders/ORD-1.csv", b"hello")
    event = TransferEvent.model_validate(example_event_dict)

    def slow_upload(_settings, _destination, _source_path):
        time.sleep(1)

    monkeypatch.setitem(uploader._UPLOADERS, "aws-s3", slow_upload)
    monkeypatch.setitem(uploader._UPLOADERS, "gcp-gcs", lambda *a, **k: None)

    fast_settings = dataclasses.replace(settings, upload_timeout_s=0.05)

    results = asyncio.run(upload_all(fast_settings, event, str(path)))

    s3_result = next(r for r in results if r.provider == "aws-s3")
    gcs_result = next(r for r in results if r.provider == "gcp-gcs")
    assert s3_result == UploadResult(provider="aws-s3", ok=False, error="aws-s3:upload_timeout")
    assert gcs_result.ok is True


def test_upload_one_records_exception_class_as_error(monkeypatch, settings, example_event_dict, write_file):
    path = write_file("orders/ORD-1.csv", b"hello")
    event = TransferEvent.model_validate(example_event_dict)

    def failing_upload(_settings, _destination, _source_path):
        raise RuntimeError("boom")

    monkeypatch.setitem(uploader._UPLOADERS, "aws-s3", failing_upload)
    monkeypatch.setitem(uploader._UPLOADERS, "gcp-gcs", lambda *a, **k: None)

    results = asyncio.run(upload_all(settings, event, str(path)))

    s3_result = next(r for r in results if r.provider == "aws-s3")
    assert s3_result == UploadResult(provider="aws-s3", ok=False, error="aws-s3:RuntimeError")


def test_unsupported_provider_returns_error_result(settings, tmp_path):
    destination = SimpleNamespace(provider="azure-blob", bucket="b", key="k", region=None)

    result = asyncio.run(uploader._upload_one(settings, destination, str(tmp_path / "whatever")))

    assert result == UploadResult(provider="azure-blob", ok=False, error="unsupported_provider:azure-blob")


def test_s3_client_uses_endpoint_override(settings):
    custom = dataclasses.replace(settings, s3_endpoint_url="http://localhost:9000")

    client = uploader.s3_client(custom)

    assert client.meta.endpoint_url == "http://localhost:9000"


def test_gcs_client_with_emulator_endpoint_does_not_raise(settings):
    custom = dataclasses.replace(settings, gcs_endpoint_url="http://localhost:4443")

    client = uploader.gcs_client(custom)

    assert client.project == settings.gcp_project
