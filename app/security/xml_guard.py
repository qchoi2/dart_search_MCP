"""Small XML guard for untrusted DART payloads."""

from __future__ import annotations

import xml.etree.ElementTree as ET

from app.errors import ErrorCode, SearchError


def parse_xml_safely(payload: bytes | str) -> ET.Element:
    raw = payload.encode("utf-8") if isinstance(payload, str) else payload
    upper = raw[:8192].upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise SearchError(ErrorCode.DOCUMENT_PARSE_FAILED, "외부 엔터티가 포함된 XML을 차단했습니다.")
    try:
        return ET.fromstring(raw)
    except ET.ParseError as exc:
        raise SearchError(ErrorCode.DOCUMENT_PARSE_FAILED, "XML 구조를 해석할 수 없습니다.") from exc
