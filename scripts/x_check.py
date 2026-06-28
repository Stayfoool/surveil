#!/usr/bin/env python3
"""Smoke test for reading a user's recent posts via the X API."""

from __future__ import annotations

import json
import os
import base64
import textwrap
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / ".env"
API_BASE = "https://api.x.com/2"


def configured_x_username() -> str:
    username = os.getenv("X_USERNAME", "").strip().lstrip("@")
    if not username:
        raise SystemExit("缺少 X_USERNAME。请在 .env 中填写要监控的 X 账号，例如 X_USERNAME=example_user。")
    return username


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


class XApiError(Exception):
    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"HTTP {status}: {body}")
        self.status = status
        self.body = body


def x_get(path: str, params: dict[str, Any], token: str) -> dict[str, Any]:
    query = urllib.parse.urlencode(params, doseq=True)
    url = f"{API_BASE}{path}"
    if query:
        url = f"{url}?{query}"
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "User-Agent": "surveil-x-check/0.1",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read().decode("utf-8")
            return json.loads(body)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise XApiError(exc.code, body) from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"X API 网络请求失败：{exc}") from exc


def refresh_oauth2_token() -> str | None:
    refresh_token = os.getenv("X_REFRESH_TOKEN")
    client_id = os.getenv("X_CLIENT_ID")
    client_secret = os.getenv("X_CLIENT_SECRET")
    if not refresh_token or not client_id:
        return None

    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
    }
    encoded = urllib.parse.urlencode(data).encode("utf-8")
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": "surveil-x-check/0.1",
        "Accept": "application/json",
    }
    if client_secret:
        basic = base64.b64encode(f"{client_id}:{client_secret}".encode("utf-8")).decode("ascii")
        headers["Authorization"] = f"Basic {basic}"

    request = urllib.request.Request(
        f"{API_BASE}/oauth2/token",
        data=encoded,
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        print(f"刷新 OAuth2 token 失败：HTTP {exc.code}\n{body}")
        return None
    except urllib.error.URLError as exc:
        print(f"刷新 OAuth2 token 网络失败：{exc}")
        return None

    access_token = payload.get("access_token")
    new_refresh_token = payload.get("refresh_token")
    if access_token:
        os.environ["X_ACCESS_TOKEN"] = str(access_token)
    if new_refresh_token:
        os.environ["X_REFRESH_TOKEN"] = str(new_refresh_token)
    update_env_tokens(access_token, new_refresh_token)
    return str(access_token) if access_token else None


def update_env_tokens(access_token: Any, refresh_token: Any) -> None:
    if not ENV_PATH.exists() or not access_token:
        return
    replacements = {"X_ACCESS_TOKEN": str(access_token)}
    if refresh_token:
        replacements["X_REFRESH_TOKEN"] = str(refresh_token)

    lines = ENV_PATH.read_text(encoding="utf-8").splitlines()
    seen: set[str] = set()
    updated: list[str] = []
    for line in lines:
        stripped = line.strip()
        if "=" not in stripped or stripped.startswith("#"):
            updated.append(line)
            continue
        key, _ = stripped.split("=", 1)
        if key in replacements:
            updated.append(f"{key}={replacements[key]}")
            seen.add(key)
        else:
            updated.append(line)
    for key, value in replacements.items():
        if key not in seen:
            updated.append(f"{key}={value}")
    ENV_PATH.write_text("\n".join(updated) + "\n", encoding="utf-8")
    print("已刷新并更新 .env 里的 OAuth2 token。")


def request_with_available_tokens(path: str, params: dict[str, Any]) -> dict[str, Any]:
    attempts: list[tuple[str, str]] = []
    if os.getenv("X_ACCESS_TOKEN"):
        attempts.append(("X_ACCESS_TOKEN", os.environ["X_ACCESS_TOKEN"]))
    if os.getenv("X_BEARER_TOKEN"):
        attempts.append(("X_BEARER_TOKEN", os.environ["X_BEARER_TOKEN"]))
    if not attempts:
        raise SystemExit(
            "缺少 X_ACCESS_TOKEN 或 X_BEARER_TOKEN。请先复制 .env.example 为 .env 并填写 token。"
        )

    last_error: XApiError | None = None
    for label, token in attempts:
        print(f"使用 {label} 请求 X API...")
        try:
            return x_get(path, params, token)
        except XApiError as exc:
            last_error = exc
            print(f"{label} 请求失败：HTTP {exc.status}")
            if label == "X_ACCESS_TOKEN" and exc.status == 401:
                refreshed = refresh_oauth2_token()
                if refreshed:
                    print("使用刷新后的 X_ACCESS_TOKEN 重试...")
                    try:
                        return x_get(path, params, refreshed)
                    except XApiError as retry_exc:
                        last_error = retry_exc
                        print(f"刷新后仍失败：HTTP {retry_exc.status}")
            continue

    if last_error:
        raise SystemExit(f"X API 请求失败：HTTP {last_error.status}\n{last_error.body}")
    raise SystemExit("X API 请求失败。")


def post_text(post: dict[str, Any]) -> str:
    note = post.get("note_tweet")
    if isinstance(note, dict) and note.get("text"):
        return str(note["text"])
    article = post.get("article")
    if isinstance(article, dict):
        title = article.get("title")
        text = article.get("text") or article.get("preview_text")
        if title and text:
            return f"{title}\n{text}"
        if title:
            return str(title)
    return str(post.get("text", ""))


def main() -> int:
    load_env(ENV_PATH)
    username = configured_x_username()
    user = request_with_available_tokens(
        f"/users/by/username/{urllib.parse.quote(username)}",
        {
            "user.fields": "id,name,username,verified,public_metrics,description",
        },
    )
    user_data = user.get("data")
    if not user_data:
        print(json.dumps(user, ensure_ascii=False, indent=2))
        raise SystemExit("没有拿到用户数据，请检查 token 权限或 username。")

    user_id = user_data["id"]
    print(f"用户：{user_data.get('name')} (@{user_data.get('username')})")
    print(f"user id：{user_id}")
    print()

    tweets = request_with_available_tokens(
        f"/users/{user_id}/tweets",
        {
            "max_results": 5,
            "exclude": "retweets,replies",
            "tweet.fields": "id,text,created_at,author_id,public_metrics,referenced_tweets,entities,note_tweet,article,lang",
            "expansions": "author_id",
        },
    )

    posts = tweets.get("data", [])
    if not posts:
        print(json.dumps(tweets, ensure_ascii=False, indent=2))
        raise SystemExit("API 返回成功，但没有帖子数据。")

    print("最近帖子：")
    for post in posts:
        text = post_text(post).replace("\r", " ").strip()
        preview = textwrap.shorten(" ".join(text.split()), width=260, placeholder="...")
        url = f"https://x.com/{username}/status/{post['id']}"
        metrics = post.get("public_metrics", {})
        print(f"- {post.get('created_at', 'unknown time')} {url}")
        if metrics:
            print(
                "  metrics: "
                f"replies={metrics.get('reply_count')} "
                f"reposts={metrics.get('retweet_count')} "
                f"likes={metrics.get('like_count')} "
                f"quotes={metrics.get('quote_count')}"
            )
        print(f"  {preview}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
