# 개발 진행 현황

## 단계 1 핵심 공시검색 MCP 완료 (이번 세션)

- 단계 0·0.5·0.6은 다시 실측하지 않고 완료 fixture와 `DEVELOPMENT_PLAN.md` v19를 기준으로 구현했다.
- `SearchRequest`, 불변 `SearchPlan`, 실행 전용 `SearchExecutionDiagnostics`, `DisclosureCandidate`, `VerifiedCase`, `EvidenceSnippet`, `ChannelStatus`, `schema_version=1.0` 계약을 고정했다.
- 설정·경로·환경변수, Cookie 세션을 유지하는 strict-TLS HTTP, 오류모델, 원자적 저장, ZIP/XML 방어, 감사로그, continuation, 프롬프트 인젝션 경계를 구현했다.
- 모든 운영 수치 기본값은 `app/config/defaults.py`를 단일 출처로 두고 `settings.json`, rules, 계획서와의 불일치를 CI 테스트로 탐지한다. TTL 디스크 캐시는 기본 `false`다.
- OpenDART 회사코드 1일 TTL, 회사명·종목코드 조회, 포함경계 3개월 창, 100건 페이지네이션, 날짜 내림차순, `total_count/total_page`, `corp_cls`, 전역 접수번호 중복제거, 원문 ZIP·인코딩·근거발췌·뷰어 링크를 구현했다.
- OpenDART 상태 `000·013·010·011·012·014·020·021·100·101·800·900·901`을 정상·무데이터·사용자조치·개발오류·즉시중단·제한적 재시도로 분리했다.
- DART 본문검색은 동일 Cookie 세션·검색 모드에서 검색어가 바뀌어도 모드설정 POST를 반복하지 않으며, 실효 페이지 크기 10·동시성 1·최소 1,000ms·식별형 UA·TLS 검증을 고정했다.
- DART 결과행의 시장문자, 공시그룹, 본문/첨부, 제출인, 접수일, 접수번호를 파싱하고 본문 우선 중복제거, 로컬 `mechanical_score`, 최신순 조기종료 편향 진단을 구현했다.
- `rm` 원문·순서·미지 플래그와 공식 8종/복수 보고서 접두어를 보존한다. `철`은 명시적 원접수번호·원 제출일·원문 근거 없이는 연결하지 않고, `채` 조합은 일반 문자분해만 적용한다.
- `search_disclosure_cases`, `get_disclosure_evidence` MCP 도구와 stdio JSON-RPC 진입점을 구현했다. 기간 불명확 시 무호출 확인요청, 최대 원문 40·결과 20·예비 10, 부분결과 continuation, 정상 0건/채널 장애 구분, 근거·링크 반환을 적용했다.
- DART 구조·접근 장애 15분, 일시 네트워크 장애 3분 회로차단과 open 상태 무호출 OpenDART 폴백을 구현했다.
- Fast Path 제외항목(KIND 자동검색, 정정 구조 diff, 복수 사건 연결, 승인형 배치 실행, 전체 기간창 사전조사, 별도 LLM, TTL 디스크 기본 활성화)은 구현·자동실행하지 않았다.
- 내부 커밋 경계 `common-contracts-and-config`, `opendart-core`, `dart-fulltext-adapter`, `search-execution-and-mcp`, `evaluation-and-regression-tests`를 유지했다.
- 고정 평가질의 24개와 기지 정답을 `tests/golden_cases/stage1/evaluation_queries.json`에 추가했다. 자동 테스트 72개와 24/24 평가가 통과했다.
- 로컬 벤치마크: 계획 생성 p95 0.0144ms, DART fixture 파서 p95 2.2860ms, 캐시 40문서·7,680,000 텍스트 바이트, `tracemalloc` peak 5,447,992바이트다.
- 현재 중단점: 단계 1 핵심 공시검색 MCP 구현·검증 완료. 단계 2 이후 기능은 시작하지 않는다.

## 단계 0.6 완료

- 최종 run_id: `stage0_6_20260716T163708Z`
- 상태: 완료(59 / 60 요청, 58.753초)
- `GATE-DART-QUERY-SWITCH`: `passed`
- `GATE-DART-PAGESIZE`: `failed`; effective page size 10 유지
- `GATE-DART-DATE-WINDOW`: `passed`
- `GATE-RM-WITHDRAWAL`: `partially_passed`; `철` provisional 유지
- `GATE-RM-BOND`: `partially_passed`; `채=채권상장법인` 의미는 confirmed, 조합문자 실측은 unconfirmed
- raw fixture와 golden fixture, 실행별 manifest를 분리해 저장했다.
- API 키·Cookie·개인식별 가능 헤더는 fixture에 저장하지 않았다.
- 단계 1 본개발 및 기존 제품 기능 구현은 시작하지 않았다.
- 현재 중단점: 단계 0.6 산출물 기록 완료, `stage_1_started=false`.

## 단계 0.5 실측

- run_id: `stage0_5_20260716T150948Z`
- 상태: 완료 (87 / 120 요청)
- `GATE-AMENDMENT-STRATA`: `passed`
- `GATE-D004-EQUAL-FILER`: `passed`
- `GATE-DART-PAGINATION`: `partially_passed`
- `GATE-FLAGSHIP-TERMS`: `passed`
- `GATE-RM-MARKET`: `partially_passed`
- raw와 golden fixture를 분리했다.
- 단계 1 본개발: 미착수
- 다음 행동: 미통과·부분통과 게이트를 확인한 뒤 사용자가 별도로 개발 착수를 지시할 때까지 중단
