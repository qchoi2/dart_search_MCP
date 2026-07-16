"""JSON Schemas exposed through MCP tools/list."""

from app.config import defaults

SEARCH_TOOL = {
    "name": "search_disclosure_cases",
    "description": "기간이 명확한 한국 DART 공시에서 원문 근거가 있는 사례를 제한 예산으로 검색합니다. 기간이 없으면 검색하지 않고 확인을 요청합니다. 전수·배치·파일생성은 자동 실행하지 않습니다.",
    "inputSchema": {
        "type": "object",
        "required": ["query"],
        "properties": {
            "query": {"type": "string", "minLength": defaults.MIN_TARGET_COUNT, "maxLength": defaults.QUERY_MAX_CHARS},
            "company": {"type": ["string", "null"]},
            "date_from": {"type": ["string", "null"], "format": "date"},
            "date_to": {"type": ["string", "null"], "format": "date"},
            "target_count": {"type": "integer", "minimum": defaults.MIN_TARGET_COUNT, "maximum": defaults.INTERACTIVE_TARGET_MAX, "default": defaults.DEFAULT_TARGET_COUNT},
            "mode": {"type": "string", "enum": ["fast", "standard"], "default": "standard"},
            "max_documents": {"type": ["integer", "null"], "minimum": defaults.MIN_TARGET_COUNT, "maximum": defaults.DOCUMENT_BUDGET_ABSOLUTE_MAX},
            "exhaustive": {"type": ["boolean", "null"]},
            "continuation_token": {"type": ["string", "null"]},
        },
        "additionalProperties": False,
    },
}

EVIDENCE_TOOL = {
    "name": "get_disclosure_evidence",
    "description": "접수번호의 공시 원문에서 지정 검색어 주변 근거를 최대 8개·각 500자로 반환합니다. 원문 전체나 정정 diff는 반환하지 않습니다.",
    "inputSchema": {
        "type": "object",
        "required": ["receipt_no", "keywords"],
        "properties": {
            "receipt_no": {"type": "string", "pattern": "^[0-9]{14}$"},
            "keywords": {"type": "array", "items": {"type": "string"}, "minItems": 1},
            "include_full_preview": {"type": "boolean", "default": False},
            "include_amendment_context": {"type": "boolean", "default": True},
        },
        "additionalProperties": False,
    },
}
