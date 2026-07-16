from __future__ import annotations

import calendar
import html
import io
import json
import re
import zipfile
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from .common import RecordedHttpClient, read_json, utc_now, write_json
from .dart_web import PREFIXES


API_BASE = "https://opendart.fss.or.kr/api"
PREFIX_RE = re.compile(r"^\s*((?:\[[^\]]+\]\s*)+)")


def report_prefixes(report_name: str) -> list[str]:
    match = PREFIX_RE.match(report_name)
    return re.findall(r"\[([^\]]+)\]", match.group(1)) if match else []


def normalize_report_name(report_name: str) -> str:
    previous = report_name.strip()
    while True:
        current = PREFIX_RE.sub("", previous, count=1).strip()
        if current == previous:
            break
        previous = current
    return re.sub(r"\s+", "", previous)


def _safe_name(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z_.-]+", "_", value).strip("_")[:100]


def _date(value: str) -> date:
    return datetime.strptime(value, "%Y%m%d").date()


def _date_text(value: date) -> str:
    return value.strftime("%Y%m%d")


def _quarter_windows_back(end: date, count: int) -> list[tuple[date, date]]:
    windows: list[tuple[date, date]] = []
    cursor = end
    for _ in range(count):
        start_month = ((cursor.month - 1) // 3) * 3 + 1
        start = date(cursor.year, start_month, 1)
        windows.append((start, cursor))
        previous = start - timedelta(days=1)
        cursor = previous
    return windows


class OpenDartProbeClient:
    def __init__(
        self,
        api_key: str,
        fixture_root: Path,
        min_interval: float = 0.35,
    ) -> None:
        self.api_key = api_key
        self.fixture_root = fixture_root
        self.http = RecordedHttpClient(fixture_root, min_interval=min_interval)

    def list_page(self, params: dict[str, Any], fixture: str) -> dict[str, Any]:
        cached_path = self.fixture_root / fixture
        if cached_path.exists() and cached_path.stat().st_size:
            try:
                cached = json.loads(cached_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                cached = None
            if isinstance(cached, dict) and cached.get("status") in {"000", "013"}:
                return cached
        query = {
            "crtfc_key": self.api_key,
            "sort": "date",
            "sort_mth": "desc",
            "page_no": 1,
            "page_count": 100,
            **params,
        }
        response = self.http.request(
            "GET",
            f"{API_BASE}/list.json",
            params=query,
            fixture=fixture,
        )
        try:
            payload = json.loads(response.text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"OpenDART returned non-JSON response in {fixture}") from exc
        return payload

    def list_all(
        self,
        params: dict[str, Any],
        fixture_prefix: str,
        *,
        max_pages: int = 100,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        rows: list[dict[str, Any]] = []
        pages: list[dict[str, Any]] = []
        page_no = 1
        while page_no <= max_pages:
            payload = self.list_page(
                {**params, "page_no": page_no},
                f"{fixture_prefix}_page_{page_no:03d}.json",
            )
            pages.append(payload)
            status = payload.get("status")
            if status == "013":
                break
            if status != "000":
                raise RuntimeError(
                    f"OpenDART list failed with {status}: {payload.get('message')}"
                )
            rows.extend(payload.get("list") or [])
            total_page = int(payload.get("total_page") or 1)
            if page_no >= total_page:
                break
            page_no += 1
        return rows, pages

    def document(self, rcept_no: str, fixture: str) -> bytes:
        cached_path = self.fixture_root / fixture
        if cached_path.exists() and cached_path.stat().st_size:
            return cached_path.read_bytes()
        response = self.http.request(
            "GET",
            f"{API_BASE}/document.xml",
            params={"crtfc_key": self.api_key, "rcept_no": rcept_no},
            fixture=fixture,
            timeout=120,
        )
        return response.body

    def corp_codes(self) -> list[dict[str, str]]:
        fixture = "opendart/corp_codes/corpCode.zip"
        cached_path = self.fixture_root / fixture
        if cached_path.exists() and cached_path.stat().st_size:
            body = cached_path.read_bytes()
        else:
            body = self.http.request(
                "GET",
                f"{API_BASE}/corpCode.xml",
                params={"crtfc_key": self.api_key},
                fixture=fixture,
                timeout=120,
            ).body
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            xml_name = next(name for name in archive.namelist() if name.lower().endswith(".xml"))
            root = ET.fromstring(archive.read(xml_name))
        return [
            {
                "corp_code": (node.findtext("corp_code") or "").strip(),
                "corp_name": (node.findtext("corp_name") or "").strip(),
                "stock_code": (node.findtext("stock_code") or "").strip(),
            }
            for node in root.findall("list")
        ]


def _tag_source(rows: Iterable[dict[str, Any]], pblntf_ty: str) -> list[dict[str, Any]]:
    return [{**row, "_source_pblntf_ty": pblntf_ty} for row in rows]


def _chain_groups(rows: Iterable[dict[str, Any]]) -> dict[tuple[str, str], list[dict[str, Any]]]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[(row.get("corp_code", ""), normalize_report_name(row.get("report_nm", "")))].append(row)
    for members in groups.values():
        members.sort(key=lambda item: (item.get("rcept_dt", ""), item.get("rcept_no", "")))
    return groups


def _candidate_chains(rows: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    candidates: list[list[dict[str, Any]]] = []
    for members in _event_clusters(rows):
        if (
            len(members) >= 3
            and not report_prefixes(members[0].get("report_nm", ""))
            and sum("정" in (item.get("rm") or "") for item in members[:-1]) >= 2
            and sum(bool(report_prefixes(item.get("report_nm", ""))) for item in members[1:]) >= 2
        ):
            candidates.append(members)
    candidates.sort(key=lambda members: members[-1].get("rcept_dt", ""), reverse=True)
    return candidates


def _event_clusters(rows: Iterable[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    clusters: list[list[dict[str, Any]]] = []
    for members in _chain_groups(rows).values():
        current: list[dict[str, Any]] = []
        for row in members:
            start_new = False
            if current:
                gap = (_date(row["rcept_dt"]) - _date(current[-1]["rcept_dt"])).days
                start_new = gap > 180 or (
                    not report_prefixes(row.get("report_nm", ""))
                    and "정" not in (current[-1].get("rm") or "")
                    and "철" not in (current[-1].get("rm") or "")
                )
            if start_new:
                clusters.append(current)
                current = []
            current.append(row)
        if current:
            clusters.append(current)
    return clusters


def discover_amendment_rows(
    client: OpenDartProbeClient,
    *,
    max_quarters: int = 24,
) -> list[dict[str, Any]]:
    all_rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    types = ("C", "B", "E", "H", "D")
    for window_index, (start, end) in enumerate(
        _quarter_windows_back(date.today(), max_quarters), start=1
    ):
        for pblntf_ty in types:
            prefix = (
                f"opendart/discovery/{window_index:02d}_{_date_text(start)}_"
                f"{_date_text(end)}_{pblntf_ty}"
            )
            rows, _ = client.list_all(
                {
                    "bgn_de": _date_text(start),
                    "end_de": _date_text(end),
                    "last_reprt_at": "N",
                    "pblntf_ty": pblntf_ty,
                },
                prefix,
                max_pages=60,
            )
            for row in _tag_source(rows, pblntf_ty):
                rcept_no = row.get("rcept_no", "")
                if rcept_no and rcept_no not in seen:
                    seen.add(rcept_no)
                    all_rows.append(row)

            flag_events = _distinct_flag_events(all_rows)
            if len(flag_events) >= 30 and len(_candidate_chains(all_rows)) >= 3:
                return all_rows

        flag_events = _distinct_flag_events(all_rows)
        if len(flag_events) >= 30 and len(_candidate_chains(all_rows)) >= 3:
            break
    return all_rows


def enrich_with_withdrawal_rows(
    client: OpenDartProbeClient,
    rows: list[dict[str, Any]],
    web_findings: dict[str, Any] | None,
    corp_codes: list[dict[str, str]],
) -> list[dict[str, Any]]:
    if not web_findings:
        return rows
    seeds = [
        seed
        for seed in web_findings.get("withdrawal_report_seed", {}).get("rows", [])
        if seed.get("rcept_no")
        and seed.get("cells")
        and "철회신고서" in seed["cells"][0]
    ][:3]
    matched_seeds: list[dict[str, Any]] = []
    for index, seed in enumerate(seeds, start=1):
        corp = _match_web_corp(seed, corp_codes)
        if not corp:
            continue
        rcept_dt = seed["rcept_no"][:8]
        day_rows, _ = client.list_all(
            {
                "corp_code": corp["corp_code"],
                "bgn_de": rcept_dt,
                "end_de": rcept_dt,
                "last_reprt_at": "N",
            },
            f"opendart/withdrawal_seeds/seed_{index:02d}_{corp['corp_code']}",
            max_pages=5,
        )
        matched_seeds.extend(
            row for row in day_rows if row.get("rcept_no") == seed["rcept_no"]
        )

    additions: list[dict[str, Any]] = []
    for index, seed in enumerate(matched_seeds[:10], start=1):
        center = _date(seed["rcept_dt"])
        corp_rows, _ = client.list_all(
            {
                "corp_code": seed["corp_code"],
                "bgn_de": _date_text(center - timedelta(days=365)),
                "end_de": _date_text(center + timedelta(days=30)),
                "last_reprt_at": "N",
            },
            f"opendart/withdrawal_seeds/corp_{index:02d}_{seed['corp_code']}",
            max_pages=30,
        )
        additions.extend(_tag_source(corp_rows, ""))

    seen = {row.get("rcept_no") for row in rows}
    rows.extend(row for row in additions if row.get("rcept_no") not in seen)
    return rows


def discover_withdrawal_flag_rows(
    client: OpenDartProbeClient,
    *,
    max_quarters: int = 12,
    max_pages_per_window: int = 20,
) -> list[dict[str, Any]]:
    collected: list[dict[str, Any]] = []
    for window_index, (start, end) in enumerate(
        _quarter_windows_back(date.today(), max_quarters), start=1
    ):
        for pblntf_ty in ("G", "C"):
            for page_no in range(1, max_pages_per_window + 1):
                payload = client.list_page(
                    {
                        "bgn_de": _date_text(start),
                        "end_de": _date_text(end),
                        "last_reprt_at": "N",
                        "pblntf_ty": pblntf_ty,
                        "page_no": page_no,
                        "page_count": 100,
                    },
                    (
                        f"opendart/withdrawal_scan/{window_index:02d}_"
                        f"{_date_text(start)}_{_date_text(end)}_{pblntf_ty}_page_{page_no:03d}.json"
                    ),
                )
                if payload.get("status") == "013":
                    break
                page_rows = _tag_source(payload.get("list") or [], pblntf_ty)
                collected.extend(page_rows)
                if any("철" in (row.get("rm") or "") for row in page_rows):
                    return collected
                if page_no >= int(payload.get("total_page") or 1):
                    break
    return collected


def _match_web_corp(
    web_row: dict[str, Any], corp_codes: list[dict[str, str]]
) -> dict[str, str] | None:
    leading_cells = " ".join((web_row.get("cells") or [""])[:3])
    matches = [
        record
        for record in corp_codes
        if record["corp_name"] and record["corp_name"] in leading_cells
    ]
    return max(matches, key=lambda record: len(record["corp_name"])) if matches else None


def compare_last_report(
    client: OpenDartProbeClient,
    discovered_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    comparisons: list[dict[str, Any]] = []
    for index, seed_members in enumerate(_candidate_chains(discovered_rows)[:3], start=1):
        first_day = _date(seed_members[0]["rcept_dt"]) - timedelta(days=30)
        last_day = _date(seed_members[-1]["rcept_dt"]) + timedelta(days=30)
        base_params = {
            "corp_code": seed_members[0]["corp_code"],
            "bgn_de": _date_text(first_day),
            "end_de": _date_text(last_day),
            "pblntf_ty": seed_members[0]["_source_pblntf_ty"],
        }
        n_rows, _ = client.list_all(
            {**base_params, "last_reprt_at": "N"},
            f"opendart/last_report/case_{index}_N",
            max_pages=20,
        )
        y_rows, _ = client.list_all(
            {**base_params, "last_reprt_at": "Y"},
            f"opendart/last_report/case_{index}_Y",
            max_pages=20,
        )
        normalized = normalize_report_name(seed_members[0]["report_nm"])
        seed_receipts = {row["rcept_no"] for row in seed_members}
        n_chain = [row for row in n_rows if row.get("rcept_no") in seed_receipts]
        y_chain = [row for row in y_rows if row.get("rcept_no") in seed_receipts]
        same_name_y_outside_event = [
            row
            for row in y_rows
            if normalize_report_name(row.get("report_nm", "")) == normalized
            and row.get("rcept_no") not in seed_receipts
        ]
        n_chain.sort(key=lambda row: (row.get("rcept_dt", ""), row.get("rcept_no", "")))
        y_chain.sort(key=lambda row: (row.get("rcept_dt", ""), row.get("rcept_no", "")))
        n_receipts = [row["rcept_no"] for row in n_chain]
        y_receipts = [row["rcept_no"] for row in y_chain]
        comparisons.append(
            {
                "case": index,
                "corp_code": seed_members[0]["corp_code"],
                "corp_name": seed_members[0]["corp_name"],
                "normalized_report_name": normalized,
                "period": {"bgn_de": base_params["bgn_de"], "end_de": base_params["end_de"]},
                "pblntf_ty": base_params["pblntf_ty"],
                "N": n_chain,
                "Y": y_chain,
                "N_receipts": n_receipts,
                "Y_receipts": y_receipts,
                "same_name_Y_rows_outside_event": same_name_y_outside_event,
                "N_contains_original_intermediate_final": (
                    len(n_chain) >= 3
                    and not report_prefixes(n_chain[0].get("report_nm", ""))
                    and len(report_prefixes(n_chain[-1].get("report_nm", ""))) >= 1
                ),
                "Y_is_only_latest_N_receipt": (
                    len(y_receipts) == 1 and bool(n_receipts) and y_receipts[0] == n_receipts[-1]
                ),
                "raw_fixtures": [
                    f"opendart/last_report/case_{index}_N_page_001.json",
                    f"opendart/last_report/case_{index}_Y_page_001.json",
                ],
            }
        )
    return comparisons


def _distinct_flag_events(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for cluster in _event_clusters(rows):
        flagged = [row for row in cluster if any(flag in (row.get("rm") or "") for flag in ("철", "정"))]
        if not flagged:
            continue
        flag = "철" if any("철" in (row.get("rm") or "") for row in flagged) else "정"
        source = next(row for row in flagged if flag in (row.get("rm") or ""))
        events.append({**source, "_flag": flag})
    events.sort(key=lambda item: item.get("rcept_dt", ""), reverse=True)
    withdrawals = [row for row in events if row["_flag"] == "철"]
    corrections = [row for row in events if row["_flag"] == "정"]
    return withdrawals[:10] + corrections[: max(0, 30 - min(10, len(withdrawals)))]


def verify_rm_flags(
    client: OpenDartProbeClient,
    discovered_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    verified_events: list[dict[str, Any]] = []
    for index, event in enumerate(_distinct_flag_events(discovered_rows)[:30], start=1):
        center = _date(event["rcept_dt"])
        start = center - timedelta(days=180)
        end = center + timedelta(days=180)
        fixture_prefix = (
            f"opendart/rm_validation/event_{index:02d}_{event['rcept_no']}"
        )
        related_rows, _ = client.list_all(
            {
                "corp_code": event["corp_code"],
                "bgn_de": _date_text(start),
                "end_de": _date_text(end),
                "last_reprt_at": "N",
                "pblntf_ty": event["_source_pblntf_ty"],
            },
            fixture_prefix,
            max_pages=20,
        )
        flag = event["_flag"]
        if flag == "정":
            base = normalize_report_name(event["report_nm"])
            evidence = [
                row
                for row in related_rows
                if row.get("rcept_no") != event["rcept_no"]
                and normalize_report_name(row.get("report_nm", "")) == base
                and (row.get("rcept_dt", ""), row.get("rcept_no", ""))
                > (event.get("rcept_dt", ""), event.get("rcept_no", ""))
            ]
        else:
            evidence = [
                row
                for row in related_rows
                if row.get("rcept_no") != event["rcept_no"]
                and "철회" in row.get("report_nm", "")
            ]
        verified_events.append(
            {
                "event": index,
                "flag": flag,
                "source": event,
                "matched": bool(evidence),
                "evidence": evidence[:10],
                "raw_fixture": f"{fixture_prefix}_page_001.json",
                "method": (
                    "동일 회사·정규화 보고서명의 후속 접수 확인"
                    if flag == "정"
                    else "동일 회사·근접기간의 철회/철회간주 보고서 확인"
                ),
            }
        )
    return verified_events


def probe_status_013(client: OpenDartProbeClient) -> dict[str, Any]:
    payload = client.list_page(
        {
            "bgn_de": "19990101",
            "end_de": "19990101",
            "last_reprt_at": "N",
            "pblntf_detail_ty": "D004",
            "page_no": 1,
            "page_count": 100,
        },
        "opendart/status/status_013_empty_window.json",
    )
    return {
        "request_condition": {
            "bgn_de": "19990101",
            "end_de": "19990101",
            "pblntf_detail_ty": "D004",
        },
        "status": payload.get("status"),
        "message": payload.get("message"),
        "has_list": bool(payload.get("list")),
        "normal_no_data_confirmed": payload.get("status") == "013" and not payload.get("list"),
        "fixture": "opendart/status/status_013_empty_window.json",
    }


def _decode_zip_text(body: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        parts: list[str] = []
        for info in archive.infolist():
            if info.is_dir() or info.file_size > 30_000_000:
                continue
            raw = archive.read(info)
            decoded = None
            for encoding in ("utf-8", "euc-kr", "cp949"):
                try:
                    decoded = raw.decode(encoding)
                    break
                except UnicodeDecodeError:
                    pass
            if decoded is not None:
                parts.append(decoded)
    plain = html.unescape(re.sub(r"<[^>]+>", " ", "\n".join(parts)))
    return re.sub(r"\s+", " ", plain).strip()


def _target_snippet(text: str, corp_name: str, radius: int = 600) -> tuple[str, bool]:
    label_positions = [match.start() for match in re.finditer("대상회사", text)]
    corp_positions = [match.start() for match in re.finditer(re.escape(corp_name), text)]
    if not corp_positions:
        return "", False
    best_corp = corp_positions[0]
    verified = False
    if label_positions:
        label, best_corp = min(
            ((label, corp) for label in label_positions for corp in corp_positions),
            key=lambda pair: abs(pair[0] - pair[1]),
        )
        verified = abs(label - best_corp) <= 3000
        center = (label + best_corp) // 2
    else:
        center = best_corp
    return text[max(0, center - radius) : center + radius], verified


def probe_d004(
    client: OpenDartProbeClient,
    *,
    max_quarters: int = 40,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, (start, end) in enumerate(_quarter_windows_back(date.today(), max_quarters), start=1):
        rows, _ = client.list_all(
            {
                "bgn_de": _date_text(start),
                "end_de": _date_text(end),
                "last_reprt_at": "N",
                "pblntf_detail_ty": "D004",
            },
            f"opendart/d004/window_{index:02d}_{_date_text(start)}_{_date_text(end)}",
            max_pages=20,
        )
        for row in rows:
            if row.get("corp_name") == row.get("flr_nm"):
                continue
            if row.get("rcept_no") in seen:
                continue
            seen.add(row["rcept_no"])
            candidates.append(row)
        if len(candidates) >= 10:
            break

    results: list[dict[str, Any]] = []
    for row in candidates[:10]:
        rcept_no = row["rcept_no"]
        fixture = f"opendart/d004/documents/{rcept_no}.zip"
        body = client.document(rcept_no, fixture)
        is_zip = body.startswith(b"PK")
        snippet = ""
        verified = False
        error = None
        if is_zip:
            try:
                text = _decode_zip_text(body)
                snippet, verified = _target_snippet(text, row["corp_name"])
            except (zipfile.BadZipFile, RuntimeError, ValueError) as exc:
                error = f"{type(exc).__name__}: {exc}"
        else:
            error = "document.xml response was not a ZIP archive"
        snippet_path = client.fixture_root / f"opendart/d004/snippets/{rcept_no}.txt"
        snippet_path.parent.mkdir(parents=True, exist_ok=True)
        snippet_path.write_text(snippet + "\n", encoding="utf-8")
        results.append(
            {
                "list_row": row,
                "corp_flr_different": row.get("corp_name") != row.get("flr_nm"),
                "document_fixture": fixture,
                "snippet_fixture": str(snippet_path.relative_to(client.fixture_root)),
                "corp_name_near_target_company_label": verified,
                "verification": "원문에서 corp_name과 '대상회사' 표기의 근접 여부",
                "error": error,
            }
        )
    return results


def _web_prefix_receipts(web_findings: dict[str, Any] | None) -> dict[str, dict[str, str]]:
    found: dict[str, dict[str, str]] = {}
    if not web_findings:
        return found
    for prefix, result in web_findings.get("prefix_samples", {}).items():
        for sample in result.get("samples", []):
            receipt = sample.get("rcept_no")
            match = re.search(r"(?<!\d)(20\d{6})(?!\d)", sample.get("text", ""))
            if receipt:
                found[prefix] = {
                    "rcept_no": receipt,
                    "rcept_dt": match.group(1) if match else receipt[:8],
                }
                break
    return found


def probe_prefixes(
    client: OpenDartProbeClient,
    discovered_rows: list[dict[str, Any]],
    web_findings: dict[str, Any] | None,
    corp_codes: list[dict[str, str]],
) -> dict[str, Any]:
    samples: dict[str, list[dict[str, Any]]] = {prefix: [] for prefix in PREFIXES}
    all_rows: dict[str, dict[str, Any]] = {row["rcept_no"]: row for row in discovered_rows}
    targeted = _targeted_prefix_rows(client, all_rows.values(), corp_codes)
    all_rows.update({row["rcept_no"]: row for row in targeted})
    observed = {
        prefix
        for row in all_rows.values()
        for prefix in report_prefixes(row.get("report_nm", ""))
    }
    wanted_all = _web_prefix_receipts(web_findings)
    wanted = {prefix: value for prefix, value in wanted_all.items() if prefix not in observed}
    for index, (prefix, value) in enumerate(wanted.items(), start=1):
        web_sample = next(
            (
                sample
                for sample in (web_findings or {}).get("prefix_samples", {}).get(prefix, {}).get("samples", [])
                if sample.get("rcept_no") == value["rcept_no"]
            ),
            None,
        )
        corp = _match_web_corp(web_sample or {}, corp_codes)
        if not corp:
            continue
        rcept_dt = value["rcept_dt"]
        rows, _ = client.list_all(
            {
                "corp_code": corp["corp_code"],
                "bgn_de": rcept_dt,
                "end_de": rcept_dt,
                "last_reprt_at": "N",
            },
            f"opendart/prefixes/missing_{index:02d}_{_safe_name(prefix)}",
            max_pages=5,
        )
        for row in rows:
            if row.get("rcept_no") == value["rcept_no"]:
                all_rows[row["rcept_no"]] = row

    multi_prefix: list[dict[str, Any]] = []
    for row in all_rows.values():
        prefixes = report_prefixes(row.get("report_nm", ""))
        for prefix in PREFIXES:
            if prefix in prefixes and len(samples[prefix]) < 5:
                samples[prefix].append(row)
        if len(prefixes) > 1:
            multi_prefix.append({**row, "parsed_prefixes": prefixes})

    return {
        "samples": samples,
        "counts": {prefix: len(rows) for prefix, rows in samples.items()},
        "all_eight_observed": all(samples[prefix] for prefix in PREFIXES),
        "multi_prefix_observed": bool(multi_prefix),
        "multi_prefix_samples": multi_prefix[:20],
        "web_receipts_requested": wanted,
        "prefixes_already_observed_in_discovery": sorted(observed),
    }


def _targeted_prefix_rows(
    client: OpenDartProbeClient,
    existing_rows: Iterable[dict[str, Any]],
    corp_codes: list[dict[str, str]],
) -> list[dict[str, Any]]:
    observed = {
        prefix
        for row in existing_rows
        for prefix in report_prefixes(row.get("report_nm", ""))
    }
    found: list[dict[str, Any]] = []

    # `연장결정`은 E002(증권신고)가 아니라 B 계열 주요사항보고서에서
    # 실제로 관측된다. 알려진 회사/공시일을 좁은 창으로 다시 호출해
    # 대규모 페이지 스캔 없이 재현 가능한 원본 JSON을 남긴다.
    if "연장결정" not in observed:
        anchors = (
            ("풍강", "20260106"),
            ("케이엘넷", "20251017"),
            ("우성", "20251024"),
        )
        by_name = {record["corp_name"]: record for record in corp_codes}
        for index, (corp_name, rcept_dt) in enumerate(anchors, start=1):
            corp = by_name.get(corp_name)
            if not corp:
                continue
            rows, _ = client.list_all(
                {
                    "corp_code": corp["corp_code"],
                    "bgn_de": rcept_dt,
                    "end_de": rcept_dt,
                    "last_reprt_at": "N",
                    "pblntf_ty": "B",
                },
                f"opendart/prefixes/target_연장결정_anchor_{index:02d}",
                max_pages=3,
            )
            found.extend(rows)
            if any(
                "연장결정" in report_prefixes(row.get("report_nm", ""))
                for row in rows
            ):
                observed.add("연장결정")
                break

    targets: dict[str, dict[str, str]] = {
        "변경등록": {"pblntf_ty": "H"},
        "연장결정": {"pblntf_ty": "B"},
    }
    for prefix, filter_params in targets.items():
        if prefix in observed:
            continue
        matched = False
        for window_index, (start, end) in enumerate(
            _quarter_windows_back(date.today(), 40), start=1
        ):
            for page_no in range(1, 11):
                payload = client.list_page(
                    {
                        "bgn_de": _date_text(start),
                        "end_de": _date_text(end),
                        "last_reprt_at": "N",
                        "page_no": page_no,
                        "page_count": 100,
                        **filter_params,
                    },
                    (
                        f"opendart/prefixes/target_{_safe_name(prefix)}_"
                        f"{window_index:02d}_page_{page_no:03d}.json"
                    ),
                )
                if payload.get("status") == "013":
                    break
                page_rows = payload.get("list") or []
                found.extend(page_rows)
                if any(prefix in report_prefixes(row.get("report_nm", "")) for row in page_rows):
                    matched = True
                    break
                if page_no >= int(payload.get("total_page") or 1):
                    break
            if matched:
                break
    return found


def run_opendart_probe(
    api_key: str,
    fixture_root: Path,
    *,
    web_findings: dict[str, Any] | None = None,
    min_interval: float = 0.35,
) -> dict[str, Any]:
    previous_findings = read_json(fixture_root / "opendart/findings.json") or {}
    client = OpenDartProbeClient(api_key, fixture_root, min_interval=min_interval)
    status_013 = probe_status_013(client)
    corp_codes = client.corp_codes()
    discovered = discover_amendment_rows(client)
    discovered = enrich_with_withdrawal_rows(client, discovered, web_findings, corp_codes)
    if not any("철" in (row.get("rm") or "") for row in discovered):
        withdrawal_rows = discover_withdrawal_flag_rows(client)
        seen_receipts = {row.get("rcept_no") for row in discovered}
        discovered.extend(
            row for row in withdrawal_rows if row.get("rcept_no") not in seen_receipts
        )
    last_report = compare_last_report(client, discovered)
    rm_validation = verify_rm_flags(client, discovered)
    prefixes = probe_prefixes(client, discovered, web_findings, corp_codes)
    d004 = probe_d004(client)

    matched = sum(1 for event in rm_validation if event["matched"])
    network_request_count = len(client.http.records)
    findings = {
        "measured_at": (
            client.http.records[-1]["started_at"]
            if client.http.records
            else previous_findings.get("measured_at", utc_now())
        ),
        "request_count": (
            network_request_count
            if network_request_count
            else previous_findings.get("request_count", 0)
        ),
        "network_requests_this_run": network_request_count,
        "cached_fixture_reuse": network_request_count == 0,
        "configured_min_interval_seconds": min_interval,
        "concurrency": 1,
        "api_key_source": "environment",
        "api_key_in_logs": "***MASKED***",
        "status_013": status_013,
        "last_reprt_comparisons": last_report,
        "last_reprt_case_count": len(last_report),
        "last_reprt_all_match_plan": (
            len(last_report) >= 3
            and all(case["N_contains_original_intermediate_final"] for case in last_report)
            and all(case["Y_is_only_latest_N_receipt"] for case in last_report)
        ),
        "rm_validation": rm_validation,
        "rm_event_count": len(rm_validation),
        "rm_matched_count": matched,
        "rm_match_rate": (matched / len(rm_validation)) if rm_validation else None,
        "rm_flags_observed": sorted({event["flag"] for event in rm_validation}),
        "prefixes": prefixes,
        "d004": d004,
        "d004_case_count": len(d004),
        "d004_verified_count": sum(
            1 for case in d004 if case["corp_name_near_target_company_label"]
        ),
        "discovery_row_count": len(discovered),
    }
    write_json(fixture_root / "opendart/findings.json", findings)
    return findings
