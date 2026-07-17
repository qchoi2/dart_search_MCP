# 개발 진행 현황

## 단계 7·8 배포 패키징 및 영구 인덱스 게이트 완료 (2026-07-18)

- 사용자 표시 문구를 “공시 MCP의 속도우선 기능”과 “공시 MCP의 심화 검색기능”으로 정리했다. 속도우선 기능에서 범위를 모두 확인하지 못하면 심화 검색기능으로 범위와 예상시간을 먼저 확인하도록 안내하고, 심화 검색기능 설명을 물어볼 수 있다는 문구를 포함한다.
- Claude Desktop 배포 방식을 MCPB 패키지로 확정했다. `installer.build_release`는 allow-list 기반으로 `app/`, `settings.json`, 아이콘, `server.py`, `pyproject.toml`, `manifest.json`만 묶고 `_local_data`, `.env`, 로그, 테스트 fixture, git 메타데이터는 제외한다.
- MCPB manifest는 v0.4, `server.type=uv`, `user_config.dart_api_key.sensitive=true`, Windows 플랫폼, 6개 MCP 도구 설명을 포함한다. OpenDART API 키는 배포물·감사로그·fixture에 저장하지 않는다.
- 사용자용 `사용설명서.html`과 앱 아이콘 PNG/ICO를 추가했다. 아이콘은 투명 PNG와 Windows ICO 7개 크기(16~256)를 포함한다.
- 기존 수동 Claude 설정 병합은 개발용 진단·복구 경로로만 유지한다. MSIX Claude 설정 경로 탐색, 기존 설정 보존, 백업, 원자적 쓰기, idempotent 등록·해제를 fixture로 검증했다.
- 단계 8 영구 인덱스는 기본 비활성으로 닫았다. 반복 수요와 실측 recall/cost 개선이 모두 확인될 때만 별도 승인 대상으로 전환하며 자동 활성화하지 않는다.
- 공식 MCPB CLI로 `info`와 manifest schema validation을 통과했다. 패키지는 아직 서명하지 않았으므로 공식 도구가 `Not signed` 경고를 표시한다.
- 전체 자동시험 `164 passed, 20 subtests passed`, 고정 평가 `24/24`, `compileall`을 통과했다.

## 검색결과 원문 링크 의무계약 (2026-07-18)

- 확정 사례마다 대표 `original_document_url`, 클릭용 `original_document_link`, 모든 구성 공시의 `original_document_links`를 반환한다.
- 예비 후보와 배치 결과에도 `original_document_url`을 추가하고 기존 URL 필드는 호환 목적으로 유지한다.
- 정정 체인은 원공시·중간정정·최종정정 링크를 모두 보존하며 대표 링크는 유효본 또는 마지막 공시를 우선한다.
- MCP 도구 설명과 응답 `source_link_policy`에 각 사용자 표시 결과마다 원문 링크를 함께 보여야 한다는 지시를 추가했다.
- 자동시험 154개와 20 subtests를 통과했다.

## 단계 6 온디맨드 정정·사건 연결 및 회귀시험 (2026-07-17)

