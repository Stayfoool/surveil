"""Feishu custom bot sender."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.request
from typing import Any


def feishu_retry_count() -> int:
    raw = os.getenv("FEISHU_RETRY_COUNT", "").strip()
    if not raw:
        return 2
    try:
        return max(0, min(5, int(raw)))
    except ValueError:
        return 2


def feishu_retry_sleep_seconds(attempt: int) -> float:
    raw = os.getenv("FEISHU_RETRY_SLEEP_SECONDS", "").strip()
    try:
        base = float(raw) if raw else 3.0
    except ValueError:
        base = 3.0
    return min(30.0, max(0.0, base) * (attempt + 1))


def should_retry_result(result: dict[str, Any]) -> bool:
    code = result.get("code")
    message = str(result.get("msg") or result.get("message") or "").lower()
    return code in {11232, 9499} or "frequency limited" in message or "rate limit" in message


def post_payload(payload: dict[str, Any], *, error_prefix: str) -> bool:
    webhook = os.getenv("FEISHU_WEBHOOK", "").strip()
    if not webhook:
        return False
    attempts = feishu_retry_count() + 1
    last_body = ""
    for attempt in range(attempts):
        request = urllib.request.Request(
            webhook,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "User-Agent": "surveil-feishu/0.1",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                body = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            last_body = f"HTTP {exc.code}\n{body}"
            if attempt < attempts - 1 and exc.code in {429, 500, 502, 503, 504}:
                time.sleep(feishu_retry_sleep_seconds(attempt))
                continue
            raise RuntimeError(f"{error_prefix}：HTTP {exc.code}\n{body}") from exc
        except urllib.error.URLError as exc:
            if attempt < attempts - 1:
                time.sleep(feishu_retry_sleep_seconds(attempt))
                continue
            raise RuntimeError(f"{error_prefix}网络请求失败：{exc}") from exc

        last_body = body
        result = json.loads(body)
        if result.get("code") in (0, None):
            return True
        if attempt < attempts - 1 and should_retry_result(result):
            time.sleep(feishu_retry_sleep_seconds(attempt))
            continue
        raise RuntimeError(f"{error_prefix}：{body}")
    raise RuntimeError(f"{error_prefix}：{last_body}")


def sign(secret: str, timestamp: str) -> str:
    string_to_sign = f"{timestamp}\n{secret}".encode("utf-8")
    digest = hmac.new(string_to_sign, b"", hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def send_text(title: str, lines: list[str]) -> bool:
    webhook = os.getenv("FEISHU_WEBHOOK", "").strip()
    if not webhook:
        return False

    content = "\n".join([title, "", *lines]).strip()
    payload: dict[str, Any] = {
        "msg_type": "text",
        "content": {"text": content},
    }
    secret = os.getenv("FEISHU_SECRET", "").strip()
    if secret:
        timestamp = str(int(time.time()))
        payload["timestamp"] = timestamp
        payload["sign"] = sign(secret, timestamp)
    return post_payload(payload, error_prefix="飞书发送失败")


def send_post(title: str, lines: list[str]) -> bool:
    webhook = os.getenv("FEISHU_WEBHOOK", "").strip()
    if not webhook:
        return False

    content_items = []
    for line in [title, "", *lines]:
        content_items.append([{"tag": "text", "text": line}])
    payload: dict[str, Any] = {
        "msg_type": "post",
        "content": {
            "post": {
                "zh_cn": {
                    "title": title,
                    "content": content_items,
                }
            }
        },
    }
    secret = os.getenv("FEISHU_SECRET", "").strip()
    if secret:
        timestamp = str(int(time.time()))
        payload["timestamp"] = timestamp
        payload["sign"] = sign(secret, timestamp)
    return post_payload(payload, error_prefix="飞书发送失败")


def send_card(card: dict[str, Any]) -> bool:
    webhook = os.getenv("FEISHU_WEBHOOK", "").strip()
    if not webhook:
        return False

    payload: dict[str, Any] = {
        "msg_type": "interactive",
        "card": card,
    }
    secret = os.getenv("FEISHU_SECRET", "").strip()
    if secret:
        timestamp = str(int(time.time()))
        payload["timestamp"] = timestamp
        payload["sign"] = sign(secret, timestamp)
    return post_payload(payload, error_prefix="飞书卡片发送失败")
