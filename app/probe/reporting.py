from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .common import utc_now, write_json
from .dart_web import PREFIXES


UNCONFIRMED = "미확인"


def combine_findings(
    web: dict[str, Any] | None,
    opendart: dict[str, Any] | None,
) -> dict[str, Any]:
    plan_comparison = {
        "last_reprt_at_measured_3_cases_N_all_Y_event_final": _bool_or_unconfirmed(
            opendart and opendart.get("last_reprt_all_match_plan")
        ),
        "rm_flags_have_followup_evidence_for_30_sampled_events": _rm_comparison(opendart),
        "all_official_report_nm_prefixes_observed": _bool_or_unconfirmed(
            opendart and opendart.get("prefixes", {}).get("all_eight_observed")
        ),
        "multiple_report_nm_prefixes_exist": _bool_or_unconfirmed(
            opendart and opendart.get("prefixes", {}).get("multi_prefix_observed")
        ),
        "status_013_is_empty_result": _bool_or_unconfirmed(
            opendart and opendart.get("status_013", {}).get("normal_no_data_confirmed")
        ),
        "D004_corp_name_matches_target_in_10_sampled_filings": _d004_comparison(opendart),
        "DART_normal_zero_has_explicit_marker": _bool_or_unconfirmed(
            web and web.get("normal_zero", {}).get("classification") == "normal_zero"
        ),
        "DART_actual_structure_failure_branch": (
            UNCONFIRMED
            if not web
            or not web.get("structure_failure_test", {}).get("actual_structure_failure_observed")
            else True
        ),
        "DART_concurrency_1_technically_operated": _bool_or_unconfirmed(
            web and web.get("all_http_200") and web.get("concurrency") == 1
        ),
        "DART_automation_permission_and_allowed_interval": UNCONFIRMED,
    }
    decisions = _decisions(web, opendart, plan_comparison)
    return {
        "schema_version": 1,
        "generated_at": utc_now(),
        "scope": "DEVELOPMENT_PLAN stage 0 measurement only; no product implementation",
        "web": web,
        "opendart": opendart,
        "plan_comparison": plan_comparison,
        "decisions": decisions,
    }


def _bool_or_unconfirmed(value: Any) -> bool | str:
    return bool(value) if value is not None else UNCONFIRMED


def _rm_comparison(opendart: dict[str, Any] | None) -> bool | str:
    if not opendart or opendart.get("rm_event_count", 0) < 30:
        return UNCONFIRMED
    return (
        opendart.get("rm_matched_count") == opendart.get("rm_event_count")
        and set(opendart.get("rm_flags_observed", [])) == {"정", "철"}
    )


def _d004_comparison(opendart: dict[str, Any] | None) -> bool | str:
    if not opendart or opendart.get("d004_case_count", 0) < 10:
        return UNCONFIRMED
    return opendart.get("d004_verified_count") == opendart.get("d004_case_count")


