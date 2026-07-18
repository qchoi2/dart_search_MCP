# 테스트 결과

## 제목제약·질의분해 회귀 (2026-07-18 KST, 세션 샌드박스)

- `python -m unittest discover -s tests -p "test*.py"`: 179 tests OK.
- `python -m pytest -q`: 170 passed, 20 subtests passed.
- `python -m app.evaluation`: 24/24.
- 신규/확장 테스트: `tests/test_stage8_query_decomposition.py`(토큰 위생, 제목제약 plan, 검색변형 순서, 회귀, dart_fulltext mode 전달), `tests/test_stage4_approved_batch.py`(제목제약 batch가 report 모드로 검색하고 동시출현으로만 확정).
- MCPB 재빌드: `dist/공시검색-MCP-0.3.1.mcpb`, SHA-256 `bf50c0c6b32aac1709ab41d3c895f73d2688303cec0f947d4cfd11f19e540363`.


## 문장형 질의 개선 회귀 (2026-07-18 KST, 세션 샌드박스)

- `python -m pytest -q`: 168 passed, 20 subtests passed. 신규 `tests/test_stage8_query_decomposition.py` 6건 포함.
- 실패 2건(`test_stage7_distribution`의 `test_bundled_server_starts_from_unpacked_package`, `test_mcpb_is_deterministic_allowlisted_and_sensitive`)은 샌드박스 마운트가 임시폴더의 `service.py` unlink를 `Operation not permitted`로 막아 `TemporaryDirectory` 정리 단계에서만 발생한다. 테스트 본문 로직은 통과하며 정상 빌드 환경에서는 재현되지 않는다.
- MCPB 재빌드: `dist/공시검색-MCP-0.3.0.mcpb`, SHA-256 `b17f337477150ca2e42b6589687146bea26f11161ac243503eba4bb03856d270`. 패키지에 갱신된 `search_terms.yaml`(synonym_groups)과 `extract_cooccurrence_evidence`가 포함됨을 확인했다.


## 단계 7·8 배포 및 최종 회귀 (2026-07-18 KST)

- `python -m pytest -q`: 164 passed, 20 subtests passed, 실패 0개
- `python -m app.evaluation`: 24/24 통과
- `python -m compileall -q app installer tests`: 통과
- `python -m installer.build_release --output dist`: `dist/공시검색-MCP-0.3.0.mcpb` 생성, SHA-256 `93f9d0f27c3e0fd014be697ace0900e10d382e5e06e486dc790b4503cfc73064`
- 공식 MCPB CLI `info`: 패키지 읽기 성공, `Not signed` 경고만 표시
- 공식 MCPB CLI `validate build/mcpb-root/manifest.json`: Manifest schema validation 통과, icon validation 통과
- 단계 7 fixture: MSIX Claude 설정 탐색, 기존 설정 보존, 백업, 원자적 등록·해제, corrupt config 미덮어쓰기, MCPB deterministic build, secret 미포함, bundled stdio initialize, PNG/ICO 아이콘, 사용자 안내서 문구 통과
- 단계 8 fixture: 영구 인덱스 기본 비활성, 반복 수요만으로는 미추천, 반복 시장 전체 검색과 실측 개선이 모두 있을 때만 별도 승인 후보 통과
- 외부 수동 확인 필요: 실제 Claude Desktop 확장 설치 UI에서 `.mcpb` 설치, OpenDART API 키 입력, 재시작 후 도구 노출 확인, 서명 필요 여부 결정

## 검색결과 원문 링크 회귀 (2026-07-18)

- 자동시험: 154개 통과, 20 subtests 통과, 실패 0개
- 확정 사례: 대표 URL 및 구성 공시 링크 목록 존재 확인
- 예비 후보: OpenDART 검증 전에도 접수번호 기반 원문 URL 존재 확인
- 배치 저장결과: `original_document_url` 존재 확인
- 링크 형식: `https://dart.fss.or.kr/dsaf001/main.do?rcpNo=<14자리 접수번호>` 확인

