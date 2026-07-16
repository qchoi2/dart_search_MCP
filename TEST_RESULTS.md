# 테스트 결과

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
