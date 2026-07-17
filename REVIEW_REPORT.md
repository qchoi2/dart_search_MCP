# REVIEW_REPORT — DEVELOPMENT_PLAN.md v19 · 단계 0/0.5/0.6 fixture · v0.1-core 독립 검토

- 검토일: 2026-07-17
- 검토 대상: 작업트리 HEAD `c96e2c8`(Harden live disclosure search validation). 태그 `v0.1-core`는 그 직전 커밋 `486b8a6`을 가리키며, 두 커밋의 차이는 `dart_fulltext.py`·`http_client.py`·`engine.py` 소규모 보강과 테스트 추가다. 본 보고서는 HEAD 기준이다.
- 검토 방법: 계획서 v19 전문(요약 1~92, §2·3·4·11·17·21·22), 단계 0/0.5/0.6 golden·raw fixture, `app/` 전 모듈, `tests/` 전체를 코드 리딩 + 실제 실행으로 확인했다. 코드는 수정하지 않았다.
- 테스트 실행 결과(검토 환경 Linux/Python 3.10): `74개 중 72 passed, 2 failed`. 실패 2건은 모두 환경 의존(→ F14)이며 Windows/Python 3.14 대상 환경에서는 통과할 것으로 판단된다. `python3 -m app.evaluation`: 24/24 통과.
- 원칙: 코드·fixture·테스트로 확인한 사실만 확정 지적으로 기재했다. 확인 불가한 부분은 "관찰/미확인"으로 명시했다.

---

## 1. 총평

단계 0.6 실측 계약의 핵심 3건(검색어 전환 시 모드설정 생략, `effective_page_size=10` 고정, 포함 경계 비중첩 날짜창)은 코드와 회귀 테스트에 정확히 반영되어 있다. OpenDART 상태코드 계약(000/013/020/800/900), 3분·15분 이원 회로차단과 OpenDART 폴백, SearchPlan 불변성, 비밀정보 로그 비노출, Fast Path 경량성도 구현·검증되어 있다.

주요 결함은 대부분 "계약을 어겼다"기보다 "계획서가 단계 1 산출물로 명시한 항목이 부분 구현"인 유형이다: 시간예산 미집행(F1), 세션 초기화 시 모드 재설정 경로 부재(F2), 사건/정정 체인 단위 묶음 부재(F3), 동시성·적응형 감속 부재(F4), 규칙파일 스키마 필드·CI 부재(F5), 감사 로그 항목 누락(F6).

---

## 2. 구현 지적사항

### F1. 소프트/하드 시간예산이 전혀 집행되지 않음

- 심각도: Medium
- 분류: 구현 미비 (계획 §4.3, §13, SearchPlan 계약)
- 파일/함수: `app/orchestrator/engine.py` `SearchEngine.execute`
- 재현: `execute()` 본문 전체에서 `plan.soft_timeout_seconds` / `plan.hard_timeout_seconds` 참조를 검색 — 사용처 없음. `clock`은 `first_candidate_elapsed_ms`·`completed_elapsed_ms` 계측에만 쓰인다. 느린 `download_document`를 주입해도 40건 예산 소진까지 무조건 진행된다.
- 현재 동작: 표준 검색이 90초 하드예산을 초과해도 중단·부분결과 전환이 없다. 시간 초과 시 `SEARCH_TIMEOUT_PARTIAL` 오류코드는 정의만 되어 있고 발생 경로가 없다.
- 기대 동작: 소프트 예산 도달 시 충분성 평가 후 조기 종료 가능, 하드 예산 도달 시 부분 결과 + continuation token 반환(§4.3).
- 최소 수정방향: 검증 루프의 후보별 반복 시작점에서 `self.clock()-start`를 soft/hard와 비교해, hard 초과 시 잔여 후보를 preliminary로 돌리고 `pending` 경로(기존 continuation 발급 로직)로 합류시킨다.

### F2. 세션 초기화·만료 시 검색모드 재설정 경로가 없음

