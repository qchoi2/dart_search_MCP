# 테스트 결과

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