def _decisions(
    web: dict[str, Any] | None,
    opendart: dict[str, Any] | None,
    comparison: dict[str, Any],
) -> list[dict[str, str]]:
    decisions: list[dict[str, str]] = []
    for key, value in comparison.items():
        if value is False:
            decisions.append(
                {
                    "topic": key,
                    "actual_result": "계획 가정과 불일치",
                    "change": "관련 S6/S7 또는 판정 규칙을 기본 비활성화하고 fixture 기반 규칙으로 재설계",
                    "reason": "단계 0 실제 응답이 계획 가정을 지지하지 않음",
                }
            )
    if web:
        decisions.append(
            {
                "topic": "DART 본문검색 개인용 조건부 사용",
                "actual_result": "robots.txt에서 /dsab007 금지는 확인되지 않았고 순차 요청은 성공했으나, 자동화 허용 빈도와 명시적 허용 문구는 확인하지 못함",
                "change": "개인용 로컬 환경에서는 기술 상태가 정상이고 검색 품질 향상이 확인되면 동시성 1·요청 시작간격 최소 1,000ms로 조건부 사용",
                "reason": "사용자가 개인용 범위에서 정책 미확인 위험을 수용했으며, 명시적 금지·접근거부·반복 구조장애 시에는 OpenDART로 폴백함",
            }
        )
    if web and opendart:
        decisions.append(
            {
                "topic": "단계 0 게이트 분리",
                "actual_result": "OpenDART 관련 항목과 DART 정상 파서는 통과했지만 DART 정책과 실제 구조장애는 미확인",
                "change": "OpenDART 목록·정정규칙·D004·DART 파서·DART 개인용·DART 정책 게이트를 분리",
                "reason": "DART의 미확인 항목이 OpenDART 기반 개발 전체를 차단하지 않도록 함",
            }
        )
    if opendart and any(
        case.get("same_name_Y_rows_outside_event")
        for case in opendart.get("last_reprt_comparisons", [])
    ):
        decisions.append(
            {
                "topic": "정정 사건 그룹 키",
                "actual_result": "같은 회사·기간·정규화 보고서명에도 서로 독립적인 공시 사건이 함께 반환된 실제 사례가 확인됨",
                "change": "명시적 원접수번호·정정대상 접수번호·정정사항표를 우선 사용하고 회사·보고서명·시간 군집은 보조수단으로만 사용",
                "reason": "아이엠증권의 last_reprt_at=Y 응답에 비교 대상 체인 밖의 동일 보고서명 공시가 별도로 존재함",
            }
        )
        decisions.append(
            {
                "topic": "last_reprt_at 사용범위",
                "actual_result": "측정한 3개 사건에서 N은 체인 전체, Y는 해당 사건 최종 접수를 반환했지만 Y에 독립 사건이 함께 존재함",
                "change": "N은 체인 수집, Y는 후보 축소에만 사용하고 사건 식별키나 최종본 증명으로 사용하지 않음",
                "reason": "동일 회사·기간·정규화 보고서명이 사건의 유일성을 보장하지 않음",
            }
        )
    if opendart and opendart.get("rm_event_count", 0) >= 30:
        decisions.append(
            {
                "topic": "rm 표본 신뢰도",
                "actual_result": "정 28건·철 2건이 후속 문서와 일치했으나 검증에 회사·보고서명·근접기간 휴리스틱이 포함됨",
                "change": "rm은 후보 신호로만 사용하고 명시적 접수번호·정정사항표·후속 공시로 최종 검증",
                "reason": "철 표본이 적고 휴리스틱이 독립 사건을 혼합할 수 있음",
            }
        )
    if opendart and opendart.get("prefixes", {}).get("counts", {}).get("연장결정", 0):
        decisions.append(
            {
                "topic": "연장결정 접두어 검색 유형",
                "actual_result": "[연장결정]은 B 계열 주요사항보고서(자기주식취득신탁계약체결결정)에서 실제 관측됨",
                "change": "연장결정 표본 탐색을 E002가 아니라 B 공시유형으로 라우팅",
                "reason": "실제 OpenDART 목록 JSON의 report_nm과 pblntf_ty=B 조건으로 확인",
            }
        )
    if opendart and opendart.get("d004_case_count", 0) >= 10:
        decisions.append(
            {
                "topic": "D004 사건 단위와 대상회사 신뢰도",
                "actual_result": "corp_name 대상회사 일치는 공시 10건에서 확인됐지만 회사·제출인 조합은 5개",
                "change": "공시 건수와 공개매수 사건 수를 분리하고 원문 생략 시 출처·미검증 상태·신뢰도를 표시",
                "reason": "같은 사건의 신고서·설명서·결과보고서가 별도 공시로 반환될 수 있음",
            }
        )
    if opendart and opendart.get("status_013", {}).get("normal_no_data_confirmed"):
        decisions.append(
            {
                "topic": "013 적용범위",
                "actual_result": "목록 API D004 빈 기간창에서 013과 목록 부재를 확인",
                "change": "목록 API의 013을 정상 빈 배열로 처리하되 다른 엔드포인트까지 실측됐다고 일반화하지 않음",
                "reason": "실측 조건과 엔드포인트의 범위를 규칙 신뢰도에 반영",
            }
        )
    if web:
        decisions.append(
            {
                "topic": "DART 구조장애 판정",
                "actual_result": "정상 결과와 정상 0건은 확인했지만 실제 구조장애는 관찰하지 못함",
                "change": "첫 모호 응답은 structure_failure_candidate로 두고 상태진단 재시도에서도 반복될 때만 구조장애로 승격",
                "reason": "합성 fixture는 분류기 분기 검증일 뿐 실제 장애의 증거가 아님",
            }
        )
        decisions.extend(
            [
                {
                    "topic": "플래그십 S3 문구 단계 0.5 게이트",
                    "actual_result": "정상 결과는 주식매수청구권 8,875건으로 확인했지만 상계납입·출자전환은 미실측",
                    "change": "상계납입·주금납입채무와 상계·출자전환 변형어의 실제 검색과 원문 정답을 S3 활성화 전 필수 게이트로 추가",
                    "reason": "플래그십 검색어의 형태소·동의어 동작을 다른 검색어 결과로 일반화할 수 없음",
                },
                {
                    "topic": "DART 검색건수·페이지네이션 계약",
                    "actual_result": "주식매수청구권 검색건수 8,875, 첫 페이지 결과행 10, 마지막 페이지 링크 888을 관찰했지만 2페이지 실제 호출은 미수행",
                    "change": "currentPage·실효 페이지 크기·추가 페이지 요청수를 단계 0.5에서 확정하고 검색건수를 충분성·후보예산·배치판정에 사용",
                    "reason": "수천 건 결과에서 페이지·원문 예산과 rate 하한을 계산해야 함",
                },
                {
                    "topic": "DART 배치 시간 하한",
                    "actual_result": "실측 초기 안전값은 동시성 1·요청 시작간격 최소 1,000ms",
                    "change": "estimated_dart_requests와 dart_rate_floor_seconds를 배치 미리보기에 포함",
                    "reason": "예상시간이 물리적인 요청 시작간격 하한보다 작아지지 않도록 함",
                },
            ]
        )
    if opendart:
        decisions.extend(
            [
                {
                    "topic": "S6·S7 정정유형 층화 게이트",
                    "actual_result": "N/Y 3사건이 채무성 증권신고·주요사항보고에 편중",
                    "change": "유상증자·합병·전환사채 N/Y 표본을 각각 확보하기 전 S6·S7 기본 활성화 금지",
                    "reason": "대표 사용사례 유형으로 실측 규칙을 일반화하기 위한 근거가 부족함",
                },
                {
                    "topic": "S8 시장 rm 플래그",
                    "actual_result": "정·철 외 유·코·채·넥·공·연의 문서 의미는 검증하지 않음",
                    "change": "I 유형 probe 전에는 진단정보로만 보존하고 하드 시장필터로 사용하지 않음",
                    "reason": "미검증 플래그를 확정 필터로 사용하면 후보를 잘못 제외할 수 있음",
                },
                {
                    "topic": "D004 동일회사 제출 분기",
                    "actual_result": "로컬 목록에 corp_name == flr_nm 8건이 있으나 대상회사 원문 검증 표본에는 포함되지 않음",
                    "change": "문서 역할별 원문 probe 전까지 target_company_confidence=provisional로 처리",
                    "reason": "신고서·의견표명서·결과보고서에서 같은 회사 필드의 역할이 다를 수 있음",
                },
                {
                    "topic": "소수표본 신뢰도 전파",
                    "actual_result": "연장결정 1건·정정명령부과 1건·rm=철 검증 2건",
                    "change": "sample_count < 3 규칙은 amendment_rules.yaml에서 confidence=provisional을 강제",
                    "reason": "소수표본 규칙이 확정 규칙처럼 전파되는 것을 방지",
                },
                {
                    "topic": "raw fixture 저장소 정리",
                    "actual_result": "단계 0 raw fixture 1,946개·약 48.89MB가 이미 커밋 이력과 v0.0-probe 태그에 포함됨",
                    "change": "추가 raw의 Git 유입을 중단하고 golden manifest·외부 압축보관으로 전환하며 이력 재작성은 별도 승인 대상으로 분리",
                    "reason": "새 커밋에서 삭제·압축해도 기존 Git 이력 크기는 줄지 않으며 태그 재작성은 파괴적 변경임",
                },
            ]
        )
    return decisions