## 단계 5·6 최종 검증 (2026-07-17)

- 자동 회귀: 154개 통과, 실패 0개
- 고정 평가: 24/24 통과
- compileall: 통과
- diff check: 통과
- 단계 5 제한 라이브: OpenDART 16요청, 목록 2에서 49.1% 단축, 원문 3에서 63.3% 단축, 오류 0건
- TTL gzip1: 최초검색 증가 1.703%, 쓰기 p95 6.737ms, 손상복구 통과, 기준 5%·100ms 충족
- 단계 6 체인 정확도: 단계 0.5 golden 20접수 중 20접수 정확(100%)
- 단계 6 실제 원문 스모크: 유상증자 `20260716000809`, 합병 `20260713000345`, 전환사채 `20260715000496`에서 최초제출일·정정표 3/3 인식
- 보수 경계: 독립사건 미병합, 불확실 체인 미확정, `rm=철` 단독 철회확정 금지, 회사명만 같은 사건 edge는 uncertain
- 미완료 주장: ADS·원주, 공개매수→주식교환 10건, 상계납입 10건 이상의 새 전수 라이브 인수시험 및 장기 90초 p95

## 단계 2~4 검토의견 반영 회귀 (2026-07-17 KST)

- `python -m pytest -q`: 139개 통과, 20 subtests 통과, 실패 0개
- `python -m app.evaluation`: 24/24 통과
- CSV formula injection: `=·+·-·@·TAB·CR` 6종 셀 이스케이프 및 CSV 전체 출력 필드 방어
- 배치 DART 경로: 90일 날짜창·페이지 후보를 사용하고 OpenDART 목록 추가호출 없이 해당 접수번호 원문만 검증
- 배치 계약: `dart_rate_floor_seconds`, open 회로의 양수 `blocked_seconds`·`blocked_until`, 순차 원문 요청, rate limit 체크포인트
- 대화형 권고: 필터 후 예상문서 80건 초과 시 구조화 권고, 동일 lineage 2회째 억제, exhaustive 요청의 명시 권고
- 감사: 배치 완료 요약 기록, 원 질의 평문 미저장, 해시·실행 변형·범위·접수번호·진단 보존
- fixture: hash 대상 경로 `.gitattributes -text` 적용

## 단계 4 승인형 배치 리서치 검증 (2026-07-17 KST)

- 기준: `21b92f5` + `DEVELOPMENT_PLAN.md` v20 단계 4
- `python -m pytest -q`: 139개 통과, 20 subtests 통과, 실패 0개
- 단계 4 전용 회귀 17개: 미리보기 원문 무호출, 동일 plan 무호출 재사용, 거절 후 30분 억제, 50% 범위증가 재허용, invalid plan·시간구간 무호출, 승인 완료, soft/hard 구간, 행 단위 체크포인트·추가승인 재개, 회로 만료 후 1회 probe, 순차 원문요청·rate limit 체크포인트, DART 날짜창 후보경로·페이지 재개, CSV 수식 방어, 배치 감사, blocked_until, 경로 없는 export 무저장, CSV/JSON 원자 저장, 경로순회 거절, MCP schema version, fixture manifest hash
- 5분 구간 fixture: soft 270초, hard 300초; 구간 종료 후 `continuation_confirmation_required`와 다음 날짜창·페이지·행 위치 반환
- 저장 검증: 체크포인트에 원 질의 평문 없음, 실제 검색 변형·정규화 해시만 보존; 결과 레코드 24시간, 체크포인트 7일; 완료 체크포인트 즉시 삭제
- 채널 검증: 단계 5 전 문서 요청 순차 1, HTTP rate limit에서 체크포인트 중단, 만료된 DART open 회로 복구 시 `PROBING` 상태진단 정확히 1회
- `python -m app.evaluation`: 24/24 통과
- `python -m compileall -q app tests`: 통과
- `git diff --check`: 통과
- 라이브 네트워크 실행, 90초 종단간 성능 통과, 동시성 4 승격은 수행하거나 주장하지 않음