- `amendment_comparison=true`는 S6, 최종 유효본·철회 질의는 S7, `sequence_required=true`는 S5 온디맨드 경로로 라우팅한다. 일반 Fast Path는 계속 접수번호 단위이며 관계 엔진을 호출하지 않는다.
- 정정 원문의 명시 원접수번호 또는 유일하게 대응되는 명시 최초제출일만 confirmed 연결 근거로 사용한다. 회사·정규화 보고서명·근접일만 같은 후보는 병합하지 않고 `uncertain` singleton으로 남긴다.
- 정정표의 행·셀을 우선 파싱해 날짜·수치·문구 변경방향을 구조화한다. 표가 없을 때만 알려진 필드 정렬 diff를 사용한다. XML DOCTYPE은 계속 차단하고 안전 파서가 거부한 문서는 엔터티를 해석하지 않는 HTML fallback에서 표 경계만 보존한다.
- 명시 체인으로 연결된 정정본은 하나의 case로 묶어 결과 20건 상한을 잠식하지 않는다. 최종 문서의 `rm`에 `정·철`이 없고 체인이 완전한 경우에만 잠정 유효본을 제시하며, 명시 연결된 최종 `철`만 철회 confirmed로 처리한다.
- 사건 그래프는 공개매수→주식교환의 날짜순서와 원문 당사자 교집합이 있을 때만 confirmed edge를 만든다. 회사명만 같은 선후관계는 uncertain이다.
- 전체 자동시험 154개, 고정평가 24/24, 단계 0.5 정답 20접수의 체인 정확도 20/20을 통과했다. 실제 유상증자·합병·전환사채 최종 정정본 3건에서 최초제출일·정정표를 3/3 인식했다.
- ADS·원주, 공개매수→주식교환 10건, 상계납입 10건 이상의 새 전수 라이브 인수시험과 장기 90초 p95는 아직 완료로 주장하지 않는다.

## 단계 5 캐시·OpenDART 동시성 성능시험 (2026-07-17)

- 제한 라이브 16요청에서 목록 순차 0.783초→동시성 2에서 0.398초(49.1% 단축), 원문 6건 순차 1.622초→동시성 3에서 0.595초(63.3% 단축), 오류 0건을 확인했다.
- OpenDART 목록 2·원문 3을 실행기에 연결했다. 원문 HTTP 429·일시 timeout·5xx는 3→2→1로 감속하고 OpenDART 020은 감속·재시도 없이 즉시 중단한다. DART 웹은 동시성 1·최소 1,000ms를 유지한다.
- A 세션, B 무압축 TTL, C gzip1 TTL, D 없음 비교에서 C는 최초검색 증가 1.703%, 쓰기 p95 6.737ms로 기준 5%·100ms를 통과했다. 무압축보다 디스크가 약 60% 작아 gzip1·24시간·500MB 상한을 기본 활성화했다.
- TTL 캐시는 접수번호·정규화 텍스트·checksum만 저장하며 질의·API 키·Cookie·raw ZIP을 저장하지 않는다. 손상·만료 파일은 삭제 후 cache miss로 복구하고 용량 초과는 LRU로 정리한다.
- 상세 측정은 `STAGE5_PERFORMANCE_RESULTS.md`, 단계 6 검증은 `STAGE6_VALIDATION_RESULTS.md`에 기록했다.

## 단계 2~4 검토의견 반영 및 v21 정합화 (2026-07-17)

- CSV 셀의 외부 공시값이 `=·+·-·@·TAB·CR`로 시작하면 작은따옴표를 붙이는 공통 guard를 추가했다. JSON 결과에는 원문을 유지하고 evidence별 위험 여부와 export 안전정책을 기록한다.
- 승인 배치 실행은 DART의 검증된 90일 비중첩 날짜창과 페이지를 체크포인트 가능한 후보발굴 경로로 사용한다. DART 장애·회로 open 때만 기존 OpenDART 목록 전수순회로 폴백하고 최종 원문은 계속 OpenDART로 검증한다.
- 대화형 결과에 필터 후 예상 문서 수·예상시간 기반 `batch_research_recommended`와 미리보기 도구명을 추가했다. 같은 검색계보의 권고는 30분간 한 번만 표시하며 추정 작업량이 50% 이상 늘 때만 재표시한다.
- 배치 preview에 `dart_rate_floor_seconds`, 실행·재개 응답에 `blocked_until·blocked_seconds`를 추가했다. 완료 시 원 질의 평문 없이 해시·실행 변형·범위·접수번호·호출진단 요약 1줄을 기존 감사 로그에 기록한다.
- v20 개정요약 6·100에 따라 단계 5 성능시험 전 원문 운영 동시성은 1로 복원했다. 동시성 3과 3→2→1 감속은 예약 상수·단계 5 범위로 유지한다.
- 계획서를 v21로 갱신해 단계 1.1~4 완료상태, 접수번호 단위 묶기, 실제 도구명 `export_search_results`, CSV 방어와 DART 배치 경로를 정본에 반영했다.
- hash manifest 대상 fixture·프로브 파일은 `.gitattributes -text`로 플랫폼 간 바이트를 고정했다.