- 심각도: Medium
- 분류: 구현 편차 (계획 요약 69, §4.3 `mode_setup_requests` 정의, §21.12-11)
- 파일/함수: `app/channels/dart_fulltext.py` `DartFulltextClient.search_page`, `health_check`
- 재현: `_active_mode`는 `search_page`에서 모드가 바뀔 때만 갱신되고, 그 외 어떤 경로에서도 리셋되지 않는다. `HttpClient`의 쿠키 jar 갱신(서버가 새 세션 쿠키를 내려주는 경우)이나 세션 만료를 감지하는 코드가 없다. 테스트도 "재설정 생략"만 검증하고(`test_mode_setup_is_not_repeated_for_keyword_switch`) 세션 변경 시 재설정은 검증하지 않는다.
- 현재 동작: 장수명 프로세스에서 서버측 세션이 만료·교체되어도 `_active_mode`가 유지되어 모드설정 POST 없이 `/search.ax`를 계속 호출한다. 계획 §4.3은 `mode_setup_requests`를 "최초 설정 + **세션 초기화**·검색모드 변경·폼 계약 변경에 따른 재설정 횟수의 합"으로 정의하나, 세션 초기화 재설정은 발생할 수 없는 구조다. (완화 요인: 결과가 비정상이면 `structure_failure_candidate` → 재시도 → 회로차단·폴백으로 수렴하므로 조용히 오염된 결과가 나올 가능성은 낮다. 실제 세션 만료 시 DART 응답 형태는 미실측이므로 이 완화는 추정이다.)
- 기대 동작: 세션 쿠키가 새로 발급되거나 상태진단에서 세션 부재가 확인되면 `_active_mode`를 리셋하고 다음 검색에서 모드설정 1회를 재수행.
- 최소 수정방향: `health_check`/`_paced_request`에서 응답의 `Set-Cookie`로 세션 식별자가 바뀐 것을 감지하면 `_active_mode=None` 처리. 최소한 `structure_failure_candidate` 1차 발생 시 재시도 전에 모드 재설정을 시도하는 분기 추가.

### F3. 정정본·동일 사건 단위 중복제거(사건 묶기) 미구현

- 심각도: Medium
- 분류: 구현 미비 (§11 단계 1 "정정본·동일 사건 중복제거의 기본 규칙 구현", §4.4)
- 파일/함수: `app/orchestrator/engine.py` `execute`, `_to_case`; `app/contracts.py` `DisclosureCandidate`
- 재현: 전 코드에서 `amendment_chain_id`·`event_id`·`original_receipt_no`는 생성 시점 이후 항상 `None`이다. `_to_case`는 접수번호당 1 사건을 만들고 `amendment_status`는 접두어 존재 시 `prefix_present_unlinked` 고정이다. 원공시+`[기재정정]` 2건을 후보로 넣으면 2개의 독립 결과가 반환된다(체인 연결·최종 유효본 1건 표시 없음).
- 현재 동작: 접수번호 전역 dedupe만 수행. 같은 사건의 원공시·정정공시 여러 건이 결과 20건을 각각 점유한다.
- 기대 동작: §4.4 — 명시적 원접수번호·정정사항표 기준으로 최종 유효본 1건을 사건 단위로 표시, 명시 근거 없으면 합치지 않고 `chain_confidence=uncertain`. `app/research/withdrawal.py`에 명시 근거 검증 프리미티브는 이미 있으나 정정 체인 쪽 대응물이 없다.
- 최소 수정방향: 단계 1 범위에서는 최소한 (a) 동일 회사·정규화 보고서명·접두어 관계의 후보를 `uncertain_cluster`로 표시하고 (b) 결과 잠식 진단 경고를 추가. 완전한 체인 연결은 S6/S7 구현 시점으로 미루되 그 결정을 DECISIONS.md에 기록.

### F4. 외부요청 동시성 2/3과 적응형 감속 미구현

- 심각도: Medium
- 분류: 구현 미비 (§11 단계 1 "외부요청 동시성 2/3 및 적응형 감속 구현", §4.2)
- 파일/함수: `app/orchestrator/engine.py` `execute`(원문 순차 다운로드), `app/channels/opendart.py` `collect_lists`(기간창 순차)
- 재현: 코드에 스레드/비동기 병렬 실행 경로가 없다. `LIST_CONCURRENCY=2`, `DOCUMENT_CONCURRENCY=3`은 defaults·settings에 존재하지만 참조하는 실행 코드가 없다(설정 검증 제외). 429/5xx 시 동시성 3→2→1 감속 로직도 없다(HTTP 재시도 백오프만 존재).
- 현재 동작: 목록·원문 모두 순차 1. 외부 부하 관점에서는 계획보다 보수적이므로 안전하나, 40건 원문 검증 시 성능목표(표준 90초) 달성이 불리하고 단계 1 명시 산출물이 빠졌다.
- 기대 동작: 목록 기간창 최대 2, 원문 다운로드 기본 3 병렬 + 오류 시 자동 감속.
- 최소 수정방향: 원문 다운로드에 `ThreadPoolExecutor(max_workers=DOCUMENT_CONCURRENCY)` 적용, 429/타임아웃 발생 시 worker 수를 낮추는 단순 카운터. 또는 "단계 1은 순차로 확정"을 DECISIONS.md에 기록하고 계획 §11을 정정.