## 단계 3 대화형 MCP 검증 (2026-07-17 KST)

- 기준: `454e405` + `DEVELOPMENT_PLAN.md` v20 단계 3
- `python -m unittest discover -s tests -p "test_*.py"`: 120개 통과, 실패 0개
- 단계 3 전용 회귀 11개: 완전한 도구 스키마, 입력 타입·날짜·버전 검증, 대화형 40문서·20결과 상한, 배치·정정·순서연결 사전 차단, continuation TTL·1,000개 상한·검색계보 보존, 구조 경고, 근거 크기 제한, MCP 오류 응답
- continuation fixture: 기간·페이지·처리 접수번호 해시·검색어 변형 저장, 다른 lineage 사용 시 토큰 비소비, 예산소진 시 `SEARCH_BUDGET_PARTIAL`
- 최신순 일부범위 fixture: `LATEST_FIRST_BIAS`, 질의별 `fully_pageable=false`, `completeness_grade=reduced`
- 결과 크기 fixture: 최종 case 20개, case당 접수번호 1개, 기계적 사실과 `legal_assessment=null` 분리
- 근거 fixture: 최대 8개·각 500자, 전체 원문 미반환, `source_text_untrusted=true`, 중복·빈·과다 키워드 거절
- 검색 미실행 경계: 배치·전수는 `batch_confirmation_required`, 정정비교·사건연결은 `clarification_required`, 모두 네트워크 호출 0건·완전성 `unconfirmed`
- `python -m app.evaluation`: 24/24 통과
- 라이브 네트워크 및 단계 4 배치 실행은 수행하지 않음

## 단계 2 DART 어댑터·폴백 잔여 계약 검증 (2026-07-17 KST)

- 기준: `v0.1.1-reliability` + `DEVELOPMENT_PLAN.md` v20 단계 2
- `python -m unittest discover -s tests -p "test_*.py"`: 109개 통과, 실패 0개
- 기존 DART golden HTML: 검색건수 42, 10행, 계산 마지막 페이지 5와 링크 마지막 페이지 5가 일치하고 계약 변화 오탐 없음
- fixture 변형: 링크 마지막 페이지를 6으로 바꾸면 `PAGINATION_CONTRACT_CHANGED`, 관측 세부정보, `completeness_grade=unconfirmed` 반환
- HTTP 403: 실제 상태코드 보존, `structure_or_access`, 첫 진단에서 15분 회로 open, 양수 `blocked_seconds`
- HTTP 429: 단발 실패는 `blocked_seconds=0` 구조 폴백, 반복 실패는 `network` 3분 회로 open
- 정상 `main.do` 진단이 선행 `search.ax` 네트워크 실패횟수를 지우지 않으며, 실제 결과요청 성공 시에만 `HEALTHY`로 복구
- 회로 만료: 상태진단 1회로 `PROBING → HEALTHY`; 실패 시 즉시 재open하며 각각 `probe_result=success|failure` 기록
- 구조장애: 기존 fixture의 `search.ax` 이상 응답 → `main.do` 1회 → 동일 `search.ax` 1회 순서와 반복 시 확정 유지
- 정상 0건: `HEALTHY`, 폴백 경고 없음, 페이지 계약 경고 없음, 완전성 무강등 유지
- deadline 제한 timeout은 기존과 같이 채널 실패횟수에 포함되지 않음
- 이번 검증에서 라이브 네트워크 요청과 90초 성능 통과 주장은 하지 않음

## 단계 1.1 핵심 검색 신뢰성 보강 검증

- 검증일: 2026-07-17 KST
- 기준점: `v0.1-core-reviewed` (`98db81e`) + `DEVELOPMENT_PLAN.md` v20
- Python: 3.14.6

### 자동 테스트

`python -m unittest discover -s tests -p "test*.py"`