def render_outputs(repo_root: Path, findings: dict[str, Any]) -> None:
    fixture_root = repo_root / "tests/fixtures/probe"
    write_json(fixture_root / "stage0_findings.json", findings)
    checklist = _approval_checklist(findings)
    write_json(fixture_root / "approval_checklist.json", checklist)
    (repo_root / "PROBE_RESULTS.md").write_text(
        _probe_results_markdown(findings, checklist), encoding="utf-8"
    )
    (repo_root / "DECISIONS.md").write_text(
        _decisions_markdown(findings), encoding="utf-8"
    )


def _approval_checklist(findings: dict[str, Any]) -> dict[str, Any]:
    comparison = findings["plan_comparison"]
    return {
        "generated_at": findings["generated_at"],
        "product_development_started": False,
        "checks": [
            {
                "id": key,
                "result": value,
                "approved": value is True,
            }
            for key, value in comparison.items()
        ],
        "stage0_complete": all(value != UNCONFIRMED for value in comparison.values()),
        "release_gate_approved": False,
        "personal_dart_use_conditionally_approved": True,
        "personal_dart_use_conditions": {
            "scope": "personal_local_only",
            "concurrency": 1,
            "minimum_request_start_interval_seconds": 1.0,
            "requires_technical_health": True,
            "requires_search_quality_gain": True,
            "fallback": "OpenDART",
        },
    }


