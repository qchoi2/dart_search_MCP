"""Stable internal and user-facing error model."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class ErrorCode(str, Enum):
    DATE_RANGE_REQUIRED = "DATE_RANGE_REQUIRED"
    API_KEY_MISSING = "API_KEY_MISSING"
    OPENDART_KEY_UNREGISTERED = "OPENDART_KEY_UNREGISTERED"
    OPENDART_KEY_SUSPENDED = "OPENDART_KEY_SUSPENDED"
    OPENDART_IP_NOT_ALLOWED = "OPENDART_IP_NOT_ALLOWED"
    OPENDART_NO_DATA = "OPENDART_NO_DATA"
    OPENDART_FILE_NOT_FOUND = "OPENDART_FILE_NOT_FOUND"
    OPENDART_REQUEST_LIMIT_EXCEEDED = "OPENDART_REQUEST_LIMIT_EXCEEDED"
    OPENDART_COMPANY_LIMIT_EXCEEDED = "OPENDART_COMPANY_LIMIT_EXCEEDED"
    OPENDART_INVALID_FIELD_VALUE = "OPENDART_INVALID_FIELD_VALUE"
    OPENDART_IMPROPER_ACCESS = "OPENDART_IMPROPER_ACCESS"
    OPENDART_SERVICE_MAINTENANCE = "OPENDART_SERVICE_MAINTENANCE"
    OPENDART_UNDEFINED_ERROR = "OPENDART_UNDEFINED_ERROR"
    OPENDART_PRIVACY_RETENTION_EXPIRED = "OPENDART_PRIVACY_RETENTION_EXPIRED"
    OPENDART_HTTP_RATE_LIMITED = "OPENDART_HTTP_RATE_LIMITED"
    OPENDART_TEMPORARY_FAILURE = "OPENDART_TEMPORARY_FAILURE"
    DART_FULLTEXT_STRUCTURE_CHANGED = "DART_FULLTEXT_STRUCTURE_CHANGED"
    DART_FULLTEXT_CIRCUIT_OPEN = "DART_FULLTEXT_CIRCUIT_OPEN"
    DOCUMENT_PARSE_FAILED = "DOCUMENT_PARSE_FAILED"
    DOCUMENT_BUDGET_EXCEEDED = "DOCUMENT_BUDGET_EXCEEDED"
    SEARCH_TIMEOUT_PARTIAL = "SEARCH_TIMEOUT_PARTIAL"
    INVALID_CONTINUATION_TOKEN = "INVALID_CONTINUATION_TOKEN"


@dataclass(frozen=True, slots=True)
class SearchError(Exception):
    code: ErrorCode
    message: str
    retryable: bool = False
    dart_status_code: str | None = None
    details: dict[str, Any] | None = None

    def __str__(self) -> str:
        return self.message

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code.value,
            "message": self.message,
            "retryable": self.retryable,
            "dart_status_code": self.dart_status_code,
            "details": self.details or {},
        }
