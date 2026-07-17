"""Spreadsheet-safe CSV cell handling for untrusted disclosure text."""

from __future__ import annotations

from typing import Any


_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def has_formula_prefix(value: Any) -> bool:
    return str(value).startswith(_FORMULA_PREFIXES)


def escape_csv_cell(value: Any) -> str:
    text = "" if value is None else str(value)
    return f"'{text}" if has_formula_prefix(text) else text
