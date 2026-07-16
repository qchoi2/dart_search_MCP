from __future__ import annotations

import html
import re
from datetime import date, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from .common import RecordedHttpClient, write_json


DART_BASE = "https://dart.fss.or.kr"
PREFIXES = (
    "기재정정",
    "첨부정정",
    "첨부추가",
    "변경등록",
    "연장결정",
    "발행조건확정",
    "정정명령부과",
    "정정제출요구",
)
PREFIX_PATTERN = re.compile(r"^\s*((?:\[[^\]]+\]\s*)+)")


class _ResultTableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.in_tr = False
        self.in_td = False
        self.current_cell: list[str] = []
        self.current_cells: list[str] = []
        self.current_receipts: list[str] = []
        self.rows: list[dict[str, Any]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = dict(attrs)
        if tag == "tr":
            self.in_tr = True
            self.current_cells = []
            self.current_receipts = []
        elif self.in_tr and tag in {"td", "th"}:
            self.in_td = True
            self.current_cell = []
        if self.in_tr and tag == "a":
            joined = " ".join(
                value or "" for key, value in attrs if key in {"href", "onclick"}
            )
            self.current_receipts.extend(re.findall(r"(?<!\d)(20\d{12})(?!\d)", joined))

    def handle_endtag(self, tag: str) -> None:
        if self.in_tr and tag in {"td", "th"} and self.in_td:
            self.current_cells.append(_clean_text(" ".join(self.current_cell)))
            self.in_td = False
        elif tag == "tr" and self.in_tr:
            text = _clean_text(" | ".join(self.current_cells))
            if self.current_receipts or (text and "조회 결과가 없습니다" not in text):
                self.rows.append(
                    {
                        "cells": self.current_cells,
                        "text": text,
                        "rcept_no": self.current_receipts[0] if self.current_receipts else None,
                    }
                )
            self.in_tr = False

    def handle_data(self, data: str) -> None:
        if self.in_tr and self.in_td:
            self.current_cell.append(data)


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def extract_prefixes(report_name: str) -> list[str]:
    match = PREFIX_PATTERN.match(report_name)
    if not match:
        return []
    return re.findall(r"\[([^\]]+)\]", match.group(1))


def official_prefixes_in_row(row: dict[str, Any]) -> tuple[str, list[str]]:
    for cell in row.get("cells", [])[:3]:
        tokens = re.findall(r"\[([^\]]+)\]", cell)
        official = [token for token in tokens if token in PREFIXES]
        if official:
            return cell, official
    return "", []


def parse_search_html(text: str) -> dict[str, Any]:
    parser = _ResultTableParser()
    parser.feed(text)
    zero_markers = sorted(
        set(
            marker
            for marker in (
                "조회 결과가 없습니다.",
                "검색결과가 없습니다.",
                "조회된 결과가 없습니다.",
            )
            if marker in text
        )
    )
    count_matches = re.findall(r"검색건수\s*[:：]\s*([0-9,]+)", text)
    result_count = int(count_matches[-1].replace(",", "")) if count_matches else None
    rows = [row for row in parser.rows if row.get("rcept_no")]
    if rows:
        classification = "results"
    elif zero_markers or result_count == 0:
        classification = "normal_zero"
    else:
        classification = "structure_failure_candidate"
    return {
        "classification": classification,
        "result_count": result_count,
        "zero_markers": zero_markers,
        "rows": rows,
        "has_result_rows": bool(rows),
    }


def _base_form(start_date: str, end_date: str, max_results: int = 100) -> dict[str, Any]:
    return {
        "currentPage": "1",
        "maxResults": str(max_results),
        "maxLinks": "10",
        "sort": "DATE",
        "sortType": "desc",
        "textCrpCik": "",
        "lateKeyword": "",
        "flrCik": "",
        "dspTypeTab": "",
        "isSort": "false",
        "isTab": "false",
        "tocSrch": "",
        "b_textCrpCik": "",
        "b_flrCik": "",
        "b_keyword": "",
        "b_docType": "",
        "b_textPresenterNm": "",
        "b_reportName": "",
        "b_startDate": start_date,
        "b_endDate": end_date,
        "b_dspType": "",
        "b_synonym": "",
        "b_reSearch": "",
        "reportNamePopYn": "",
        "autoSearch": "N",
        "option": "contents",
        "keyword": "",
        "textCrpNm": "",
        "textPresenterNm": "",
        "startDate": start_date,
        "endDate": end_date,
        "decadeType": "",
        "docType": "",
        "reportName": "",
    }


def _search(
    client: RecordedHttpClient,
    *,
    query: str,
    option: str,
    start_date: str,
    end_date: str,
    fixture: str,
) -> dict[str, Any]:
    form = _base_form(start_date, end_date)
    form["option"] = option
    if option == "contents":
        form["keyword"] = query
        form["b_keyword"] = query
    elif option == "report":
        form["reportName"] = query
        form["b_reportName"] = query
        form["reportNamePopYn"] = "N"
    else:
        raise ValueError(f"Unsupported DART search option: {option}")
    mode_endpoint = "detailSearchMain2.do" if option == "contents" else "detailSearchMain.do"
    mode_fixture = fixture[:-5] + "_mode.html" if fixture.endswith(".html") else fixture + ".mode"
    client.request(
        "POST",
        f"{DART_BASE}/dsab007/{mode_endpoint}",
        form=form,
        headers={"Referer": f"{DART_BASE}/dsab007/main.do"},
        fixture=mode_fixture,
        timeout=90,
    )
    endpoint = "search.ax" if option == "contents" else "detailSearch.ax"
    response = client.request(
        "POST",
        f"{DART_BASE}/dsab007/{endpoint}",
        form=form,
        headers={"Referer": f"{DART_BASE}/dsab007/main.do"},
        fixture=fixture,
        timeout=90,
    )
    parsed = parse_search_html(response.text)
    parsed.update(
        {
            "query": query,
            "option": option,
            "http_status": response.status,
            "fixture": response.fixture,
            "request": response.record,
        }
    )
    return parsed


def run_dart_web_probe(fixture_root: Path, min_interval: float = 1.0) -> dict[str, Any]:
    root = fixture_root / "dart_web"
    client = RecordedHttpClient(fixture_root, min_interval=min_interval)

    robots = client.request(
        "GET",
        f"{DART_BASE}/robots.txt",
        fixture="dart_web/robots.txt",
    )
    notice = client.request(
        "GET",
        f"{DART_BASE}/introduction/content5.do",
        fixture="dart_web/information_notice.html",
    )
    main = client.request(
        "GET",
        f"{DART_BASE}/dsab007/main.do",
        fixture="dart_web/search_main.html",
    )

    today = date.today()
    start_date = today.replace(year=today.year - 1).strftime("%Y%m%d")
    end_date = today.strftime("%Y%m%d")

    zero = _search(
        client,
        query=f"코덱스실측존재하지않음{end_date}a94",
        option="contents",
        start_date=start_date,
        end_date=end_date,
        fixture="dart_web/fulltext_zero.html",
    )

    positive: dict[str, Any] | None = None
    for index, query in enumerate(("주식매수청구권", "합병"), start=1):
        candidate = _search(
            client,
            query=query,
            option="contents",
            start_date=start_date,
            end_date=end_date,
            fixture=f"dart_web/fulltext_positive_{index}.html",
        )
        if positive is None or candidate["has_result_rows"]:
            positive = candidate
        if candidate["has_result_rows"]:
            break
    assert positive is not None

    withdrawal_seed = _search(
        client,
        query="철회신고서",
        option="contents",
        start_date=today.replace(year=today.year - 10).strftime("%Y%m%d"),
        end_date=end_date,
        fixture="dart_web/withdrawal_seed.html",
    )
    withdrawal_report_seed = _search(
        client,
        query="철회신고서",
        option="report",
        start_date=today.replace(year=today.year - 10).strftime("%Y%m%d"),
        end_date=end_date,
        fixture="dart_web/withdrawal_report_seed.html",
    )

    prefix_samples: dict[str, Any] = {}
    multi_prefix_rows: list[dict[str, Any]] = []
    for prefix in PREFIXES:
        result = _search(
            client,
            query=f'"[{prefix}]"',
            option="contents",
            start_date=today.replace(year=today.year - 10).strftime("%Y%m%d"),
            end_date=end_date,
            fixture=f"dart_web/prefix_{prefix}.html",
        )
        samples: list[dict[str, Any]] = []
        for row in result["rows"]:
            report_name, row_prefixes = official_prefixes_in_row(row)
            enriched = {**row, "report_name": report_name, "prefixes": row_prefixes}
            if prefix in row_prefixes:
                samples.append(enriched)
            if len(row_prefixes) > 1:
                multi_prefix_rows.append(enriched)
        prefix_samples[prefix] = {
            "classification": result["classification"],
            "result_count": result["result_count"],
            "fixture": result["fixture"],
            "samples": samples[:5],
        }

    synthetic_broken = root / "synthetic_structure_failure.html"
    synthetic_broken.parent.mkdir(parents=True, exist_ok=True)
    synthetic_broken.write_text(
        "<!doctype html><html lang=\"ko\"><title>synthetic</title><body><table></table></body></html>\n",
        encoding="utf-8",
    )
    synthetic_classification = parse_search_html(synthetic_broken.read_text(encoding="utf-8"))

    started_times = [record["started_at"] for record in client.records]
    parsed_starts = [datetime.fromisoformat(value) for value in started_times]
    observed_intervals = [
        round((current - previous).total_seconds(), 3)
        for previous, current in zip(parsed_starts, parsed_starts[1:])
    ]
    robots_text = robots.text
    findings = {
        "measured_at": client.records[-1]["started_at"],
        "request_count": len(client.records),
        "concurrency": 1,
        "configured_min_interval_seconds": min_interval,
        "observed_start_intervals_seconds": observed_intervals,
        "minimum_observed_start_interval_seconds": (
            min(observed_intervals) if observed_intervals else None
        ),
        "all_http_200": all(record["status"] == 200 for record in client.records),
        "request_started_at": started_times,
        "robots": {
            "fixture": robots.fixture,
            "raw": robots_text.strip(),
            "fulltext_path_explicitly_disallowed": any(
                "/dsab007" in line and line.lower().lstrip().startswith("disallow")
                for line in robots_text.splitlines()
            ),
            "viewer_path_disallowed": "/dsaf001/main.do" in robots_text,
        },
        "information_notice": {
            "fixture": notice.fixture,
            "mentions_accuracy_not_guaranteed": "정확성" in notice.text and "완전성" in notice.text,
        },
        "search_main": {"fixture": main.fixture, "http_status": main.status},
        "normal_zero": zero,
        "normal_results": positive,
        "withdrawal_seed": withdrawal_seed,
        "withdrawal_report_seed": withdrawal_report_seed,
        "structure_failure_test": {
            "actual_structure_failure_observed": False,
            "actual_result": "미확인",
            "synthetic_fixture": str(synthetic_broken.relative_to(fixture_root)),
            "synthetic_classification": synthetic_classification["classification"],
            "note": "실제 구조장애를 유발하지 않았으며 분류기 분기만 합성 fixture로 확인함",
        },
        "prefix_samples": prefix_samples,
        "multi_prefix_rows": _dedupe_rows(multi_prefix_rows)[:20],
    }
    write_json(root / "findings.json", findings)
    return findings


def _dedupe_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for row in rows:
        key = row.get("rcept_no") or row.get("text", "")
        if key and key not in seen:
            seen.add(key)
            result.append(row)
    return result
