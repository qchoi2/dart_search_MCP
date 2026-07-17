"""Bounded live probe for DART server-side session lifecycle signals."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Callable

from app.channels.dart_fulltext import DART_BASE, MODE_ENDPOINTS, DartFulltextClient, parse_search_html
from app.http_client import HttpClient, HttpResponse

SESSION_PROBE_USER_AGENT = (
    "dart-search-mcp-session-lifecycle-probe/0.1 "
    "(local contract measurement; concurrency=1; max_requests=20)"
)
MAX_REQUESTS = 20
MIN_START_INTERVAL_SECONDS = 1.0


@dataclass(frozen=True, slots=True)
class ProbeObservation:
    step: str
    method: str
    endpoint: str
    status: int
    body_sha256: str
    body_bytes: int
    classification: str
    search_count: int | None
    row_count: int
    set_cookie_header_present: bool
    cookie_count_after: int


class SessionLifecycleProbe:
    def __init__(
        self,
        *,
        http: HttpClient | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        self.http = http or HttpClient(user_agent=SESSION_PROBE_USER_AGENT)
        self.clock = clock
        self.sleeper = sleeper
        self.request_count = 0
        self.started_at: list[float] = []

    def _request(self, step: str, method: str, endpoint: str, **kwargs) -> ProbeObservation:
        if self.request_count >= MAX_REQUESTS:
            raise RuntimeError("session lifecycle probe request cap reached")
        now = self.clock()
        if self.started_at:
            wait = MIN_START_INTERVAL_SECONDS - (now - self.started_at[-1])
            if wait > 0:
                self.sleeper(wait)
        self.started_at.append(self.clock())
        self.request_count += 1
        response = self.http.request(method, f"{DART_BASE}{endpoint}", max_retries=0, **kwargs)
        return self._observation(step, method, endpoint, response)

    def _observation(self, step: str, method: str, endpoint: str, response: HttpResponse) -> ProbeObservation:
        parsed = parse_search_html(response.body.decode("utf-8", errors="replace")) if endpoint.endswith("search.ax") else None
        headers = {key.casefold(): value for key, value in response.headers.items()}
        cookie_jar = getattr(self.http, "_cookie_jar", ())
        return ProbeObservation(
            step=step,
            method=method,
            endpoint=endpoint,
            status=response.status,
            body_sha256=hashlib.sha256(response.body).hexdigest(),
            body_bytes=len(response.body),
            classification=parsed.classification if parsed else "not_search_response",
            search_count=parsed.search_count if parsed else None,
            row_count=len(parsed.rows) if parsed else 0,
            set_cookie_header_present="set-cookie" in headers,
            cookie_count_after=sum(1 for _ in cookie_jar),
        )

    def run(self) -> dict:
        form_a = DartFulltextClient._form("상계납입", date(2026, 1, 1), date(2026, 7, 17), "contents", 1)
        form_b = DartFulltextClient._form("출자전환", date(2026, 1, 1), date(2026, 7, 17), "contents", 1)
        referer = {"Referer": f"{DART_BASE}/dsab007/main.do", "X-Requested-With": "XMLHttpRequest"}
        observations = [
            self._request("new_session_health", "GET", "/dsab007/main.do"),
            self._request("initial_mode_setup", "POST", f"/dsab007/{MODE_ENDPOINTS['contents']}", form=form_a, headers=referer),
            self._request("initial_search", "POST", "/dsab007/search.ax", form=form_a, headers=referer),
            self._request("same_session_keyword_switch", "POST", "/dsab007/search.ax", form=form_b, headers=referer),
        ]
        self.http.recreate_cookie_jar()
        observations.extend([
            self._request("new_cookie_jar_direct_search", "POST", "/dsab007/search.ax", form=form_a, headers=referer),
            self._request("new_cookie_jar_mode_setup", "POST", f"/dsab007/{MODE_ENDPOINTS['contents']}", form=form_a, headers=referer),
            self._request("new_cookie_jar_search_after_setup", "POST", "/dsab007/search.ax", form=form_a, headers=referer),
        ])
        intervals_ms = [round((right - left) * 1000, 3) for left, right in zip(self.started_at, self.started_at[1:])]
        direct = next(item for item in observations if item.step == "new_cookie_jar_direct_search")
        recovered = next(item for item in observations if item.step == "new_cookie_jar_search_after_setup")
        return {
            "schema_version": 1,
            "probe": "dart_session_lifecycle",
            "constraints": {
                "max_requests": MAX_REQUESTS,
                "actual_requests": self.request_count,
                "concurrency": 1,
                "minimum_start_interval_ms": 1000,
                "http_retries": 0,
                "tls_certificate_verification": True,
                "tls_hostname_verification": True,
                "identifying_user_agent": SESSION_PROBE_USER_AGENT,
                "cookie_values_persisted": False,
                "api_key_used": False,
            },
            "observed_start_intervals_ms": intervals_ms,
            "observations": [asdict(item) for item in observations],
            "server_expiry_signal": {
                "status": "unconfirmed",
                "reason": "The bounded probe does not wait for or force an actual server expiry.",
            },
            "cookie_replacement_signal": {
                "status": "observed_not_unique" if direct.classification != recovered.classification else "unconfirmed",
                "direct_search_classification": direct.classification,
                "after_mode_setup_classification": recovered.classification,
                "automatic_detection_eligible": False,
            },
            "automatic_server_expiry_detection": "not_implemented",
        }


def write_probe_result(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