## 단계 4 승인형 배치 리서치 완료 (2026-07-17)

- 단계 3 커밋 `21b92f5`를 기준으로 v20 단계 4를 구현했다. 공개 MCP 도구는 `preview_batch_research`, `run_batch_research`, `continue_batch_research`, `export_search_results` 네 개를 추가했다.
- 미리보기는 3개월 날짜창의 첫 목록 페이지와 사용 가능한 DART 첫 결과 페이지로 요청 수·DART 검색건수·중복제거 수율·예상 고유문서·실행시간·저장량을 추정한다. 원문은 다운로드하지 않으며 `plan_id`, 5/10/15/30분 선택지, 권장 10분, CSV/JSON 가능 형식, 체크포인트·보존정책을 반환한다.
- 배치는 유효한 메모리 전용 `plan_id`, `approved=true`, 허용된 실행시간이 모두 있어야 시작한다. 거절·만료 plan·잘못된 시간구간은 네트워크 0회이며, 같은 계보·범위의 미리보기는 기존 plan을 무호출 재사용한다. 거절 후 30분간 재권고를 억제하고 범위·예상작업이 50% 이상 늘 때만 다시 허용한다.
- 승인 실행은 `soft=max(30초, 선택구간-30초)`, `hard=선택구간`을 사용한다. 날짜창·공시유형·페이지·페이지 내 행 위치·처리 접수번호·DART 회로상태를 원자적 체크포인트에 저장하고, 구간 종료 시 `continuation_confirmation_required`로 중단한다. 재개도 새 승인과 시간구간이 필요하다.
- 만료된 open 회로가 체크포인트에서 복구되면 wall clock을 다시 평가해 `PROBING`으로 전환하고 DART 상태진단을 정확히 한 번 수행한다. 성공은 `HEALTHY`, 실패는 재차단 상태로 저장한다.
- v21 대조에서 v20 개정요약 6·100의 단계 5 이월 계약을 우선해 배치 원문 다운로드의 운영 동시성도 1로 정정했다. 동시성 3과 적응형 감속은 단계 5 성능시험 전까지 활성화하지 않는다.
- 체크포인트는 7일, 결과 메타데이터·근거 레코드는 24시간 보존한다. 원문 전체는 디스크에 저장하지 않고, 체크포인트·결과 레코드에도 원 질의 평문 대신 정규화 해시와 실제 실행 변형만 남긴다. 완료 체크포인트는 즉시 삭제되고 만료 자료는 다음 배치 동작에서 정리된다.
- 결과 파일은 검색 완료 후에도 자동 생성하지 않는다. `export_search_results`에 사용자가 `output_directory`와 CSV/JSON 형식을 명시한 경우에만 원자적으로 저장한다.
- 단계 4 fixture·manifest와 전용 회귀 17개를 포함한 전체 자동 테스트 139개가 통과했다. 고정 평가 24/24, compileall, diff check도 통과했으며 라이브 네트워크·90초 성능 통과 주장은 하지 않는다.
- 제외 범위는 유지했다: 정정 체인 추정·구조 diff·사건 연결(S6·S7), KIND, 영구 인덱스, MCP Tasks, TTL 디스크 캐시 기본 활성화, 동시성 4 승격.

## 단계 3 대화형 MCP 완료 (2026-07-17)

