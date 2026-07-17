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
    "description": "접수번호의 공시 원문에서 지정 검색어 주변 근거를 최대 8개·각 500자로 반환합니다. 원문 전체나 정정 diff는 반환하지 않습니다.",
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
    "description": "배치 검색의 요청 수·시간·저장량을 추정하고 승인용 plan_id를 발급합니다. 원문은 다운로드하지 않습니다.",
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
    "description": "유효한 plan_id와 명시 승인, 5/10/15/30분 실행구간을 받아 배치를 시작합니다.",
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
    "description": "체크포인트의 배치 작업을 새 승인구간 동안 재개합니다.",
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
    "description": "완료된 배치 결과를 사용자가 지정한 폴더에 CSV/JSON으로 원자적으로 저장합니다.",
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
