"""Unit tests for src.idempotency.IdempotencyStore."""

from __future__ import annotations

from src.idempotency import IdempotencyStore

KEY = "sha256:" + "a" * 64


def test_unknown_key_is_not_done():
    store = IdempotencyStore(":memory:")
    try:
        assert store.is_done(KEY) is False
    finally:
        store.close()


def test_mark_done_then_is_done_true():
    store = IdempotencyStore(":memory:")
    try:
        store.mark_done(KEY, correlation_id="corr-1")
        assert store.is_done(KEY) is True
    finally:
        store.close()


def test_mark_done_is_idempotent():
    store = IdempotencyStore(":memory:")
    try:
        store.mark_done(KEY, correlation_id="corr-1")
        store.mark_done(KEY, correlation_id="corr-2")  # must not raise
        assert store.is_done(KEY) is True
    finally:
        store.close()


def test_persists_across_instances(tmp_path):
    db_path = str(tmp_path / "idempotency.db")

    store1 = IdempotencyStore(db_path)
    store1.mark_done(KEY, correlation_id="corr-1")
    store1.close()

    store2 = IdempotencyStore(db_path)
    try:
        assert store2.is_done(KEY) is True
    finally:
        store2.close()