### F5. 규칙파일 필수 스키마 필드·CI 검사 부재, `채` 조합 unconfirmed 마커 미표시

- 심각도: Medium
- 분류: 구현 미비 (요약 63, §21.9, §21.3)
- 파일/함수: `app/rules/amendment_rules.yaml`; `app/research/normalization.py` `parse_rm`; `tests/test_stage1_evaluation.py`
- 재현: `amendment_rules.yaml`에는 접두어 배열·rm 의미·confirmed/candidate 목록만 있고, 계획이 "규칙 스키마의 필수 필드"로 지정한 `evidence_fixture`·`sample_count`·`sample_scope`·`confidence`·`checked_at`이 전무하다. `sample_count<3 → confidence=provisional` 자동 강등과 "CI에서 누락을 실패시킨다"에 해당하는 테스트도 없다(`grep -rn sample_count app/rules tests/test_stage1*` → 없음). 또한 `채` 포함 조합 파싱 시 `combination_confidence=unconfirmed`를 표시하는 코드가 `app/` 어디에도 없다(§21.3 명시 계약; probe 산출물 `rm_bond.json`에만 존재).
- 현재 동작: 규칙의 근거·표본수·신뢰도가 규칙파일에서 소실되었고, `[연장결정]` 1건·`[정정명령부과]` 1건 등 provisional 의무 규칙이 다른 접두어와 구분되지 않는다. `parse_rm("채정")`은 신뢰도 표시 없이 정상 분해된다.
- 기대 동작: 규칙 항목별 evidence 필드 보존 + CI 검사, `채` 포함 조합에 unconfirmed 마커.
- 최소 수정방향: yaml에 접두어·rm 항목별 `sample_count/confidence/evidence_fixture/checked_at` 추가, `test_stage1_evaluation.py`에 스키마 필수필드·`sample_count<3→provisional` assert 추가. `parse_rm` 반환 또는 후보 필드에 `rm_combination_confidence`를 추가하고 `채` 포함 다문자 조합이면 `unconfirmed`.

### F6. 감사 로그가 §3.3 기본 기록 항목 다수를 누락

- 심각도: Medium
- 분류: 구현 미비 (§3.3, §11 단계 1 "감사 로그 스키마 구현")
- 파일/함수: `app/orchestrator/engine.py` `SearchEngine._audit`
- 재현: `_audit` 레코드 필드는 ts·lineage·질의해시·mode·기간·company·status·검증 접수번호·diagnostics·coverage뿐이다. §3.3의 "사용자의 원 질의"(해시만 있음), "사용한 검색어 변형"(`plan.query_variants` 미기록), "후보 접수번호"(검증본만 기록), "제외 사유 코드", "재시도 수", "결과의 완전성 등급"이 없다. 보존기간 30일 정리 로직도 없다(크기 50MB 절삭만 구현).
- 현재 동작: 재현성 목적(어떤 변형어로 무엇을 후보로 얻어 왜 제외했는가)을 감사 로그만으로 복원할 수 없다.
- 기대 동작: §3.3 기본 항목 기록. (참고: 원 질의 평문 기록은 P3의 계획서 내 긴장과 연결 — 해시만 기록한 현재가 의도적 결정이라면 문서화 필요.)
- 최소 수정방향: `_audit`에 `query_variants`, `preliminary_receipts`, `excluded` 사유(verification_status), `completeness`(coverage.complete 파생), diagnostics의 재시도 카운터를 추가. 원 질의 기록 여부는 결정 기록 후 계획서와 일치시킨다.

### F7. `fully_pageable` 사전계산·질의 정밀화·배치 권고 경로 부재

