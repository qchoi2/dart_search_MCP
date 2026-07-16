# DART 공시검색 MCP — 단계 1 Fast Path

이 저장소의 단계 1 구현은 OpenDART 목록·원문과 DART 본문검색을 제한 예산 안에서 결합해, 접수번호와 원문 근거가 있는 공시 사례를 반환한다. 전수 배치, KIND 자동검색, 정정 diff, 복수 사건 연결, 자동 CSV·JSON 생성은 아직 실행하지 않는다.

## 실행

1. `_local_data/.env`에 `DART_API_KEY=<OpenDART API 키>`를 저장한다.
2. 프로젝트 루트에서 `python -m app.mcp_server.server`를 실행한다.
3. MCP 클라이언트에서 `search_disclosure_cases` 또는 `get_disclosure_evidence`를 호출한다.

기간이 없는 검색은 네트워크를 호출하지 않고 날짜 확인을 요청한다. 설정 기본값은 루트 `settings.json`에 있으며 TTL 디스크 캐시는 기본 비활성이다.

## 검증

```text
python -m unittest discover -s tests -p "test*.py"
python -m app.evaluation
```

두 번째 명령은 단계 0·0.5·0.6 fixture에 기반한 24개 고정 평가질의와 로컬 성능·메모리 벤치마크를 실행한다.