- v20의 다음 순서인 단계 3을 `454e405` 기준으로 대조했다. `search_disclosure_cases`와 `get_disclosure_evidence`, 기본 검색기간 확인, 세션 캐시, 결과·원문 예산은 단계 1에서 선행 구현돼 있었고 MCP 경계·continuation·구조 경고의 잔여 계약을 보강했다.
- `search_disclosure_cases` 도구 스키마를 실제 `SearchRequest`와 일치시켜 `cache_mode`, `amendment_comparison`, `sequence_required`, `output_mode`, `schema_version`을 포함했다. 런타임에서도 bool/int 혼동, 날짜 형식, 선택 힌트 타입, 스키마 버전과 비활성 TTL 캐시 모드를 검증한다.
- 대화형 도구는 최종 20건·원문 확인 40건 상한을 유지한다. `output_mode=batch`, `exhaustive=true`는 네트워크 전에 `batch_confirmation_required`, 정정비교·사건순서 연결은 `INTERACTIVE_SCOPE_UNAVAILABLE`로 중단한다.
- continuation token은 30분 TTL의 프로세스 메모리 상태를 가리키며 최대 1,000개로 제한한다. 검색계보·기간·페이지·처리 접수번호 해시·실행 검색어 변형을 저장하고, 다른 검색계보에서 잘못 제시해도 원래 토큰을 소실하지 않는다.
- 예산소진 continuation은 `SEARCH_BUDGET_PARTIAL`, DART 최신순 일부 범위는 `LATEST_FIRST_BIAS` 구조 경고로 반환한다. 검색 미실행 상태는 `coverage.complete=false`, `completeness_grade=unconfirmed`로 표시한다.
- `get_disclosure_evidence`는 키워드 1~20개를 검증하고 최대 8개·각 500자 근거만 반환한다. 전체 원문 미반환을 유지하고 `status`, `schema_version`, `evidence_count`, `source_text_untrusted`를 추가했다.
- 결과는 기계적 사실과 `legal_assessment`를 분리하고 접수번호 단위 case를 최대 20개 반환한다. 서로 다른 접수번호를 같은 사건으로 추정 병합하지 않으며 명시 사건 연결·정정 diff는 S6·S7 범위로 유지한다.
- 단계 3 전용 회귀 11개를 포함한 전체 자동 테스트 120개와 고정 평가 24/24가 통과했다. 라이브 네트워크 요청은 추가하지 않았다.
- 다음 계획 단계는 단계 4 승인형 배치 리서치지만 이번 단계에서는 구현하거나 자동 실행하지 않았다.

## 단계 2 DART 어댑터·폴백 잔여 계약 보강 완료 (2026-07-17)

- `DEVELOPMENT_PLAN.md` v20의 단계 2 완료기준을 `v0.1.1-reliability` 코드와 다시 대조했다. 본문검색·상태진단·구조장애 재진단·OpenDART 폴백의 큰 흐름은 구현돼 있었고, HTTP 접근오류 분류, 회로 진단 필드, 페이지 계약 변화 감지가 잔여 항목이었다.
- DART에서 HTTP 408·425·429와 5xx·연결오류는 네트워크 장애로 분류한다. 완료된 검색 동작 기준 2회 연속 실패하면 3분 회로를 열고, 단발 실패도 즉시 OpenDART 폴백 대상으로 반환한다.
- HTTP 4xx 중 408·425·429를 제외한 명시적 거절은 구조·접근 장애로 분류해 즉시 15분 회로를 연다. 공통 HTTP 계층이 실제 상태코드를 보존하므로 추정 HTML이나 특정 Cookie 이름을 사용하지 않는다.
- 회로 진단에 `blocked_until`을 추가하고 기존 `blocked_until_epoch` 호환 필드를 유지했다. 차단 만료 후 상태진단은 `PROBING`에서 정확히 1회 수행하며 `probe_result=success|failure`를 감사 진단에 남긴다.
- DART 결과의 실측 10행 페이지 크기, `search_count` 기반 계산 페이지 수, 실제 `search(n)` 마지막 링크를 대조한다. 불일치 시 `PAGINATION_CONTRACT_CHANGED`, 관측값, `completeness_grade=unconfirmed`를 구조화해 반환한다. 정상 0건은 이 판정에서 제외한다.
- 단계 2 보강 회귀를 포함한 전체 자동 테스트 109개가 통과했다. 네트워크 요청은 추가하지 않았으며 기존 golden HTML fixture를 재사용했다.
- 후속 단계 범위인 정정 체인·구조 diff·사건 연결·KIND·배치·OpenDART/원문 동시성·적응형 감속·TTL 디스크 캐시 기본 활성화는 변경하지 않았다.