- 심각도: Low
- 분류: 구현 미비 (요약 70, §4.3 fully_pageable 공식, §21.12-14)
- 파일/함수: `app/channels/dart_fulltext.py` `search_variants`
- 재현: 첫 페이지의 `search_count`로 `ceil(search_count/10)`(=`estimated_pages`)은 계산하지만, 잔여 요청예산과 비교해 완주 불가 질의를 정밀화·조기종료·배치 권고로 분기하는 코드가 없다. 넓은 질의(예: 검색건수 50,864)에서도 예산 소진까지 페이지를 순회한 뒤 `latest_first_bias=True`만 남긴다.
- 현재 동작: 요청 수 자체는 예산으로 상한되므로 낭비는 제한적이나, "페이지 깊이를 늘리기 전에 먼저 판단"하는 계약과 다르고 배치 권고 신호가 생성되지 않는다.
- 기대 동작: 첫 페이지 후 `fully_pageable` 계산 → false면 해당 질의 추가 페이지 중단(상위 고유 후보 조기종료) 및 진단에 완주 불가 표시.
- 최소 수정방향: `search_variants` 페이지 루프 진입 전 `estimated_pages`와 잔여예산 비교 분기 1개 추가, 진단 필드(`fully_pageable_by_query`) 기록.

### F8. 실측되지 않은 `maxResults=10` 값을 전송

- 심각도: Low
- 분류: 구현 편차 (§21.12-9, 단계 0.6 `page_size.json`)
- 파일/함수: `app/channels/dart_fulltext.py` `DartFulltextClient._form`
- 재현: `_form`은 `maxResults=str(DART_EFFECTIVE_PAGE_SIZE)`= `"10"`을 전송한다. 실측 form 기본값은 15이고 허용값은 15·30·50·100이다(`page_size.json`, 프로브 `requests.jsonl`의 maxResults 분포: 15×21, 30·50×각2, 100×4 — 10은 0회).
- 현재 동작: 확대 파라미터에 의존하지 않는다는 계약(항목 5)은 충족하나, 서버가 15 미만 값을 어떻게 처리하는지는 미실측이다. 페이지 수 계산(`ceil(count/10)`)은 실측 서버 페이징(10행)과 일치하므로 현재 동작상 문제 징후는 없다.
- 기대 동작: 실측된 요청 계약(폼 기본값 15) 그대로 전송하고, 파싱·페이지 계산만 `effective_page_size=10`을 사용.
- 최소 수정방향: `_form`의 `maxResults`를 실측 기본 `"15"`(별도 상수 `DART_FORM_MAX_RESULTS`)로 분리. 관련 테스트(`test_form_fixed_page_size_and_inclusive_dates`)의 기대값 갱신.

### F9. 구조장애 확정 절차가 "상태진단 재시도"가 아니라 동일 요청 재전송이며, 회로차단을 failure() 2회 호출로 인위 개방

- 심각도: Low
- 분류: 구현 편차 / 코드 위생 (§21.12-1·6, 요약 46)
- 파일/함수: `app/channels/dart_fulltext.py` `search_page` (L393~409)
- 재현: `structure_failure_candidate` 시 `/dsab007/main.do` 상태진단 없이 같은 `search.ax` POST를 반복하고, 이를 `diagnostics.health_check_requests`로 계상한다. 확정 시 `breaker.failure("structure_or_access")`를 연속 2회 호출해 임계값(2)을 즉시 충족시킨다.
- 현재 동작: 결과적으로 "재시도 후 반복 시 15분 차단+폴백"이라는 외형 동작은 맞다(테스트 `test_structure_failure_requires_retry_then_opens_15_minute_circuit` 통과). 그러나 (a) 진단 카운터의 의미가 오염되고(실제 상태진단이 아님), (b) failure 2회 호출은 1개 확정 사건을 2개 장애로 기록해 `opened_count`·`failure_count` 통계가 왜곡된다. 계획서 문구도 모호하다(→ P2).
- 기대 동작: 확정 1회 이벤트로 회로를 개방하는 명시적 API(`breaker.trip("structure_or_access")` 류), 재시도 요청의 별도 카운터.
- 최소 수정방향: `CircuitBreaker`에 즉시 개방 메서드 추가, 재시도를 `dart_result_page_requests` 또는 신규 `structure_retry_requests`로 계상.

### F10. 상태진단 실패를 일률적으로 network(3분)로 분류

- 심각도: Low
- 분류: 구현 편차 (§4.6 장애유형 구분, §2.6)
- 파일/함수: `app/channels/dart_fulltext.py` `health_check`
- 재현: `healthy = status==200 and b"detailSearch" in body`. 200 응답이지만 마커가 없는 경우(로그인/보안 페이지·구조 개편)도 `breaker.failure("network")`로 3분 차단이다.
- 현재 동작: 구조·접근성 장애가 15분 대신 3분 차단으로 과소 차단될 수 있다.
- 기대 동작: 200+마커 부재는 structure_or_access 후보로, 네트워크 예외는 network로 분리.
- 최소 수정방향: `health_check`에서 예외 경로와 "200이지만 마커 부재" 경로의 failure_class를 분리.

