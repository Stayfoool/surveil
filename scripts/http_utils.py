"""Shared HTTP helpers for monitor fetchers."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from threading import Lock
from typing import Mapping

import httpx


DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

_CLIENT: httpx.Client | None = None
_CLIENT_KEY: tuple[str, str, float] | None = None
_CLIENT_LOCK = Lock()


@dataclass(frozen=True)
class HttpResponse:
    status_code: int
    url: str
    headers: httpx.Headers
    content: bytes


def default_user_agent() -> str:
    return os.getenv("SURVEIL_USER_AGENT", "").strip() or DEFAULT_USER_AGENT


def default_proxy() -> str:
    return (
        os.getenv("SURVEIL_HTTP_PROXY", "").strip()
        or os.getenv("HTTPS_PROXY", "").strip()
        or os.getenv("HTTP_PROXY", "").strip()
    )


def get_http_client(timeout: float | None = None) -> httpx.Client:
    global _CLIENT, _CLIENT_KEY
    proxy = default_proxy()
    user_agent = default_user_agent()
    timeout_value = timeout or float(os.getenv("SURVEIL_HTTP_TIMEOUT_SECONDS", "20") or "20")
    key = (proxy, user_agent, timeout_value)
    with _CLIENT_LOCK:
        if _CLIENT is not None and _CLIENT_KEY == key:
            return _CLIENT
        if _CLIENT is not None:
            _CLIENT.close()
        kwargs = {
            "headers": {"User-Agent": user_agent},
            "timeout": httpx.Timeout(timeout_value),
            "follow_redirects": True,
            "http2": True,
            "trust_env": not bool(proxy),
        }
        if proxy:
            kwargs["proxy"] = proxy
        _CLIENT = httpx.Client(**kwargs)
        _CLIENT_KEY = key
        return _CLIENT


def retry_count(default: int = 2) -> int:
    raw = os.getenv("SURVEIL_HTTP_RETRY_COUNT", "").strip()
    try:
        return max(0, min(5, int(raw))) if raw else default
    except ValueError:
        return default


def retry_sleep(attempt: int) -> float:
    raw = os.getenv("SURVEIL_HTTP_RETRY_BACKOFF_SECONDS", "").strip()
    try:
        base = float(raw) if raw else 2.0
    except ValueError:
        base = 2.0
    return min(60.0, max(0.0, base) * (2**attempt))


def should_retry_status(status_code: int) -> bool:
    return status_code in {408, 425, 429, 500, 502, 503, 504}


def http_get(
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    timeout: float | None = None,
    retries: int | None = None,
) -> HttpResponse:
    attempts = (retry_count() if retries is None else max(0, retries)) + 1
    client = get_http_client(timeout)
    request_headers = dict(headers or {})
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            response = client.get(url, headers=request_headers)
            if should_retry_status(response.status_code) and attempt < attempts - 1:
                time.sleep(retry_sleep(attempt))
                continue
            if response.status_code != 304:
                response.raise_for_status()
            return HttpResponse(
                status_code=response.status_code,
                url=str(response.url),
                headers=response.headers,
                content=response.content,
            )
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
            last_error = exc
            if attempt >= attempts - 1:
                raise
            time.sleep(retry_sleep(attempt))
        except httpx.HTTPStatusError:
            raise
    raise RuntimeError(f"HTTP 请求失败：{last_error}")