## 단계 1.1 핵심 검색 신뢰성 보강 완료 (2026-07-17)

- 기준점 `v0.1-core-reviewed`와 `DEVELOPMENT_PLAN.md` v20을 대조하고 단계 1.1 범위만 구현했다.
- Fast Path는 접수번호 전역 중복제거와 동일 접수번호 본문·첨부행 병합만 수행한다. 정정 접두어, `rm` 원문·플래그, 명시 관계 필드를 보존하며 서로 다른 접수번호의 사건 병합, 정정 체인 추정, 최종 유효본 판정, 구조 diff를 실행하지 않는다. Fast Path `effective_receipt_no`는 확정하지 않고 `null`로 둔다.
- 감사로그 기본 계약을 정규화 질의 해시, 실제 실행 변형, 기간·회사·공시유형 범위, 후보·검증 접수번호, 제외 사유, 호출·캐시·재시도 진단, 경고코드, 완전성 등급으로 축소했다. `audit.audit_query_text=on`일 때만 평문 질의 필드를 허용하며 기본값은 `off`다.
- DART 클라이언트에 명시적 `reset_session`과 Cookie jar 세대 추적을 추가했다. reset, jar 재생성, 프로세스 재시작, 모드 변경은 활성 모드와 상태진단 성공 캐시를 폐기하며 다음 검색에서 모드설정 POST를 1회 수행한다. 같은 세션·같은 모드의 검색어 전환은 재설정하지 않는다.
- SearchPlan의 단일 hard deadline을 회사코드 조회, OpenDART 목록, DART 상태진단·모드설정·결과요청, 원문 다운로드, 애플리케이션 재시도, HTTP 재시도·백오프·timeout까지 전달했다. timeout은 `min(HTTP_TIMEOUT_SECONDS, remaining)`이고 새 요청 전·DART 간격 대기 전후·HTTP 백오프 전후에 잔여시간을 확인한다.
- deadline 종료는 `partial`, `SEARCH_TIMEOUT_PARTIAL`, 처리·미처리 범위와 continuation token으로 반환한다. `deadline_limited_timeout`은 DART 회로의 네트워크 실패횟수에 포함하지 않는다.
- DART 이상 응답은 첫 회에 `structure_failure_candidate`로만 두고 `main.do` 상태진단 1회 후 동일 `search.ax` 1회 재시도에서 반복될 때만 구조장애로 확정한다. 정상 0건은 `searchCnt=0` 또는 결과 테이블 내부의 고정 마커만 인정하며 회로·강등에 포함하지 않는다.
- 폴백 응답에 `DART_FULLTEXT_FALLBACK`, reason, fallback_source, 정수 blocked_seconds, completeness_grade, 실제 원문 확인 수, 미처리 후보 수를 추가했다. 단발 폴백은 0초, open 회로는 양수이며 폴백 0건도 최소 `reduced`, 정상 무폴백 0건은 `complete`를 유지한다.
- 규칙파일은 최초 로드 시 schema version, 필수 키, 수치 형식, 근거 5필드와 소수표본 provisional 조건을 즉시 검증한다.
- 세션 제한 프로브는 7/20요청, 동시성 1, 최소 시작간격 1,000.130ms, 재시도 0회, TLS 검증 유지로 완료했다. Cookie jar 재생성 직후 직접 검색과 모드설정 후 검색이 모두 정상이고 응답 해시가 같아 서버 만료·Cookie 교체의 고유 실패 신호는 `unconfirmed`다. 자동감지는 추가하지 않았다.
- fixture 회귀 100개, 고정 평가 24/24, compileall, diff check가 통과했다. 라이브 90초 성능 통과 주장은 하지 않는다.
- 제외 범위는 그대로다: OpenDART 목록 동시성 2, 원문 동시성 3, 적응형 감속, S6·S7 정정/사건 연결, `rm=채` 조합 확정, KIND, 배치, TTL 디스크 캐시 기본 활성화, 대화형 MCP 도구 확장.