### F11. `채` 플래그를 `market_jurisdiction`으로 매핑해 신호를 혼합

- 심각도: Low
- 분류: 구현 편차 (§21.3 "`공`·`연`·`채`는 별도 해석하며 서로 섞지 않는다")
- 파일/함수: `app/channels/opendart.py` `candidate_from_list_row` (L179)
- 재현: `market = next(flag for flag in flags if flag in {"유","코","넥","채"}, None)` — `채`(채권상장법인)가 시장소관 필드에 들어간다.
- 현재 동작: `rm="채"` 후보의 `market_jurisdiction="채"`.
- 기대 동작: 시장소관은 `유·코·넥`만. `채`는 별도 신호 필드 또는 rm_flags 해석으로만 유지.
- 최소 수정방향: 집합에서 `채` 제거(필요 시 `bond_listed=True` 파생 필드 추가).

### F12. 공시뷰어 URL 하드코딩 중복

- 심각도: Low
- 분류: 코드 위생 (§21.8 상수 단일화, 항목 18)
- 파일/함수: `app/orchestrator/engine.py` `get_evidence` (L301)
- 재현: `f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={receipt_no}"` 하드코딩. `app/research/normalization.py` `dart_viewer_url()`이 동일 형식의 단일 출처로 이미 존재.
- 최소 수정방향: `dart_viewer_url(receipt_no)` 호출로 교체.

### F13. 검색 1회당 상태진단 1회 — 세션당 1회 계약 대비 과다

- 심각도: Low
- 분류: 구현 편차 (§4.3 "기본 세션은 상태진단 1회와 최초 모드설정 POST 1회를 사용")
- 파일/함수: `app/orchestrator/engine.py` `execute` (L100)
- 재현: DART 우선 전략이면 매 `execute()`마다 `health_check`(GET main.do)를 호출한다. 같은 세션에서 검색을 반복하면 검색 수만큼 상태진단이 누적되고 각각 DART 요청예산 10회에서 차감된다.
- 기대 동작: 회로 정상·최근 성공 상태면 상태진단 생략(세션당 1회 또는 TTL 기반).
- 최소 수정방향: breaker 상태가 HEALTHY이고 마지막 성공이 N분 이내면 health_check 생략하는 조건 추가.

### F14. 환경 의존 테스트 2건 — 재현성(§21.13) 저해

- 심각도: Low
- 분류: 이식성/테스트 (검토 환경에서 실제 실패 재현)
- 파일/함수: `tests/test_probe.py::test_stage05_golden_manifest_hashes`; `tests/test_stage1_contracts.py::test_shared_http_client_owns_cookie_session_and_verified_tls`
- 재현: 검토 환경(Linux, Python 3.10)에서 `python3 -m pytest tests/ -q` → 2 failed. (a) `tests/fixtures/probe/golden/stage0_5/manifest.json`의 `path`가 `golden\stage0_5\...` 백슬래시로 저장되어 POSIX에서 파일을 찾지 못한다(stage0_6 manifest는 검증 필요 — 동일 생성기 사용 시 동일 문제 가능, 단 stage0_6 테스트는 이 환경에서 통과했으므로 슬래시 저장으로 관찰됨). (b) TLS 테스트가 `ssl.VERIFY_X509_STRICT` 상수 존재=기본 활성화로 가정하나 3.13 미만에서는 상수만 있고 기본 비활성이라 `tls_strict_flag_relaxed=False`가 되어 실패한다. 구현(`http_client.py`)은 올바르게 조건부 처리하며, 테스트의 가정이 잘못됐다.
- 기대 동작: 대상 플랫폼 외에서도 회귀 테스트가 결정적으로 통과(§21.13 재현성 취지).
- 최소 수정방향: (a) manifest 생성·판독 시 `path`를 POSIX 구분자로 정규화(판독측 `item["path"].replace("\\\\","/")`가 최소 수정). (b) 테스트 조건을 "컨텍스트에 strict flag가 기본 설정된 경우에만 relaxed=True"로 변경.

### F15. `DART_FULLTEXT_FALLBACK` 경고 코드와 완전성 등급 강등 미구현

