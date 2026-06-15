"""Unit tests for src.csv_security - basic pre-upload CSV checks."""

from __future__ import annotations

import dataclasses

from src.csv_security import scan_csv_file


def test_clean_csv_passes(write_file, settings):
    path = write_file("orders/ORD-1.csv", b"order_id,sku,quantity,channel\nORD-1,SKU-1,3,storefront\n")

    assert scan_csv_file(str(path), settings) == []


def test_oversized_file_is_rejected(write_file, settings):
    path = write_file("orders/big.csv", b"a" * 100)
    settings = dataclasses.replace(settings, max_csv_file_size_bytes=10)

    issues = scan_csv_file(str(path), settings)

    assert len(issues) == 1
    assert issues[0].startswith("file_too_large:")


def test_null_byte_content_is_rejected(write_file, settings):
    path = write_file("orders/binary.csv", b"order_id,sku\x00,quantity\n1,2,3\n")

    assert scan_csv_file(str(path), settings) == ["binary_content:null_byte"]


def test_non_utf8_content_is_rejected(write_file, settings):
    path = write_file("orders/latin1.csv", "order_id,sku\nORD-1,café\n".encode("latin-1"))

    issues = scan_csv_file(str(path), settings)

    assert len(issues) == 1
    assert issues[0].startswith("invalid_encoding:")


def test_formula_injection_prefixes_are_flagged(write_file, settings):
    content = (
        b"order_id,sku,quantity,channel\n"
        b"ORD-1,=cmd|' /c calc'!A0,3,storefront\n"
        b"ORD-2,+SKU-2,1,ebay\n"
        b"ORD-3,SKU-3,2,amazon\n"
    )
    path = write_file("orders/injection.csv", content)

    issues = scan_csv_file(str(path), settings)

    assert "formula_injection:row=2,col=2" in issues
    assert "formula_injection:row=3,col=2" in issues
    assert not any("row=4" in issue for issue in issues)
