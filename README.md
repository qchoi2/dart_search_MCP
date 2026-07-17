# DART 공시검색 MCP v0.3.0

이 저장소는 OpenDART 목록·원문과 DART 본문검색을 제한 예산 안에서 결합해, 접수번호와 원문 근거가 있는 공시 사례를 반환하는 Claude Desktop MCP 서버다. 모든 검색결과에는 `original_document_url`을 제공하며, 여러 원문으로 구성된 결과는 `original_document_links`에 DART 원문 링크를 함께 제공한다.

## 사용자 설치

배포용 파일은 MCPB 패키지다.

```text
dist/공시검색-MCP-0.3.0.mcpb
dist/사용설명서.html
dist/SHA256SUMS.txt
```

Claude Desktop에서 설정 → 확장 → 고급 설정 → 확장 설치를 선택하고 `.mcpb` 파일을 설치한다. 설치 중 OpenDART API 인증키를 입력한다. 인증키는 MCPB `user_config`의 sensitive 항목으로 받으며 패키지·로그·fixture에 저장하지 않는다.

## 검색기능 표현

- `search_disclosure_cases`: 공시 MCP의 속도우선 기능. 짧은 시간 안에 우선 확인할 결과를 제공한다.
- `preview_batch_research`, `run_batch_research`, `continue_batch_research`, `export_search_results`: 공시 MCP의 심화 검색기능. 범위가 넓을 때 예상 범위와 시간을 먼저 보여주고, 사용자의 확인 뒤 실행한다.

속도우선 기능이 요청 범위를 모두 확인하지 못하면 사용자 메시지는 “공시 MCP의 속도우선 기능에서는 요청 범위를 모두 확인하지 못했습니다. 공시 MCP의 심화 검색기능으로 범위와 예상시간을 먼저 확인해 주세요. 심화 검색기능이 무엇인지 궁금하면 물어봐 주세요.” 형식으로 안내한다.

## 개발 실행

1. `_local_data/.env`에 `DART_API_KEY=<OpenDART API 키>`를 저장한다.
2. 프로젝트 루트에서 `python -m app.mcp_server.server`를 실행한다.
3. MCP 클라이언트에서 `search_disclosure_cases` 또는 `get_disclosure_evidence`를 호출한다.

Windows에서 수동 MCP 등록을 테스트할 때는 `env`에 `PYTHONUTF8=1`을 함께 지정한다.

```json
{
  "mcpServers": {
    "dart-disclosure-search": {
      "command": "python",
      "args": ["-m", "app.mcp_server.server"],
      "cwd": "<프로젝트 루트>",
      "env": {"PYTHONUTF8": "1"}
    }
  }
}
```

## 배포 패키지 생성

```text
python -m installer.build_release --output dist
```

빌더는 `app/`, `settings.json`, 아이콘, `server.py`, `pyproject.toml`, `manifest.json`만 allow-list로 묶고 `_local_data`, `.env`, 테스트 fixture, 로그, git 메타데이터는 포함하지 않는다.

## 검증

```text
python -m pytest -q
python -m app.evaluation
python -m compileall -q app installer tests
```

현재 기본값은 TTL 디스크 캐시와 영구 인덱스를 활성화하지 않는다. 영구 인덱스는 반복 수요와 실측 recall/cost 개선이 모두 확인될 때만 별도 승인 대상으로 전환된다.
