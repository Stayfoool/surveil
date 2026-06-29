#!/usr/bin/env python3
"""Sina Finance ZhiYan official API/MCP client.

OpenAPI direct access is the preferred production path for this project.
MCP/HTTP is kept only as a backup/verification path while Sina's logged-in
API detail page is needed to confirm the exact OpenAPI endpoint.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

import httpx

from http_utils import http_post


DEFAULT_MCP_URL = "http://mcp.finance.sina.com.cn/mcp-http"
DEFAULT_PROTOCOL_VERSION = "2025-03-26"


class SinaZyError(RuntimeError):
    pass


@dataclass
class SinaZyMcpClient:
    api_key: str
    base_url: str = DEFAULT_MCP_URL
    timeout: int = 20
    session_id: str = ""
    initialized: bool = False
    _next_id: int = field(default=1, init=False)

    @classmethod
    def from_env(cls) -> "SinaZyMcpClient":
        api_key = (
            os.getenv("SINA_ZY_API_KEY", "")
            or os.getenv("SINA_API_KEY", "")
            or os.getenv("SINA_FINANCE_API_KEY", "")
        ).strip()
        if not api_key:
            raise SinaZyError("缺少 SINA_ZY_API_KEY")
        return cls(
            api_key=api_key,
            base_url=os.getenv("SINA_ZY_MCP_URL", DEFAULT_MCP_URL).strip() or DEFAULT_MCP_URL,
            timeout=int(os.getenv("SINA_ZY_TIMEOUT_SECONDS", "20") or "20"),
        )

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "User-Agent": "surveil-sina-zy/0.1",
            "X-Auth-Token": self.api_key,
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        return headers

    def _parse_body(self, body: bytes, content_type: str) -> dict[str, Any]:
        text = body.decode("utf-8", errors="replace").strip()
        if not text:
            return {}
        if "text/event-stream" in content_type or text.startswith("event:"):
            payloads: list[str] = []
            for line in text.splitlines():
                if line.startswith("data:"):
                    payload = line.split(":", 1)[1].strip()
                    if payload and payload != "[DONE]":
                        payloads.append(payload)
            for payload in reversed(payloads):
                try:
                    parsed = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, dict):
                    return parsed
            raise SinaZyError(f"无法解析新浪智研 SSE 响应：{text[:500]}")
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise SinaZyError(f"无法解析新浪智研 JSON 响应：{text[:500]}") from exc
        if not isinstance(parsed, dict):
            raise SinaZyError(f"新浪智研响应不是 JSON object：{text[:500]}")
        return parsed

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = {
            "jsonrpc": "2.0",
            "id": self._next_id,
            "method": method,
            "params": params or {},
        }
        self._next_id += 1
        try:
            response = http_post(
                self.base_url,
                content=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers=self._headers(),
                timeout=self.timeout,
            )
            if not self.session_id:
                self.session_id = response.headers.get("Mcp-Session-Id", "") or response.headers.get(
                    "mcp-session-id", ""
                )
            parsed = self._parse_body(response.content, response.headers.get("Content-Type", ""))
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text[:500] if exc.response is not None else ""
            status_code = exc.response.status_code if exc.response is not None else "unknown"
            if status_code in {401, 403}:
                raise SinaZyError("新浪智研 API Key 未授权或已失效") from exc
            raise SinaZyError(f"新浪智研请求失败 HTTP {status_code}: {detail}") from exc
        except httpx.HTTPError as exc:
            raise SinaZyError(f"新浪智研网络请求失败：{exc}") from exc

        if parsed.get("error"):
            raise SinaZyError(f"新浪智研 JSON-RPC 错误：{parsed['error']}")
        return parsed

    def initialize(self) -> None:
        if self.initialized:
            return
        self.request(
            "initialize",
            {
                "protocolVersion": DEFAULT_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "surveil", "version": "0.1"},
            },
        )
        try:
            self.request("notifications/initialized", {})
        except SinaZyError:
            # Some servers do not require/accept initialized notifications over
            # simple HTTP. Tool calls still work after initialize.
            pass
        self.initialized = True

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        self.initialize()
        parsed = self.request("tools/call", {"name": name, "arguments": arguments or {}})
        result = parsed.get("result")
        if not isinstance(result, dict):
            return result
        if result.get("isError"):
            raise SinaZyError(f"新浪智研工具调用失败：{result}")
        content = result.get("content")
        if isinstance(content, list):
            for item in content:
                if not isinstance(item, dict):
                    continue
                text = item.get("text")
                if not isinstance(text, str) or not text.strip():
                    continue
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    return text
        if "structuredContent" in result:
            return result["structuredContent"]
        return result

    def news_flash_list(self, *, page: int = 1, num: int = 20, latest_id: str = "", older: bool = False) -> Any:
        args: dict[str, Any] = {"page": page, "num": min(max(1, num), 20)}
        if latest_id:
            args["id"] = str(latest_id)
            args["type"] = 1 if older else 0
        return self.call_tool("newsFlashList", args)

    def stock_news_search(self, *, market: str, symbol: str, page: int = 1, num: int = 20) -> Any:
        return self.call_tool(
            "stockNewsSearch",
            {"market": market, "symbol": symbol, "page": str(page), "num": str(min(max(1, num), 20))},
        )

    def news_article_detail(self, docid: str) -> Any:
        return self.call_tool("newsArticleDetail", {"docid": docid})


class SinaZyApiClient:
    @classmethod
    def from_env(cls) -> "SinaZyApiClient":
        api_key = (
            os.getenv("SINA_ZY_API_KEY", "")
            or os.getenv("SINA_API_KEY", "")
            or os.getenv("SINA_FINANCE_API_KEY", "")
        ).strip()
        base_url = os.getenv("SINA_ZY_API_BASE_URL", "").strip()
        if not api_key:
            raise SinaZyError("缺少 SINA_ZY_API_KEY")
        if not base_url:
            raise SinaZyError("缺少 SINA_ZY_API_BASE_URL：请从新浪智研登录后的 OpenAPI 接口详情页确认请求地址")
        raise SinaZyError(
            "SINA_NEWS_PROVIDER=zy_api 已启用，但 OpenAPI 实际调用路径尚未确认；"
            "请提供新浪智研接口详情页中的请求地址/示例 curl 后再启用。"
        )


def client_from_env(provider: str) -> Any:
    normalized = provider.strip().lower()
    if normalized in {"zy_api", "api", "openapi", "official_api"}:
        return SinaZyApiClient.from_env()
    if normalized in {"zy_mcp", "mcp"}:
        return SinaZyMcpClient.from_env()
    raise SinaZyError(f"不支持的新浪智研 provider：{provider}")


def result_data(payload: Any) -> Any:
    if isinstance(payload, dict) and isinstance(payload.get("result"), dict):
        status = payload["result"].get("status") if isinstance(payload["result"].get("status"), dict) else {}
        if str(status.get("code", "0")) not in {"0", ""}:
            raise SinaZyError(f"新浪智研返回异常：{status}")
        return payload["result"].get("data")
    return payload
