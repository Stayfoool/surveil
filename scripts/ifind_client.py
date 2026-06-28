"""Minimal iFinD REST client used by the remote monitor."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from env_utils import get_env


DEFAULT_BASE_URL = "https://quantapi.51ifind.com/api/v1"


class IfindError(RuntimeError):
    """Raised when iFinD returns an error or cannot be reached."""


class IfindNoDataError(IfindError):
    """Raised when iFinD returns a normal no-data response."""


@dataclass
class IfindClient:
    base_url: str
    refresh_token: str
    timeout: int = 30
    access_token: str = ""
    access_token_expire_at: float = 0.0

    @classmethod
    def from_env(cls) -> "IfindClient":
        base_url = get_env("IFIND_API_BASE_URL", default=DEFAULT_BASE_URL).rstrip("/")
        refresh_token = get_env("IFIND_REFRESH_TOKEN", "IFIND_API_KEY")
        access_token = get_env("IFIND_ACCESS_TOKEN")
        if not refresh_token and not access_token:
            raise IfindError("缺少 IFIND_REFRESH_TOKEN 或 IFIND_ACCESS_TOKEN")
        timeout_raw = os.getenv("IFIND_TIMEOUT_SECONDS", "").strip()
        try:
            timeout = max(5, int(timeout_raw)) if timeout_raw else 30
        except ValueError:
            timeout = 30
        return cls(base_url=base_url, refresh_token=refresh_token, access_token=access_token, timeout=timeout)

    def _url(self, endpoint: str) -> str:
        return f"{self.base_url.rstrip('/')}/{endpoint.lstrip('/')}"

    def _request(self, endpoint: str, payload: dict[str, Any] | None, headers: dict[str, str]) -> dict[str, Any]:
        data = b"{}" if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self._url(endpoint),
            data=data,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "surveil-ifind-client/0.1",
                **headers,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise IfindError(f"iFinD 请求失败：HTTP {exc.code} {endpoint}\n{body[:1000]}") from exc
        except urllib.error.URLError as exc:
            raise IfindError(f"iFinD 网络请求失败：{endpoint}: {exc}") from exc

        try:
            parsed = json.loads(body)
        except json.JSONDecodeError as exc:
            raise IfindError(f"iFinD 响应不是 JSON：{endpoint}\n{body[:1000]}") from exc
        if isinstance(parsed, dict) and str(parsed.get("errorcode", "0")) not in ("0", ""):
            if str(parsed.get("errorcode")) == "-4001" and str(parsed.get("errmsg", "")).lower() == "no data.":
                raise IfindNoDataError(f"iFinD 无数据：{endpoint}\n{body[:1000]}")
            raise IfindError(f"iFinD 返回错误：{endpoint}\n{body[:1000]}")
        return parsed

    def ensure_access_token(self) -> str:
        if self.access_token and time.time() < self.access_token_expire_at - 60:
            return self.access_token
        if self.access_token and not self.refresh_token:
            return self.access_token
        if not self.refresh_token:
            raise IfindError("缺少 IFIND_REFRESH_TOKEN，无法刷新 access token")
        response = self._request(
            "get_access_token",
            payload=None,
            headers={"refresh_token": self.refresh_token},
        )
        data = response.get("data") if isinstance(response, dict) else None
        if not isinstance(data, dict) or not data.get("access_token"):
            raise IfindError(f"iFinD access token 响应异常：{json.dumps(response, ensure_ascii=False)[:1000]}")
        self.access_token = str(data["access_token"])
        expires_in = data.get("expires_in") or data.get("expire_in") or data.get("expires")
        try:
            ttl = int(expires_in)
        except (TypeError, ValueError):
            ttl = 3600
        self.access_token_expire_at = time.time() + max(300, ttl)
        return self.access_token

    def post(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        token = self.ensure_access_token()
        return self._request(endpoint, payload=payload, headers={"access_token": token})

    def data_pool(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.post("data_pool", payload)

    def realtime_quotes(self, codes: str, indicators: str) -> dict[str, Any]:
        return self.post("real_time_quotation", {"codes": codes, "indicators": indicators})

    def history_quotes(
        self,
        codes: str,
        indicators: str,
        startdate: str,
        enddate: str,
        functionpara: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "codes": codes,
            "indicators": indicators,
            "startdate": startdate,
            "enddate": enddate,
        }
        if functionpara:
            payload["functionpara"] = functionpara
        return self.post("cmd_history_quotation", payload)

    def report_query(
        self,
        codes: str,
        begin_date: str,
        end_date: str,
        report_type: str = "901",
        outputpara: str = "reportDate:Y,thscode:Y,secName:Y,ctime:Y,reportTitle:Y,pdfURL:Y,seq:Y",
    ) -> dict[str, Any]:
        return self.post(
            "report_query",
            {
                "codes": codes,
                "functionpara": {"reportType": report_type},
                "beginrDate": begin_date,
                "endrDate": end_date,
                "outputpara": outputpara,
            },
        )

    def trade_dates(self, startdate: str, offset: int = -10, marketcode: str = "212001") -> dict[str, Any]:
        return self.post(
            "get_trade_dates",
            {
                "marketcode": marketcode,
                "functionpara": {
                    "dateType": "0",
                    "period": "D",
                    "offset": str(offset),
                    "dateFormat": "0",
                    "output": "sequencedate",
                },
                "startdate": startdate,
            },
        )


def mask_token(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "<redacted>"
    return f"{value[:4]}...{value[-4:]}"