def _probe_results_markdown(findings: dict[str, Any], checklist: dict[str, Any]) -> str:
    web = findings.get("web")
    dart = findings.get("opendart")
    comparison = findings["plan_comparison"]
    lines = [
        "# Stage 0 Probe Results",
        "",
        f"- 생성시각(UTC): {findings['generated_at']}",
        "- 범위: 단계 0 실측 전용. 본 프로그램 기능은 구현하지 않음.",
        "- API 키: 요청 로그에서 `***MASKED***`로 저장하며 원문 키는 저장하지 않음.",
        "",
        "## 1. `last_reprt_at=N/Y` 결과집합",
        "",
    ]
    if dart:
        lines.append(f"실측 사건 수: {dart.get('last_reprt_case_count', 0)}")
        lines.append("")
        for case in dart.get("last_reprt_comparisons", []):
            n_rows = "; ".join(
                f"{row['rcept_no']} `{row['report_nm']}` (rm={row.get('rm') or '없음'})"
                for row in case.get("N", [])
            )
            y_rows = "; ".join(
                f"{row['rcept_no']} `{row['report_nm']}` (rm={row.get('rm') or '없음'})"
                for row in case.get("Y", [])
            )
            outside_rows = "; ".join(
                f"{row['rcept_no']} `{row['report_nm']}`"
                for row in case.get("same_name_Y_rows_outside_event", [])
            )
            lines.extend(
                [
                    f"### 사건 {case['case']}: {case['corp_name']}",
                    "",
                    f"- 정규화 보고서명: `{case['normalized_report_name']}`",
                    f"- N 실제 행: {n_rows or UNCONFIRMED}",
                    f"- Y의 동일 사건 실제 행: {y_rows or UNCONFIRMED}",
                    f"- Y 응답의 동일 보고서명·별도 사건: {outside_rows or '없음'}",
                    f"- N에서 원공시·중간정정·최종정정 확인: {_ko(case['N_contains_original_intermediate_final'])}",
                    f"- Y가 N의 최종 접수 1건만 반환: {_ko(case['Y_is_only_latest_N_receipt'])}",
                    f"- 원본 fixture: {', '.join(case['raw_fixtures'])}",
                    "",
                ]
            )
    else:
        lines.extend([UNCONFIRMED, ""])

    lines.extend(["## 2. `rm`의 `정`·`철`", ""])
    if dart:
        lines.extend(
            [
                f"- 서로 다른 사건 표본: {dart.get('rm_event_count', 0)}건",
                f"- 실제 후속 체인 일치: {dart.get('rm_matched_count', 0)}건",
                f"- 일치율: {_percent(dart.get('rm_match_rate'))}",
                f"- 관찰 플래그: {', '.join(dart.get('rm_flags_observed', [])) or UNCONFIRMED}",
            ]
        )
        for event in dart.get("rm_validation", []):
            source = event["source"]
            lines.append(
                f"- 사건 {event['event']}: {source['rcept_no']} {source['corp_name']} "
                f"`{source['report_nm']}` — rm=`{event['flag']}`, 체인 {_ko(event['matched'])}, "
                f"fixture `{event['raw_fixture']}`"
            )
        lines.extend(
            [
                "- 사건별 원본 JSON: `tests/fixtures/probe/opendart/rm_validation/`",
                "",
            ]
        )
    else:
        lines.extend([UNCONFIRMED, ""])

    lines.extend(["## 3. `report_nm` 접두어", ""])
    if dart:
        counts = dart.get("prefixes", {}).get("counts", {})
        prefix_samples = dart.get("prefixes", {}).get("samples", {})
        for prefix in PREFIXES:
            sample_rows = prefix_samples.get(prefix, [])
            example = sample_rows[0] if sample_rows else None
            sample_text = (
                f"; 표본 {example['rcept_no']} {example['corp_name']} "
                f"`{example['report_nm']}`"
                if example
                else ""
            )
            lines.append(f"- {prefix}: {counts.get(prefix, 0)}건{sample_text}")
        multi = dart.get("prefixes", {}).get("multi_prefix_samples", [])
        lines.extend(
            [
                f"- 복수 접두어 관찰: {_ko(bool(multi))}",
                f"- 복수 접두어 표본 수: {len(multi)}",
                "- 원본 JSON/HTML: `tests/fixtures/probe/opendart/prefixes/`, `tests/fixtures/probe/dart_web/prefix_*.html`",
                "",
            ]
        )
    elif web:
        lines.extend(
            [
                "OpenDART `report_nm` 확인은 미확인. DART 웹 검색 HTML 표본만 수집됨.",
                "",
            ]
        )
    else:
        lines.extend([UNCONFIRMED, ""])

    lines.extend(["## 4. OpenDART 상태 `013`", ""])
    if dart:
        status = dart["status_013"]
        lines.extend(
            [
                f"- 조건: `{json.dumps(status['request_condition'], ensure_ascii=False)}`",
                f"- 실제 상태/메시지: `{status.get('status')}` / `{status.get('message')}`",
                f"- 목록 없음 확인: {_ko(not status.get('has_list'))}",
                f"- fixture: `{status['fixture']}`",
                "",
            ]
        )
    else:
        lines.extend([UNCONFIRMED, ""])

    lines.extend(["## 5. D004 공개매수 대상회사", ""])
    if dart:
        lines.extend(
            [
                f"- `corp_name != flr_nm` 표본: {dart.get('d004_case_count', 0)}건",
                f"- 원문에서 대상회사 표기와 일치: {dart.get('d004_verified_count', 0)}건",
            ]
        )
        for index, result in enumerate(dart.get("d004", []), start=1):
            row = result["list_row"]
            lines.append(
                f"- 사례 {index}: {row['rcept_no']} — 대상 `{row['corp_name']}`, "
                f"제출인 `{row['flr_nm']}`, 원문 검증 {_ko(result['corp_name_near_target_company_label'])}"
            )
        lines.extend(
            [
                "- 원본 ZIP/검증문맥: `tests/fixtures/probe/opendart/d004/`",
                "",
            ]
        )
    else:
        lines.extend([UNCONFIRMED, ""])

    lines.extend(["## 6. DART 본문검색", ""])
    if web:
        zero = web["normal_zero"]
        positive = web["normal_results"]
        structure = web["structure_failure_test"]
        lines.extend(
            [
                f"- 정상 0건 실측 검색어: `{zero.get('query', UNCONFIRMED)}`",
                f"- 정상 0건 분류: `{zero['classification']}`",
                f"- 명시적 0건 마커: {', '.join(zero.get('zero_markers', [])) or UNCONFIRMED}",
                f"- 정상 결과 실측 검색어: `{positive.get('query', UNCONFIRMED)}`",
                f"- 응답 검색건수: {positive.get('result_count', UNCONFIRMED)}건",
                f"- 정상 결과행 분류: `{positive['classification']}` / 결과행 {len(positive.get('rows', []))}건",
                f"- 실측 fixture: `{zero.get('fixture', UNCONFIRMED)}`, `{positive.get('fixture', UNCONFIRMED)}`",
                f"- 실제 구조장애 관찰: {UNCONFIRMED if not structure['actual_structure_failure_observed'] else '확인'}",
                f"- 합성 구조장애 fixture 분류: `{structure['synthetic_classification']}` (실제 장애 근거로 사용하지 않음)",
                f"- 동시성: {web['concurrency']}, 설정 요청간격: {web['configured_min_interval_seconds']}초, "
                f"실측 최소 시작간격: {web.get('minimum_observed_start_interval_seconds', UNCONFIRMED)}초, "
                f"전 요청 HTTP 200: {_ko(web['all_http_200'])}",
                f"- robots의 `/dsab007` 명시 금지 관찰: {'있음' if web['robots']['fulltext_path_explicitly_disallowed'] else '없음'}",
                f"- 자동화 허용 빈도·명시적 허용정책: {UNCONFIRMED}",
                "- 원본 HTML/robots/request log: `tests/fixtures/probe/dart_web/`, `tests/fixtures/probe/requests.jsonl`",
                "",
            ]
        )
    else:
        lines.extend([UNCONFIRMED, ""])

    lines.extend(["## 계획서 가정과 실제 결과", ""])
    for key, value in comparison.items():
        lines.append(f"- `{key}`: {_ko(value)}")
    lines.extend(
        [
            "",
            "## 단계 0 승인 상태",
            "",
            f"- 모든 판정 확정(미확인 없음): {'예' if checklist['stage0_complete'] else '아니오'}",
            f"- 출시 게이트 승인(실측 당시): {'예' if checklist['release_gate_approved'] else '아니오'}",
            "- 미확인 항목은 추론으로 보완하지 않음.",
            "",
            "## 후속 사용자 결정",
            "",
            "- 이 절은 실측 결과를 변경하지 않고 실측 이후의 운영 결정을 기록한다.",
            "- 사용범위: 개인용 로컬 사용",
            "- DART 본문검색: 명시적 정책 근거가 미확인이어도 기술 상태가 정상이고 검색 품질 향상이 확인되면 조건부 사용",
            "- 초기 안전값: 동시성 1, 요청 시작간격 최소 1.0초",
            "- 중단조건: 명시적 금지, 접근거부, 반복 구조장애, 또는 검색 품질 이득 부재",
            "- 폴백: OpenDART 목록·원문검색",
            "- 다수 사용자 배포·공유 서버·서비스 제공으로 범위가 바뀌면 정책 게이트를 다시 판정함",
            "",
        ]
    )
    return "\n".join(lines)


def _decisions_markdown(findings: dict[str, Any]) -> str:
    decisions = findings.get("decisions", [])
    lines = [
        "# Stage 0 Decisions",
        "",
        "실측 결과와 후속 사용자 결정을 기록한다. 미확인 사실과 개인용 사용 결정을 구분한다.",
        "",
    ]
    if not decisions:
        lines.extend(["현재 기록할 계획 변경 없음.", ""])
        return "\n".join(lines)
    for index, decision in enumerate(decisions, start=1):
        lines.extend(
            [
                f"## {index}. {decision['topic']}",
                "",
                f"- 실제 결과: {decision['actual_result']}",
                f"- 변경: {decision['change']}",
                f"- 이유: {decision['reason']}",
                "",
            ]
        )
    return "\n".join(lines)


def _ko(value: Any) -> str:
    if value is True:
        return "일치/확인"
    if value is False:
        return "불일치/미충족"
    return str(value)


def _percent(value: Any) -> str:
    return UNCONFIRMED if value is None else f"{float(value) * 100:.1f}%"
