from __future__ import annotations

import hashlib
import http.cookiejar
import json
import os
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping


MASK = "***MASKED***"
SECRET_KEYS = frozenset({"crtfc_key", "api_key", "dart_api_key"})
USER_AGENT = "dart-search-mcp-stage0-probe/0.1 (local measurement; concurrency=1)"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def sha256_bytes(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def mask_pairs(pairs: Iterable[tuple[str, Any]]) -> list[tuple[str, str]]:
    return [
        (key, MASK if key.lower() in SECRET_KEYS else str(value))
        for key, value in pairs
    ]


def masked_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    return urllib.parse.urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            urllib.parse.urlencode(mask_pairs(query), doseq=True),
            parsed.fragment,
        )
    )


def decode_body(body: bytes, content_type: str = "") -> str:
    candidates: list[str] = []
    if "charset=" in content_type.lower():
        candidates.append(content_type.lower().split("charset=", 1)[1].split(";", 1)[0].strip())
    candidates.extend(["utf-8", "euc-kr", "cp949"])
    for encoding in candidates:
        try:
            return body.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            pass
    return body.decode("utf-8", errors="replace")


def find_api_key() -> tuple[str | None, str | None]:
    names = ("DART_API_KEY", "OPENDART_API_KEY", "OPEN_DART_API_KEY", "CRTFC_KEY")
    for name in names:
        value = os.environ.get(name)
        if value:
            return value.strip(), name
    if os.name == "nt":
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
                for name in names:
                    try:
                        value, _ = winreg.QueryValueEx(key, name)
                    except FileNotFoundError:
                        continue
                    if value:
                        return str(value).strip(), f"Windows user environment:{name}"
        except (FileNotFoundError, OSError):
            pass
    return None, None


@dataclass(frozen=True)
class HttpResult:
    body: bytes
    status: int
    headers: Mapping[str, str]
    fixture: str | None
    record: Mapping[str, Any]

    @property
    def text(self) -> str:
        return decode_body(self.body, self.headers.get("content-type", ""))


class RecordedHttpClient:
    """Sequential, rate-limited HTTP client that records masked request metadata."""

    def __init__(self, fixture_root: Path, min_interval: float = 1.0) -> None:
        self.fixture_root = fixture_root
        self.fixture_root.mkdir(parents=True, exist_ok=True)
        self.min_interval = max(0.0, min_interval)
        self.cookie_jar = http.cookiejar.CookieJar()
        tls_context = ssl.create_default_context()
        self.tls_strict_flag_relaxed = False
        strict_flag = getattr(ssl, "VERIFY_X509_STRICT", 0)
        if strict_flag and tls_context.verify_flags & strict_flag:
            # DART's otherwise browser-valid chain is rejected by Python 3.14's
            # OpenSSL strict flag because a CA BasicConstraints extension is not
            # marked critical. Hostname and certificate-chain verification remain on.
            tls_context.verify_flags &= ~strict_flag
            self.tls_strict_flag_relaxed = True
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cookie_jar),
            urllib.request.HTTPSHandler(context=tls_context),
        )
        self._last_started: float | None = None
        self.records: list[dict[str, Any]] = []
        self.log_path = self.fixture_root / "requests.jsonl"

    def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | Iterable[tuple[str, Any]] | None = None,
        form: Mapping[str, Any] | Iterable[tuple[str, Any]] | None = None,
        headers: Mapping[str, str] | None = None,
        fixture: str | None = None,
        timeout: float = 60.0,
    ) -> HttpResult:
        query_pairs = _pairs(params)
        form_pairs = _pairs(form)
        if query_pairs:
            separator = "&" if urllib.parse.urlsplit(url).query else "?"
            url = url + separator + urllib.parse.urlencode(query_pairs, doseq=True)
        data = None
        if form is not None:
            data = urllib.parse.urlencode(form_pairs, doseq=True).encode("utf-8")

        now_mono = time.monotonic()
        if self._last_started is not None:
            remaining = self.min_interval - (now_mono - self._last_started)
            if remaining > 0:
                time.sleep(remaining)
        self._last_started = time.monotonic()
        started_at = utc_now()
        started_mono = time.monotonic()

        request_headers = {
            "User-Agent": USER_AGENT,
            "Accept": "*/*",
        }
        if form is not None:
            request_headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
            request_headers["X-Requested-With"] = "XMLHttpRequest"
        request_headers.update(headers or {})
        request = urllib.request.Request(url, data=data, headers=request_headers, method=method.upper())

        error: str | None = None
        try:
            with self.opener.open(request, timeout=timeout) as response:
                body = response.read()
                status = int(response.status)
                response_headers = {key.lower(): value for key, value in response.headers.items()}
        except urllib.error.HTTPError as exc:
            body = exc.read()
            status = int(exc.code)
            response_headers = {key.lower(): value for key, value in exc.headers.items()}
            error = f"HTTPError: {exc.reason}"
        except Exception as exc:
            body = b""
            status = 0
            response_headers = {}
            error = f"{type(exc).__name__}: {exc}"

        duration_ms = round((time.monotonic() - started_mono) * 1000, 1)
        fixture_path: Path | None = None
        if fixture:
            fixture_path = self.fixture_root / fixture
            fixture_path.parent.mkdir(parents=True, exist_ok=True)
            fixture_path.write_bytes(body)

        record: dict[str, Any] = {
            "started_at": started_at,
            "method": method.upper(),
            "url": masked_url(url),
            "form": dict(mask_pairs(form_pairs)) if form is not None else None,
            "request_headers": {
                key: value
                for key, value in request_headers.items()
                if key.lower() not in {"cookie", "authorization"}
            },
            "concurrency": 1,
            "configured_min_interval_seconds": self.min_interval,
            "tls_certificate_verification": True,
            "tls_python_x509_strict_flag_relaxed": self.tls_strict_flag_relaxed,
            "status": status,
            "duration_ms": duration_ms,
            "response_content_type": response_headers.get("content-type"),
            "response_bytes": len(body),
            "response_sha256": sha256_bytes(body),
            "fixture": str(fixture_path.relative_to(self.fixture_root)) if fixture_path else None,
            "error": error,
        }
        self.records.append(record)
        with self.log_path.open("a", encoding="utf-8") as log:
            log.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")

        if status == 0:
            raise RuntimeError(error or "HTTP request failed")
        return HttpResult(
            body=body,
            status=status,
            headers=response_headers,
            fixture=record["fixture"],
            record=record,
        )


def _pairs(
    value: Mapping[str, Any] | Iterable[tuple[str, Any]] | None,
) -> list[tuple[str, Any]]:
    if value is None:
        return []
    if isinstance(value, Mapping):
        items = value.items()
    else:
        items = value
    pairs: list[tuple[str, Any]] = []
    for key, item in items:
        if isinstance(item, (list, tuple)):
            pairs.extend((key, nested) for nested in item)
        elif item is not None:
            pairs.append((key, item))
    return pairs
