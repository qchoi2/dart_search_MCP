"""Shared strict-TLS HTTP client with bounded retries and redacted errors."""

from __future__ import annotations

import json
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
        self._opener = opener or urllib.request.urlopen
        self._sleep = sleeper
        self._context = ssl.create_default_context()

    def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, object] | None = None,
        form: Mapping[str, object] | None = None,
        headers: Mapping[str, str] | None = None,
        max_retries: int = HTTP_MAX_RETRIES,
    ) -> HttpResponse:
        if params:
            query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
            url += ("&" if "?" in url else "?") + query
        body = None if form is None else urllib.parse.urlencode(form).encode("utf-8")
        request_headers = {"User-Agent": self.user_agent, "Accept": "application/json, application/xml, text/html;q=0.9, */*;q=0.8"}
        request_headers.update(headers or {})
        request = urllib.request.Request(url, data=body, headers=request_headers, method=method.upper())
        for attempt in range(max_retries + 1):
            try:
                with self._opener(request, timeout=self.timeout, context=self._context) as response:
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
                if attempt >= max_retries:
                    raise SearchError(ErrorCode.OPENDART_TEMPORARY_FAILURE, "네트워크 연결에 일시적 문제가 있습니다.", retryable=True) from exc
            self._sleep(HTTP_BACKOFF_BASE_SECONDS * (2**attempt))