- 심각도: Low
- 분류: 구현 미비 (§2.6 — 계획상 단계 2 산출물이나, v0.1-core가 폴백 자체는 이미 구현했으므로 잔여분으로 기록)
- 파일/함수: `app/orchestrator/engine.py` `execute`; `app/errors.py`
- 재현: 폴백 시 `warnings`에 한국어 문자열과 `coverage.fallback_used=true`만 남는다. §2.6이 규정한 구조화 경고 코드 `DART_FULLTEXT_FALLBACK`은 코드베이스에 존재하지 않고(`grep -rn DART_FULLTEXT_FALLBACK app/` → 없음), "완전성 등급 한 단계 강등"에 해당하는 등급 필드 자체가 응답에 없다(coverage.complete boolean만 존재).
- 최소 수정방향: 응답에 구조화 경고 배열(`{code, message, fallback_source}`)과 `completeness_grade` 필드 도입. 단계 2 작업으로 이월해도 되나 이월 결정을 기록할 것.

### F16. 필수 테스트 누락 (항목 22)

- 심각도: Low
- 분류: 테스트 누락
- 파일: `tests/test_stage1_dart_fulltext.py`, `tests/test_stage1_contracts.py`
- 재현/현재: 다음 시나리오의 테스트가 없다 —
  1. 검색모드 변경(contents→report) 시 모드설정 POST가 다시 발생하는지(항목 3의 절반; 현재는 "생략"만 검증).
  2. 세션 초기화 시 모드 재설정(F2와 동일 원인 — 구현이 없어 테스트도 불가).
  3. HTTP 429/5xx 지수 백오프·재시도 상한(`http_client.py`의 구현 존재, 테스트 0건: `grep -rn "429" tests/` → 없음).
  4. 감사 로그의 검색어 변형·후보 기록(F6 확정 전에는 보류 가능).
- 기대: 계획 §21.13 데이터 기반 회귀 목록 + 단계 1 산출물에 대응하는 단위 테스트.
- 최소 수정방향: FakeHttp 기반으로 1·3을 즉시 추가(각 15줄 내외).

### F17. "실제 fixture와 연결된" 평가질의는 19개 — 문자 그대로의 20개 기준에 1개 미달

- 심각도: Low
- 분류: 구현 편차/해석 (요약 81, §11 단계 1, `MIN_FIXED_EVALUATION_QUERIES=20`)
- 파일: `tests/golden_cases/stage1/evaluation_queries.json`; `app/evaluation.py`
- 재현: 질의 총 24개(≥20 충족, `test_at_least_20_fixed_queries_and_required_categories` 통과). 그러나 `fixtures` 배열이 비어 있는 질의가 5개다(E07 invalid_api_key, E20 document_missing, E21 request_limit, E23/E24 회로차단) — 이들은 상수·상태코드 계약만 검사한다. fixture 연결 질의는 19개.
- 현재 동작: fixture 연결 질의의 `expected_receipts`는 실제 fixture 본문에서 확인됨(evaluation 24/24 통과, E13 날짜창·E18 페이지크기·E19 전환은 stage0.6 golden에 직접 연결). 계획의 "원문 검증 접수번호를 시드로"라는 취지는 대체로 충족.
- 기대 동작: "최소 20개 질의가 실제 fixture와 연결"을 문자 그대로 적용한다면 fixture 연결 질의 자체가 20개 이상이어야 한다.
- 최소 수정방향: E20(014)·E21(020)에 단계 0의 상태코드 fixture(예: `tests/fixtures/probe/opendart/status/`)를 연결하거나, 회로차단 질의에 `synthetic_structure_failure.html`을 연결하면 2~3개를 즉시 fixture 기반으로 전환할 수 있다.

---

## 3. 계획서(DEVELOPMENT_PLAN.md v19) 자체의 문제 — 구현 결함과 별도 분류

### P1. 계획서의 진행상태 서술이 저장소 상태와 모순

- 심각도: Medium (문서 정합성)
- 위치: §11 단계 0 상태 블록("단계 1은 아직 시작하지 않았다"), 요약 92("단계 1 본개발은 별도 착수 지시 전까지 시작하지 않는다"), §11 단계 0.5/0.6 서술("단계 1은 시작하지 않았다")
- 문제: 저장소에는 이미 단계 1 산출물 전체와 단계 2(어댑터·폴백)·단계 3(MCP 도구 2종, continuation) 상당 부분이 `v0.1-core`로 존재한다. v19가 착수 전에 확정된 문서라면 상태 서술이 낡은 것이고, 검토 기준으로는 "무엇이 단계 1 범위였는지"가 문서만으로 판정되지 않는다(예: F1·F15가 단계 1 결함인지 단계 2·3 이월분인지).
- 정정 방향: 상태 블록을 v0.1-core 반영으로 갱신하고, 선구현된 단계 2·3 항목과 잔여 항목을 명시.