## REVIEW_REPORT 대조·최소 수정 (2026-07-17)

- F1~F17과 P1~P5를 v19, 현재 코드, 단계 0·0.5·0.6 fixture에 대조하고 `REVIEW_RESOLUTION.md`에 분류했다.
- 시간예산 hard 중단과 continuation, 질의별 `fully_pageable` 선판단, 실측 form `maxResults=15`/실효 페이지 10 분리, 상태진단 세션 재사용과 장애유형 분리를 구현했다.
- 구조 재시도를 실제 상태진단과 분리 계상하고 확정 장애 1건을 명시적으로 15분 차단하도록 통계를 바로잡았다.
- `채`를 시장소관에서 제외하고 실제 조합 미확인 상태를 후보 필드에 보존했다. 규칙 근거 5필드와 소수표본 provisional CI를 추가했다.
- 감사로그에 검색 변형·예비 후보·제외 사유·재시도·완전성을 추가하되 원 질의 평문은 저장하지 않는다.
- 모드 변경, 429 백오프, 시간예산, 감사필드, 페이지 완주판정, 교차 플랫폼 경로/TLS 회귀 테스트를 추가했다.
- Fast Path에서 금지된 사건 연결, 단계 2 구조 경고, 라이브 동시성 스케줄러는 구현하지 않았다. 서버 세션 만료 형태는 fixture 부재로 추가 재현 대상으로 남겼다.

## 단계 1 라이브 인수검증 보완 (2026-07-17)

- Python 3.14의 `VERIFY_X509_STRICT`가 DART 인증서 체인의 legacy CA BasicConstraints와 충돌하는 문제를 공통 HTTP 클라이언트에서 해결했다. 인증서 필수 검증과 호스트명 검증은 유지하며, 단계 0·0.6 실측 클라이언트와 같은 strict-flag 호환 설정만 적용한다.
- 회사명 검색은 CORPCODE로 먼저 8자리 회사코드를 확정하고 DART의 `textCrpCik`·`b_textCrpCik`에 전달하도록 수정했다. 회사명 문자열만 전달되어 다른 회사가 섞이던 실제 결함을 제거했다.
- 명시적인 `주요사항보고서` 요청은 구조화된 보고서명 접두어로 결과보고서 오탐을 제거하고, OpenDART 회사코드 + 공시유형 `B` 목록으로 전기간을 보강한다.
- 2025년 삼성전자 주요사항보고서는 OpenDART 3개월 창 전수 집계에서 8건으로 측정했고, 목표 5건 모두 삼성전자·주요사항보고서·원문 근거·정상 DART 링크임을 확인했다.
- 기간 누락은 네트워크 전에 `clarification_required/DATE_RANGE_REQUIRED`, 희귀 유형 1일 검색은 결과 0건·채널 `HEALTHY`·coverage complete로 확인했다.
- 실제 키 한 글자 변경 시험은 `010/OPENDART_KEY_UNREGISTERED`로 처리됐고 응답·감사로그에 키가 남지 않았다. 시험 후 원래 키를 복원해 정상 `013` 응답을 재확인했다.
- 상계납입 5건은 실제 DART/OpenDART 원문으로 확인했다. 실행 검색어는 `상계납입`만 사용됐고 `상계 납입`은 실행되지 않았으며, 접수번호 중복 0건·요청 시작간격 최소 1.000초·`latest_first_bias=true`가 기록됐다.
- 최신 자동 테스트는 74개 통과, 실패 0개다.

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