- 총 100개 통과, 실패 0개
- 신규 단계 1.1 fixture: 새 세션 최초 모드설정, 동일 모드 검색어 전환, 모드 변경, reset, Cookie jar 재생성, DART 구조진단 순서, Fast 예산 경계 진단, 폴백 경고·등급, 정상 0건, 감사 최소화, 접수번호 비병합
- deadline: 공통 HTTP timeout 절단, DART 상태진단·모드설정·결과요청, OpenDART 목록·원문 전파, 요청 전 중단, 재시도/백오프 전후 중단, deadline timeout 회로 비누적, partial·continuation 응답 통과
- `python -m app.evaluation`: 24/24 통과
- `python -m compileall -q app tests`: 통과
- `git diff --check`: 통과

### 세션 생명주기 라이브 프로브

- 실제 요청: 7 / 20
- 동시성: 1
- 최소 요청 시작간격: 1,000.130ms
- HTTP 재시도: 0
- TLS 인증서 체인·호스트명 검증: 활성
- API 키: 미사용
- Cookie 이름·값 fixture: 저장 0건
- 새 세션, 같은 세션 검색어 전환, Cookie jar 재생성 직후 직접 검색, 재설정 후 검색: 모두 HTTP 200
- Cookie jar 재생성 직후 직접 검색과 모드설정 후 검색: 결과 15건·10행, 응답 SHA-256 동일
- 서버 실제 만료 신호: `unconfirmed`
- Cookie 교체 고유 실패 신호: `unconfirmed`
- 자동 만료 감지: 미구현, 명시적 reset만 유지

### 성능·범위 주의

- 고정 평가 벤치마크: plan builder p95 0.0085ms, fixture parser p95 2.4573ms
- 네트워크 90초 종단간 성능 게이트는 실행하거나 통과를 주장하지 않았다.
- OpenDART 목록 동시성 2, 원문 동시성 3, 적응형 감속, 정정 체인·diff·사건 연결, KIND, 배치, TTL 디스크 기본 활성화는 테스트 대상 실행기에 연결하지 않았다.

## REVIEW_REPORT 해소 회귀검증 (2026-07-17 KST)

- 기준: `DEVELOPMENT_PLAN.md` v19, 코드, 단계 0·0.5·0.6 fixture. 추가 라이브 네트워크 요청은 수행하지 않았다.
- 신규 회귀: hard timeout 부분결과/continuation, `fully_pageable=false` 조기종료, form 15·실효 10 분리, 모드 변경 재설정, 세션 상태진단 재사용, 200+마커부재 구조장애 분류, 구조 재시도 통계, `채` 시장분리·조합 신뢰도, 감사 재현필드, HTTP 429 지수 백오프, POSIX manifest 경로, Python별 TLS strict 기본값.
- 고정 평가세트는 24개를 유지하고 구조장애 질의를 기존 synthetic fixture에 연결해 fixture 연결 질의를 20개로 강화했다.
- 단계 0·0.5·0.6 fixture 내용은 변경하지 않았다.
- `python -m pytest tests -q`: 80 passed, 10 subtests passed, 실패 0개.
- `python -m app.evaluation`: 24/24 통과. 계획 생성 p95 0.0186ms, DART fixture 파서 p95 4.4143ms, 캐시 40문서·7,680,000바이트, peak 5,447,992바이트.
- `python -m compileall -q app tests`: 통과.
- `git diff --check`(단계 0.6 raw 원문 제외): 통과.

## 단계 1 라이브 인수검증 (2026-07-17 KST)

