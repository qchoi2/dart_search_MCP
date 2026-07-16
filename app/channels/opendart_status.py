"""OpenDART status normalization and retry policy."""

from __future__ import annotations

from dataclasses import dataclass

from app.errors import ErrorCode, SearchError


@dataclass(frozen=True, slots=True)
class OpenDartStatus:
    code: str
    healthy: bool
    no_data: bool
    retryable: bool
    error_code: ErrorCode | None


STATUS_MAP = {
    "000": OpenDartStatus("000", True, False, False, None),
    "013": OpenDartStatus("013", True, True, False, ErrorCode.OPENDART_NO_DATA),
    "010": OpenDartStatus("010", False, False, False, ErrorCode.OPENDART_KEY_UNREGISTERED),
    "011": OpenDartStatus("011", False, False, False, ErrorCode.OPENDART_KEY_SUSPENDED),
    "012": OpenDartStatus("012", False, False, False, ErrorCode.OPENDART_IP_NOT_ALLOWED),
    "014": OpenDartStatus("014", False, False, False, ErrorCode.OPENDART_FILE_NOT_FOUND),
    "020": OpenDartStatus("020", False, False, False, ErrorCode.OPENDART_REQUEST_LIMIT_EXCEEDED),
    "021": OpenDartStatus("021", False, False, False, ErrorCode.OPENDART_COMPANY_LIMIT_EXCEEDED),
    "100": OpenDartStatus("100", False, False, False, ErrorCode.OPENDART_INVALID_FIELD_VALUE),
    "101": OpenDartStatus("101", False, False, False, ErrorCode.OPENDART_IMPROPER_ACCESS),
    "800": OpenDartStatus("800", False, False, False, ErrorCode.OPENDART_SERVICE_MAINTENANCE),
    "900": OpenDartStatus("900", False, False, True, ErrorCode.OPENDART_UNDEFINED_ERROR),
    "901": OpenDartStatus("901", False, False, False, ErrorCode.OPENDART_PRIVACY_RETENTION_EXPIRED),
}

_MESSAGES = {
    "010": "OpenDART 인증키가 등록되지 않았습니다.",
    "011": "OpenDART 인증키가 사용 중지 상태입니다.",
    "012": "현재 IP에서는 OpenDART 인증키를 사용할 수 없습니다.",
    "014": "요청한 공시 원문 파일이 없습니다.",
    "020": "OpenDART 요청한도를 초과하여 재시도 없이 검색을 중단합니다.",
    "021": "조회 대상 회사를 100개 이하 묶음으로 나눠야 합니다.",
    "100": "OpenDART 요청 필드값이 올바르지 않습니다.",
    "101": "OpenDART가 요청을 부적절한 접근으로 분류했습니다.",
    "800": "OpenDART가 점검 중이므로 복구 후 다시 실행해야 합니다.",
    "900": "OpenDART에서 정의되지 않은 오류가 발생했습니다.",
    "901": "OpenDART 개인정보 보유기간이 만료된 상태입니다.",
}


def classify_status(code: str) -> OpenDartStatus:
    return STATUS_MAP.get(code, STATUS_MAP["900"])


def ensure_success(payload: dict, *, allow_no_data: bool = True) -> OpenDartStatus:
    code = str(payload.get("status", "900"))
    status = classify_status(code)
    if status.healthy and (allow_no_data or not status.no_data):
        return status
    message = _MESSAGES.get(code, str(payload.get("message") or "OpenDART 오류가 발생했습니다."))
    raise SearchError(status.error_code or ErrorCode.OPENDART_UNDEFINED_ERROR, message, status.retryable, code)
