"""Basic security checks run on a CSV file before it's uploaded.

Three checks, in order (each can short-circuit the rest since a file that
fails one isn't meaningfully a CSV anymore):

1. **Size limit** - reject oversized files before reading them into memory.
2. **Binary content** - reject files containing NUL bytes (a CSV with
   embedded NULs isn't text; likely a non-CSV file with a ``.csv`` extension).
3. **Encoding** - reject files that aren't valid UTF-8.
4. **Formula/CSV injection (CWE-1236)** - flag any cell whose first
   character is one Excel/Sheets/LibreOffice treat as a formula prefix
   (``=``, ``+``, ``-``, ``@``, tab, or CR). Opening such a file in a
   spreadsheet app can execute the "formula" - the standard CSV-injection
   attack vector.

A non-empty return value means the file should not be uploaded.
"""

from __future__ import annotations

import csv
import io
import os

from .config import Settings

# Per OWASP's CSV injection guidance. Note "+"/"-" can also be a leading
# character of a legitimate negative number or signed value - this is a
# basic, deliberately conservative check, so such fields will be flagged.
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def scan_csv_file(path: str, settings: Settings) -> list[str]:
    """Return a list of issue descriptions; empty means the file passed."""
    size_bytes = os.path.getsize(path)
    if size_bytes > settings.max_csv_file_size_bytes:
        return [f"file_too_large:{size_bytes}>{settings.max_csv_file_size_bytes}"]

    with open(path, "rb") as f:
        raw = f.read()

    if b"\x00" in raw:
        return ["binary_content:null_byte"]

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        return [f"invalid_encoding:{exc.reason}"]

    issues = []
    for row_idx, row in enumerate(csv.reader(io.StringIO(text)), start=1):
        for col_idx, cell in enumerate(row, start=1):
            if cell.startswith(_FORMULA_PREFIXES):
                issues.append(f"formula_injection:row={row_idx},col={col_idx}")
    return issues