### P2. §21.12-6 "상태진단 재시도"의 절차 정의 모호

- 심각도: Low
- 위치: §21.12-1·6, 요약 46
- 문제: 구조장애 후보의 승격 조건인 "상태진단 재시도"가 (a) `/dsab007/main.do` 상태진단 후 재검색인지 (b) 동일 검색 요청 재전송인지 정의되지 않았다. 구현(F9)은 (b)를 택했고 카운터는 상태진단 명칭을 썼다 — 모호성이 구현 편차의 직접 원인.
- 정정 방향: 승격 절차를 요청 시퀀스 수준으로 명시(예: main.do 진단 1회 → 동일 질의 1회 재시도 → 반복 시 승격).

### P3. §3.3 "사용자의 원 질의 기록"과 최소기록·마스킹 원칙 간 긴장

- 심각도: Low
- 위치: §3.3 기본 기록 항목 vs 같은 절의 "기본적으로 기록하지 않는 항목" 취지, §9.11
- 문제: 원 질의 평문은 그 자체로 민감할 수 있는데(법률 리서치 주제), 계획은 평문+해시 기록을 요구한다. 구현은 해시만 기록해 문서와 불일치(F6). 어느 쪽이 정본인지 계획서가 결정해야 한다.
- 정정 방향: "원 질의는 기본 기록하되 `audit_log=off`/`audit_query_text=off`로 제외 가능" 또는 "해시만 기록"으로 확정하고 F6과 함께 정렬.

### P4. §21.9 규칙 스키마 의무의 적용 시점 불명확

- 심각도: Low
- 위치: 요약 63, §21.9 vs §11 단계 1 목록
- 문제: "규칙파일·S6·S7 구현 전에 고정한다", "CI에서 실패시킨다"고 하면서 §11 단계 1 산출물 목록에는 규칙파일 스키마·CI 항목이 없다. `amendment_rules.yaml`이 단계 1에서 이미 생성·사용되므로(F5) 의무가 지금 적용되는지 S6/S7 시점인지 문서상 판정 불가.
- 정정 방향: "규칙파일이 저장소에 존재하는 순간부터 스키마 필수" 등 적용 시점을 명시.

### P5. 요약 86 "문서-코드 일치 CI 검사"의 검사 범위 미정의

- 심각도: Low
- 위치: 요약 86, §22
- 문제: 수치 상수 전반의 문서-코드 일치를 CI로 검사한다고 하나 검사 대상 상수 목록·방법이 정의되지 않았다. 현재 구현은 대표 문자열 7종의 존재 확인(`test_plan_code_rules_and_document_constants_do_not_drift`)으로, 계획 문구를 문자 그대로 이행하는 것은 사실상 불가능한 수준의 광범위 요구다.
- 정정 방향: 검사 대상 상수의 명시적 목록(페이지 크기·간격·회로시간·예산·캐시 한도 등)과 앵커 문자열 규약을 계획서에 정의.

---

## 4. 23개 검토항목별 판정 요약

