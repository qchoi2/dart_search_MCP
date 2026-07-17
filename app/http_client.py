"""Shared strict-TLS HTTP client with bounded retries and redacted errors."""

from __future__ import annotations

import json
import http.cookiejar
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable, Mapping

from app.config.defaults import (
    HTTP_BACKOFF_BASE_SECONDS,
    HTTP_MAX_RETRIES,
    HTTP_TIMEOUT_SECONDS,
    USER_AGENT,
)
from app.errors import ErrorCode, SearchError


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes
    url: str

    def json(self) -> dict:
        return json.loads(self.body.decode("utf-8-sig"))


@dataclass(slots=True)
class DeadlineBudget:
    """One monotonic hard deadline shared by every request in a search."""

    ends_at: float
    clock: Callable[[], float] = time.monotonic
    deadline_limited_timeout: bool = False
    request_start_blocked: bool = False
    backoff_blocked: bool = False

    def remaining(self) -> float:
        return self.ends_at - self.clock()

    def require_remaining(self, stage: str) -> float:
        remaining = self.remaining()
        if remaining <= 0:
            self.request_start_blocked = stage != "backoff"
            self.backoff_blocked = stage == "backoff"
            raise SearchError(
                ErrorCode.SEARCH_TIMEOUT_PARTIAL,
                "하드 시간예산이 끝나 새 네트워크 요청을 시작하지 않았습니다.",
                details={"stage": stage, "deadline_limited_timeout": self.deadline_limited_timeout},
            )
        return remaining

    def timeout_for(self, default_timeout: float) -> float:
        remaining = self.require_remaining("request_start")
        timeout = min(default_timeout, remaining)
        if timeout < default_timeout:
            self.deadline_limited_timeout = True
        return timeout


class HttpClient:
    def __init__(
        self,
        *,
        timeout: float = HTTP_TIMEOUT_SECONDS,
        user_agent: str = USER_AGENT,
        opener: Callable[..., object] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        self.timeout = timeout
        self.user_agent = user_agent
        self._context = ssl.create_default_context()
        self.tls_strict_flag_relaxed = False
        strict_flag = getattr(ssl, "VERIFY_X509_STRICT", 0)
        if strict_flag and self._context.verify_flags & strict_flag:
            # Python 3.14 rejects DART's otherwise browser-valid chain because
            # a CA BasicConstraints extension is not marked critical.  This is
            # the same compatibility setting used by the measured probe client;
            # certificate-chain and hostname verification remain mandatory.
            self._context.verify_flags &= ~strict_flag
            self.tls_strict_flag_relaxed = True
        self._custom_opener = opener
        self._cookie_jar = http.cookiejar.CookieJar()
        self.session_generation = 0
        self._opener = opener or self._build_session_opener()
        self._sleep = sleeper

    def _build_session_opener(self) -> Callable[..., object]:
        director = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self._cookie_jar),
            urllib.request.HTTPSHandler(context=self._context),
        )

        def session_open(request, *, timeout, context):
            del context
            return director.open(request, timeout=timeout)

        return session_open

    def recreate_cookie_jar(self) -> None:
        """Start a new HTTP session without inferring any server-side signal."""
        self._cookie_jar = http.cookiejar.CookieJar()
        self.session_generation += 1
        if self._custom_opener is None:
            self._opener = self._build_session_opener()

    reset_session = recreate_cookie_jar

    def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, object] | None = None,
        form: Mapping[str, object] | None = None,
        headers: Mapping[str, str] | None = None,
        max_retries: int = HTTP_MAX_RETRIES,
        deadline: DeadlineBudget | None = None,
    ) -> HttpResponse:
        if params:
            query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
            url += ("&" if "?" in url else "?") + query
        body = None if form is None else urllib.parse.urlencode(form).encode("utf-8")
        request_headers = {"User-Agent": self.user_agent, "Accept": "application/json, application/xml, text/html;q=0.9, */*;q=0.8"}
        request_headers.update(headers or {})
        request = urllib.request.Request(url, data=body, headers=request_headers, method=method.upper())
        for attempt in range(max_retries + 1):
            request_timeout = self.timeout if deadline is None else deadline.timeout_for(self.timeout)
            deadline_limited = bool(deadline and request_timeout < self.timeout)
            try:
                with self._opener(request, timeout=request_timeout, context=self._context) as response:
                    return HttpResponse(response.status, dict(response.headers), response.read(), response.geturl())
            except urllib.error.HTTPError as exc:
                if exc.code == 429:
                    code = ErrorCode.OPENDART_HTTP_RATE_LIMITED
                elif exc.code >= 500:
                    code = ErrorCode.OPENDART_TEMPORARY_FAILURE
                else:
                    raise SearchError(ErrorCode.OPENDART_TEMPORARY_FAILURE, f"HTTP 요청이 {exc.code} 상태로 거절되었습니다.") from exc
                if attempt >= max_retries:
                    raise SearchError(code, "외부 서비스가 일시적으로 응답하지 않습니다.", retryable=True) from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                if deadline_limited:
                    raise SearchError(
                        ErrorCode.SEARCH_TIMEOUT_PARTIAL,
                        "하드 시간예산으로 제한된 HTTP timeout에 도달했습니다.",
                        details={"stage": "request_timeout", "deadline_limited_timeout": True},
                    ) from exc
                if attempt >= max_retries:
                    raise SearchError(ErrorCode.OPENDART_TEMPORARY_FAILURE, "네트워크 연결에 일시적 문제가 있습니다.", retryable=True) from exc
            backoff = HTTP_BACKOFF_BASE_SECONDS * (2**attempt)
            if deadline is not None:
                remaining = deadline.require_remaining("backoff")
                if remaining <= backoff:
                    deadline.backoff_blocked = True
                    raise SearchError(
                        ErrorCode.SEARCH_TIMEOUT_PARTIAL,
                        "재시도 백오프 뒤 요청을 시작할 시간이 없어 부분 결과로 종료합니다.",
                        details={"stage": "backoff", "deadline_limited_timeout": deadline.deadline_limited_timeout},
                    )
            self._sleep(backoff)
            if deadline is not None:
                deadline.require_remaining("backoff")
