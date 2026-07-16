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
        "last_reprt_at_N_all_Y_final": _bool_or_unconfirmed(
            opendart and opendart.get("last_reprt_all_match_plan")
        ),
        "rm_flags_match_actual_chains_for_30_events": _rm_comparison(opendart),
        "all_official_report_nm_prefixes_observed": _bool_or_unconfirmed(
            opendart and opendart.get("prefixes", {}).get("all_eight_observed")
        ),
        "multiple_report_nm_prefixes_exist": _bool_or_unconfirmed(
            opendart and opendart.get("prefixes", {}).get("multi_prefix_observed")
        ),
        "status_013_is_empty_result": _bool_or_unconfirmed(
            opendart and opendart.get("status_013", {}).get("normal_no_data_confirmed")
        ),
        "D004_corp_name_is_target_company": _d004_comparison(opendart),
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
                "topic": "DART 본문검색 출시 게이트",
                "actual_result": "robots.txt에서 /dsab007 금지는 확인되지 않았고 순차 요청은 성공했으나, 자동화 허용 빈도와 명시적 허용 문구는 확인하지 못함",
                "change": "접근정책의 명시적 근거가 추가 확인될 때까지 출시 게이트를 승인하지 않음",
                "reason": "기술적 접근 성공은 정책상 허용을 의미하지 않음",
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
                "change": "회사와 정규화 보고서명만으로 사건을 합치지 않고, 접수번호·접수일·정정 순서와 사건 시간 군집을 함께 사용",
                "reason": "아이엠증권의 last_reprt_at=Y 응답에 비교 대상 체인 밖의 동일 보고서명 공시가 별도로 존재함",
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
                f"- 정상 0건 분류: `{zero['classification']}`",
                f"- 명시적 0건 마커: {', '.join(zero.get('zero_markers', [])) or UNCONFIRMED}",
                f"- 정상 결과행 분류: `{positive['classification']}` / 결과행 {len(positive.get('rows', []))}건",
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
            f"- 출시 게이트 승인: {'예' if checklist['release_gate_approved'] else '아니오'}",
            "- 미확인 항목은 추론으로 보완하지 않음.",
            "",
        ]
    )
    return "\n".join(lines)


def _decisions_markdown(findings: dict[str, Any]) -> str:
    decisions = findings.get("decisions", [])
    lines = [
        "# Stage 0 Decisions",
        "",
        "실측으로 확인된 계획 차이만 기록한다. 미확인은 결론으로 바꾸지 않는다.",
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
