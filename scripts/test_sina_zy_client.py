#!/usr/bin/env python3
"""Regression checks for Sina ZhiYan client plumbing."""

from __future__ import annotations

from dataclasses import dataclass

import httpx

import sina_zy_client
from sina_zy_client import SinaZyError, SinaZyMcpClient


@dataclass
class FakeResponse:
    status_code: int
    url: str
    headers: dict[str, str]
    content: bytes


def test_mcp_client_uses_shared_http_post() -> None:
    calls: list[dict] = []
    original_http_post = sina_zy_client.http_post
    try:
        def fake_http_post(url: str, *, headers=None, content=None, json_data=None, timeout=None, retries=None):
            calls.append(
                {
                    "url": url,
                    "headers": headers or {},
                    "content": content,
                    "json_data": json_data,
                    "timeout": timeout,
                    "retries": retries,
                }
            )
            return FakeResponse(
                200,
                url,
                {"Content-Type": "application/json", "Mcp-Session-Id": "session-1"},
                b'{"jsonrpc":"2.0","id":1,"result":{"ok":true}}',
            )

        sina_zy_client.http_post = fake_http_post
        client = SinaZyMcpClient(api_key="test-key", base_url="https://example.com/mcp", timeout=7)
        result = client.request("tools/list", {})
        assert result["result"]["ok"] is True
        assert client.session_id == "session-1"
        assert calls[0]["url"] == "https://example.com/mcp"
        assert calls[0]["headers"]["X-Auth-Token"] == "test-key"
        assert calls[0]["timeout"] == 7
        assert b'"method": "tools/list"' in calls[0]["content"]
    finally:
        sina_zy_client.http_post = original_http_post


def test_mcp_client_maps_auth_error() -> None:
    original_http_post = sina_zy_client.http_post
    try:
        def fake_http_post(*args, **kwargs):  # noqa: ANN002, ANN003
            request = httpx.Request("POST", "https://example.com/mcp")
            response = httpx.Response(401, request=request, text="unauthorized")
            raise httpx.HTTPStatusError("unauthorized", request=request, response=response)

        sina_zy_client.http_post = fake_http_post
        client = SinaZyMcpClient(api_key="bad-key", base_url="https://example.com/mcp")
        try:
            client.request("tools/list", {})
        except SinaZyError as exc:
            assert "API Key" in str(exc)
        else:
            raise AssertionError("401 should map to SinaZyError")
    finally:
        sina_zy_client.http_post = original_http_post


def main() -> int:
    test_mcp_client_uses_shared_http_post()
    test_mcp_client_maps_auth_error()
    print("sina zy client checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