| # | 항목 | 판정 | 근거/관련 지적 |
|---|---|---|---|
| 1 | 단계 0.6 결과 구현 | 대체로 충족 | 전환·페이지크기·날짜창 반영 확인. 잔여: F2(세션 재설정), F5(`채` 조합 마커), F8 |
| 2 | 동일 세션·모드에서 검색어 전환 시 모드설정 POST 없음 | 충족 | `_active_mode` 게이트, `test_mode_setup_is_not_repeated_for_keyword_switch`로 검증 |
| 3 | 세션/모드 변경 시 모드 재설정 | 부분 충족 | 모드 변경 시 재설정은 구현(`_active_mode != mode`), 세션 변경 경로 부재 — F2, F16-1 |
| 4 | `effective_page_size=10` | 충족 | `DART_EFFECTIVE_PAGE_SIZE=10`, `estimated_pages=ceil(count/10)`, E18 fixture 연결 |
| 5 | 페이지 크기 확대 파라미터 비의존 | 충족(단서) | `maxResultsCb` 미전송(테스트로 금지), 다만 미실측 값 10 전송 — F8 |
| 6 | 날짜창: 포함 경계 비중첩 연속 | 충족 | `dart_date_windows` + 테스트, 경계 포함은 `startDate/endDate` 그대로 전송(실측 계약 일치) |
| 7 | 날짜창 전체 기간 누락·중복 검사 | 충족 | `search_date_windows`의 창별 complete·`continuous`·전역 rcept_no 합집합; golden `date_window.json` missing/extra 빈 배열 검증 |
| 8 | `철` 확정 신호 오용 없음 | 충족 | 후보 신호만(`withdrawal_status=candidate_signal_only`), `verify_withdrawal_reference`는 명시 접수번호/라벨된 원제출일만 인정, 근접성 반례 테스트 존재 |
| 9 | `채` 의미 confirmed / 조합 unconfirmed | 부분 충족 | 의미 confirmed(yaml)·순서보존 파싱은 구현, 조합 `unconfirmed` 마커 미표시 — F5 |
| 10 | DART 결과행 구조 필드 파싱 | 충족 | 시장문자·그룹태그·본문/첨부·제출인·접수일 파싱, 실제 fixture 테스트 |
| 11 | 본문·첨부 중복의 결정적 제거 | 충족 | `merge_duplicate_rows` 접수번호 키 결정적 병합 + 엔진 전역 dedupe |
| 12 | 본문 매치 우선 | 충족 | 병합 시 body 우선·mixed 승격, ranking `body_or_mixed=+3`으로 검증 순서 반영 |
| 13 | 최신순 정렬 vs mechanical_score 구분 | 충족 | `sort=DATE` 전송, `coverage.server_sort/local_ranking` 분리, `latest_first_bias` 진단 |
| 14 | OpenDART 000·013·오류코드 | 충족 | `opendart_status.py` 전체 코드표, 013 정상 빈 창 계속, 020/800 무재시도 중단, 900 1회 재시도 — 테스트 존재 |
| 15 | 정상 0건 vs 구조장애 | 충족 | `normal_zero`/`structure_failure_candidate` 분류 + 재시도 승격, 승격 절차 정의는 P2/F9 참조 |
| 16 | 회로차단기·OpenDART 폴백 | 충족(잔여) | 3분/15분 이원, blocked 시간·폴백 안내 details 포함. 구조화 경고 코드·완전성 강등은 미구현 — F10, F15 |
| 17 | SearchPlan 불변성 | 충족 | frozen dataclass + FrozenInstanceError 테스트, 측정값은 Diagnostics 분리 |
| 18 | defaults.py·rules 상수 단일화 | 대체로 충족 | settings=DEFAULT_SETTINGS 테스트, MCP 스키마 상수 참조. 예외: F12(뷰어 URL 중복) |
| 19 | 평가질의 ≥20개 fixture 연결 | 부분 충족 | 24개 중 19개 fixture 연결, 5개는 상수/코드 계약형 — F17 |
| 20 | 키·쿠키·원문 로그 비노출 | 충족 | fixture 내 crtfc_key 마스킹 확인(grep), 쿠키 미저장, 감사 로그 재귀 마스킹+원문 제외 테스트, .env 로더 비로깅 |
| 21 | Fast Path 경량성 | 충족 | KIND/diff/디스크캐시/배치 미실행, exhaustive는 즉시 승인요구, plan_builder p95<50ms 벤치 테스트 |
| 22 | 필수 테스트 누락 | 일부 누락 | F14(환경 의존 2건), F16(모드 변경·백오프 등) |
| 23 | 계획서 모순·구현 불가 요구 | 5건 식별 | P1~P5 (2.4.5↔17.4 우선순위는 v19에서 정합 확인) |

---

## 5. 미확인·한계

- DART 서버측 세션 만료 시의 실제 응답 형태, `maxResults=10` 전송 시의 서버 동작은 실측 fixture가 없어 판정하지 않았다(F2·F8은 코드-계약 불일치로만 확정).
- 검토 환경이 Linux/Python 3.10이므로 F14 두 실패가 대상 환경(Windows/Python 3.14)에서 재현되는지는 미확인이다. 다만 (a) 백슬래시 manifest는 POSIX CI를 막고, (b) TLS 테스트는 3.13 미만 어디서나 실패하므로 이식성 결함 자체는 확정이다.
- 작업트리의 fixture 대량 diff(129k줄 삽입=삭제)는 줄바꿈 변환으로 보이며 내용 변경 근거는 발견하지 못했다.
