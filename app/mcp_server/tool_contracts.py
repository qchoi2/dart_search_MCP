"""JSON Schemas exposed through MCP tools/list."""

from app.config import defaults

SEARCH_TOOL = {
    "name": "search_disclosure_cases",
    "description": "공시 MCP의 속도우선 기능으로 기간이 명확한 한국 DART 공시에서 원문 근거가 있는 사례를 검색합니다. 사용자에게 검색 결과를 제시할 때 각 결과의 original_document_url 또는 original_document_links에 있는 DART 공시 원문 링크를 항상 함께 표시해야 합니다. amendment_comparison 또는 sequence_required를 켜면 S6/S7 온디맨드 관계분석을 수행합니다. 더 넓은 범위가 필요하면 공시 MCP의 심화 검색기능을 안내합니다.",
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
            "cache_mode": {"type": "string", "enum": ["auto", "session"], "default": "auto"},
            "exhaustive": {"type": ["boolean", "null"]},
            "amendment_comparison": {"type": ["boolean", "null"]},
            "sequence_required": {"type": ["boolean", "null"]},
            "output_mode": {"type": "string", "enum": ["interactive"], "default": "interactive"},
            "continuation_token": {"type": ["string", "null"]},
            "schema_version": {"type": "string", "const": defaults.SCHEMA_VERSION, "default": defaults.SCHEMA_VERSION},
        },
        "additionalProperties": False,
    },
}

EVIDENCE_TOOL = {
    "name": "get_disclosure_evidence",
    "description": "접수번호의 공시 원문에서 지정 검색어 주변 근거를 최대 8개·각 500자로 반환하고, 요청 시 명시적 정정 관계·정정표 문맥을 구조화합니다. 원문 전체는 반환하지 않습니다.",
    "inputSchema": {
        "type": "object",
        "required": ["receipt_no", "keywords"],
        "properties": {
            "receipt_no": {"type": "string", "pattern": "^[0-9]{14}$"},
            "keywords": {
                "type": "array",
                "items": {"type": "string", "minLength": 1, "maxLength": defaults.QUERY_MAX_CHARS},
                "minItems": 1,
                "maxItems": defaults.INTERACTIVE_TARGET_MAX,
                "uniqueItems": True,
            },
            "include_full_preview": {"type": "boolean", "default": False},
            "include_amendment_context": {"type": "boolean", "default": True},
        },
        "additionalProperties": False,
    },
}

BATCH_PREVIEW_TOOL = {
    "name": "preview_batch_research",
    "description": "공시 MCP의 심화 검색기능을 시작하기 전에 예상 요청 수·시간·저장량을 보여주고 확인용 plan_id를 발급합니다. 이 단계에서는 원문을 다운로드하지 않습니다.",
    "inputSchema": {
        "type": "object",
        "required": ["query", "date_from", "date_to"],
        "properties": {
            "query": {"type": "string", "minLength": 1, "maxLength": defaults.QUERY_MAX_CHARS},
            "company": {"type": ["string", "null"]},
            "date_from": {"type": "string", "format": "date"},
            "date_to": {"type": "string", "format": "date"},
            "disclosure_types": {"type": "array", "items": {"type": "string"}, "default": []},
            "target_count": {"type": "integer", "minimum": 1, "maximum": defaults.BATCH_TARGET_MAX, "default": defaults.BATCH_TARGET_MAX},
            "exhaustive": {"type": "boolean", "default": True},
        },
        "additionalProperties": False,
    },
}

BATCH_RUN_TOOL = {
    "name": "run_batch_research",
    "description": "유효한 plan_id와 명시 승인, 5/10/15/30분 실행구간을 받아 공시 MCP의 심화 검색기능을 시작합니다.",
    "inputSchema": {
        "type": "object",
        "required": ["plan_id", "approved"],
        "properties": {
            "plan_id": {"type": "string"},
            "approved": {"type": "boolean"},
            "confirmation_interval_minutes": {"type": ["integer", "null"], "enum": [5, 10, 15, 30, None]},
        },
        "additionalProperties": False,
    },
}

BATCH_CONTINUE_TOOL = {
    "name": "continue_batch_research",
    "description": "체크포인트에 저장된 공시 MCP 심화 검색을 새 승인구간 동안 이어서 실행합니다.",
    "inputSchema": {
        "type": "object",
        "required": ["job_id", "approved"],
        "properties": {
            "job_id": {"type": "string"},
            "approved": {"type": "boolean"},
            "confirmation_interval_minutes": {"type": ["integer", "null"], "enum": [5, 10, 15, 30, None]},
        },
        "additionalProperties": False,
    },
}

EXPORT_RESULTS_TOOL = {
    "name": "export_search_results",
    "description": "완료된 공시 MCP 심화 검색결과를 사용자가 지정한 폴더에 CSV/JSON으로 안전하게 저장합니다.",
    "inputSchema": {
        "type": "object",
        "required": ["search_record_id", "formats"],
        "properties": {
            "search_record_id": {"type": "string"},
            "formats": {"type": "array", "items": {"type": "string", "enum": ["csv", "json"]}, "minItems": 1, "uniqueItems": True},
            "output_directory": {"type": ["string", "null"]},
        },
        "additionalProperties": False,
    },
}


TOOLS = [
    SEARCH_TOOL,
    EVIDENCE_TOOL,
    BATCH_PREVIEW_TOOL,
    BATCH_RUN_TOOL,
    BATCH_CONTINUE_TOOL,
    EXPORT_RESULTS_TOOL,
]