- Python 3.14 TLS 호환 설정 후 인증서 검증 `CERT_REQUIRED`, 호스트명 검증 활성 상태에서 DART `HEALTHY`와 OpenDART 정상 응답을 확인했다.
- 전체 자동 테스트: 74개 통과, 실패 0개.
- 삼성전자 주요사항보고서(2025-01-01~2025-12-31): OpenDART 유형 `B` 전기간 집계 8건, 목표 결과 5건. 5건 모두 회사명·보고서명·원문 근거 확인, DART 링크 HTTP 200 및 삼성전자 표식 확인.
- 검색기간 누락: `clarification_required`, `DATE_RANGE_REQUIRED`, plan/diagnostics 없음으로 네트워크 전 중단 확인.
- 정상 0건: 2025-01-01 하루 삼성전자 `공개매수 대상회사 의견표명서` 검색 결과 0건, status `completed`, 채널 `HEALTHY`, coverage complete, OpenDART 측정 0건.
- 잘못된 API 키: 실제 `.env` 키 한 글자 변경 시 status `api_key_action_required`, `OPENDART_KEY_UNREGISTERED`, DART 상태 `010`. 응답·로그 키 노출 0건. 원래 키 복원 후 정상 `013` 재확인.
- 상계납입(2025-07-16~2026-07-16): 실제 결과 5건, 고유 접수번호 5건, 중복 0건, 전부 OpenDART 원문에서 `상계납입` 확인. 실행 검색어는 붙여쓴 `상계납입` 1종이고 `상계 납입` 미실행. DART 요청 간격 최솟값 1.000초, `latest_first_bias=true`, partial coverage 표시.
- 확인된 상계납입 접수번호: `20260708000160`, `20260616000322`, `20260612000426`, `20260601000409`, `20260601000332`.

## 단계 1 핵심 공시검색 MCP 검증

- 검증일: 2026-07-17 KST
- 기준: `DEVELOPMENT_PLAN.md` v19 및 완료된 단계 0·0.5·0.6 fixture
- 단계 1 추가 실측 네트워크 요청: 0건
- Python: 3.14.6

### 자동 테스트

`python -m unittest discover -s tests -p "test*.py"`

- 총 72개 통과, 실패 0개
- 계약·설정·복구·strict TLS Cookie 세션·감사로그 마스킹·continuation·프롬프트 인젝션: 통과
- 안전 ZIP 경로이탈·고압축률, XML DOCTYPE/ENTITY 차단: 통과
- CORPCODE 실제 fixture 10만 건 이상 파싱, 회사명·종목코드 조회, 3개월 포함경계 창: 통과
- OpenDART `000·013·010·011·012·014·020·021·100·101·800·900·901`, 900 1회 제한 재시도, 020 무재시도: 통과
- DART 실제 HTML 42건/10행, 구조 필드, 본문·첨부 중복제거, 동일 모드 검색어 전환, 정상 0건: 통과
- 구조 상태진단 후 15분 차단, 네트워크 3분 차단, open 상태 무호출: 통과
- 검색기간 무지정 무호출, 전역 접수번호 중복제거 후 원문예산, 근거·링크, continuation, 정상 0건/장애 분리: 통과
- MCP `tools/list`, `search_disclosure_cases`, `get_disclosure_evidence` 호출: 통과
- settings/rules/schema/계획서 상수 드리프트 및 `verify=False`·브라우저 UA·`maxResultsCb` 의존 금지: 통과
- `python -m compileall -q app tests`: 통과

### 고정 평가질의

`python -m app.evaluation`

- 24개 중 24개 통과, 실패 0개
- 평가세트: `tests/golden_cases/stage1/evaluation_queries.json`
- 포함 범주: 특정회사 목록/본문, 정밀질의 2종, 출자전환, 정상 0건, 잘못된 키, 철회 후보·명시 후속, 정정 체인, 공개매수 대상회사, 본문·첨부 중복, 날짜창 경계, 독립사건 오병합 방지, rm `채`·순서·미지문자, 복수 접두어, 페이지 크기, 모드 재사용, 014·020, broad 제한, 두 회로차단.

### 성능·메모리

