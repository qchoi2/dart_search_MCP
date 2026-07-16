# 개발 진행 현황

## 이번 세션 반영 요약

- `DEVELOPMENT_PLAN.md`를 v19로 갱신해 단계 0.6 실측 결과를 구현 규칙과 요청예산 계약에 반영했다.
- 같은 Cookie 세션·검색 모드에서는 검색어가 바뀌어도 모드설정 POST를 반복하지 않는 규칙을 확정했다.
- `maxResults`·`maxResultsCb` 확대가 실제 결과행을 늘리지 못하므로 `effective_page_size=10`을 유지하고 확대 파라미터에 의존하지 않도록 했다.
- DART 날짜 범위가 시작일·종료일 포함 경계임을 기록하고, 비중첩 연속 날짜창·창별 완료·전역 접수번호 합집합을 전수성 배치 규칙으로 활성화했다.
- `rm=철`은 명시 연결 9건과 오병합 반례 1건을 근거로 provisional 후보 신호를 유지하고, 회사명·보고서명·근접기간만으로 병합·제외하지 않도록 했다.
- 실제 `rm=채` 16건으로 `채=채권상장법인` 의미는 confirmed로 올렸지만, 실제 조합문자 표본이 없어 조합 파싱 신뢰도는 unconfirmed로 분리했다.
- 단계 0.6 전용 runner, 실행별 raw fixture, curated golden fixture, 원자적 manifest, 결과 보고서와 회귀 테스트를 추가했다.
- `python -m unittest tests.test_probe -v` 16개, compileall, golden hash, raw 실응답을 제외한 소스·문서 `git diff --check` 검증을 통과했다.
- 단계 1 본개발과 기존 제품 기능 구현은 시작하지 않았다.

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