- 반복 수: 계획 생성 1,000회, 실제 DART fixture 파싱 100회
- `SearchPlan` 생성 p95: 0.0144ms (목표 50ms 이하)
- DART 결과 HTML 파싱 p95: 2.2860ms
- 세션 캐시: 45회 입력 후 40문서 유지, 파싱 텍스트 7,680,000바이트
- 캐시 벤치마크 `tracemalloc`: current 5,127,859바이트, peak 5,447,992바이트
- 계약 상한: 40문서 또는 파싱 텍스트 64MB 중 먼저 도달; 문서 수·바이트 양쪽 퇴출 테스트 통과
- 네트워크 p50/p95와 상주 RSS는 라이브 성능시험을 다시 수행하지 말라는 지시에 따라 이번 단계에서 측정하지 않았다.

### 보수 유지·미확인

- 실제 DART 구조장애 발생률과 자동화 허용빈도는 여전히 미확인이다.
- `rm=철`은 provisional 후보 신호이며 명시 연결 근거 없이는 사건을 합치지 않는다.
- `rm=채` 의미는 confirmed지만 `채` 포함 실제 조합문자의 순서·다른 플래그 보존 실측은 unconfirmed다.
- TTL 디스크 캐시는 단계 1에서 구현·기본 활성화하지 않았고 `settings.json`에서 `false`다.
- 네트워크 종단간 성능, 정정 구조 diff, 복수 사건 연결, KIND, 승인형 배치는 단계 1 Fast Path 범위 밖이다.

## 단계 0.6 검증

- 검증일: 2026-07-17 KST
- 최종 run_id: `stage0_6_20260716T163708Z`
- 실제 네트워크 요청: 59 / 60
- 실행시간: 58.753초, 동시성 1, 요청 시작 간격 최솟값 1,000ms
- TLS 인증서 검증 활성화, `verify=False` 미사용
- API 키 URL 29건 모두 `***MASKED***`; Cookie/Authorization 헤더 기록 0건
- 판정: query switch `passed`, page size `failed`, date window `passed`, rm `철` `partially_passed`, rm `채` `partially_passed`
- raw/golden manifest의 SHA-256 검증을 회귀 테스트에 포함했다.
- `python -m compileall -q app tests`: 통과
- `python -m unittest tests.test_probe -v`: 16개 테스트 통과
- `git diff --check -- . ':(exclude)tests/fixtures/probe/stage0_6/raw/**'`: 통과. 외부 원문 그대로인 raw HTML의 공백은 변형하지 않음.
- `stage_1_started=false`, `main_development_started=false`

## 단계 0.5 검증

- 검증일: 2026-07-17 KST
- 기준 run_id: `stage0_5_20260716T150948Z`
- Python: 3.14.6
- 네트워크 요청: 87 / 120
- 실행시간: 약 104초
- 동시성: 1
- DART/OpenDART 요청 시작 간격: 최소 1초 설정

### 자동 테스트

`python -m unittest tests.test_probe -v`

- 13개 테스트 통과
- 기존 단계 0 파서·마스킹·N/Y·golden manifest 회귀 7개 통과
- 단계 0.5 플래그십 문구·페이지 중복·정정 층화·rm provisional·D004 역할·golden hash 회귀 6개 통과

### 정적 검증

- `python -m compileall -q app tests`: 통과
- `git diff --check`: 통과
- raw 및 golden fixture 내 제공된 OpenDART API 키 원문 검색: 0건
- 요청 로그의 API 키: `***MASKED***`
- 요청 로그의 Cookie/Authorization 헤더: 기록하지 않음
- 최종 raw fixture: 103개 파일, 3,230,653바이트
- golden fixture: 게이트별 JSON 5개, 통합 findings 1개, hash manifest 1개

### 중단·상한 검증

- 첫 시도는 회사코드 없는 3개월 초과 검색에 대한 OpenDART `status=100`으로 중단됐고 중단 manifest가 보존됐다.
- 후속 완료 run은 각각 `stop_reason=completed_stage0_5_only`를 기록했다.
- 최종 run은 요청 상한 120회 중 87회를 사용했으며 자식 프로세스를 생성하지 않았다.
- 단계 1 본개발은 시작하지 않았다.
