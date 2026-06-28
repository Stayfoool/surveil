#!/usr/bin/env python3
"""Listen to X Filtered Stream for posts from one account."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cards import build_serenity_card
from db_utils import connect_sqlite, retry_on_locked
from feishu import send_card
from link_enrichment import enrich_post_links
from llm_analysis import llm_config
from x_check import configured_x_username, load_env, post_text, refresh_oauth2_token, request_with_available_tokens


ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / ".env"
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "surveil.sqlite3"
API_BASE = "https://api.x.com/2"
REST_BACKFILL_INTERVAL_SECONDS = int(os.getenv("X_REST_BACKFILL_INTERVAL_SECONDS", "60"))
MEDIA_FIELDS = "media_key,type,url,preview_image_url,width,height,variants"


def bearer_token() -> str:
    token = os.getenv("X_BEARER_TOKEN") or os.getenv("X_ACCESS_TOKEN")
    if not token:
        raise SystemExit("缺少 X_BEARER_TOKEN 或 X_ACCESS_TOKEN。")
    return token


def connect_db() -> sqlite3.Connection:
    conn = connect_sqlite(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS seen_posts (
            source TEXT NOT NULL,
            post_id TEXT NOT NULL,
            url TEXT NOT NULL,
            text TEXT NOT NULL,
            published_at TEXT,
            first_seen_at TEXT NOT NULL,
            PRIMARY KEY (source, post_id)
        )
        """
    )
    conn.commit()
    return conn


def x_request(method: str, path: str, token: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    data = None
    headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent": "surveil-x-stream/0.1",
        "Accept": "application/json",
    }
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        f"{API_BASE}{path}",
        data=data,
        headers=headers,
        method=method,
    )
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = response.read().decode("utf-8")
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"X API {method} {path} 失败：HTTP {exc.code}\n{body}") from exc
        except urllib.error.URLError as exc:
            last_error = exc
            if attempt == 2:
                break
            time.sleep(2 + attempt * 3)
    raise RuntimeError(f"X API {method} {path} 网络失败：{last_error}") from last_error


def ensure_rule(username: str, token: str) -> None:
    rule_value = f"from:{username} -is:retweet"
    rules = x_request("GET", "/tweets/search/stream/rules", token)
    existing = rules.get("data", []) or []
    delete_ids = [
        rule["id"]
        for rule in existing
        if rule.get("tag") == "surveil-serenity" and rule.get("value") != rule_value
    ]
    if delete_ids:
        x_request("POST", "/tweets/search/stream/rules", token, {"delete": {"ids": delete_ids}})

    rules = x_request("GET", "/tweets/search/stream/rules", token)
    existing = rules.get("data", []) or []
    if any(rule.get("value") == rule_value for rule in existing):
        print(f"Filtered Stream rule 已存在：{rule_value}", flush=True)
        return
    result = x_request(
        "POST",
        "/tweets/search/stream/rules",
        token,
        {"add": [{"value": rule_value, "tag": "surveil-serenity"}]},
    )
    print(f"已添加 Filtered Stream rule：{json.dumps(result, ensure_ascii=False)}", flush=True)


def save_post(conn: sqlite3.Connection, username: str, post: dict[str, Any]) -> bool:
    post_id = str(post["id"])
    text = post_text(post).strip()
    url = f"https://x.com/{username}/status/{post_id}"
    now = datetime.now(timezone.utc).isoformat()
    try:
        conn.execute(
            """
            INSERT INTO seen_posts (
                source, post_id, url, text, published_at, first_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                f"x:{username}",
                post_id,
                url,
                text,
                post.get("created_at"),
                now,
            ),
        )
    except sqlite3.IntegrityError:
        return False
    conn.commit()
    post["url"] = url
    post["full_text"] = text
    return True


def save_post_with_retry(username: str, post: dict[str, Any]) -> bool:
    def operation() -> bool:
        with connect_db() as conn:
            return save_post(conn, username, post)

    return retry_on_locked(operation)


def attach_media(post: dict[str, Any], payload: dict[str, Any]) -> None:
    media_keys = post.get("attachments", {}).get("media_keys", [])
    media_items = payload.get("includes", {}).get("media", [])
    by_key = {item.get("media_key"): item for item in media_items}
    attached = []
    for key in media_keys:
        item = by_key.get(key)
        if not item:
            continue
        media_url = media_url_from_item(item)
        if not media_url:
            continue
        attached.append(
            {
                "type": item.get("type", "media"),
                "url": media_url,
                "width": item.get("width"),
                "height": item.get("height"),
            }
        )
    post["_media"] = attached


def media_url_from_item(item: dict[str, Any]) -> str:
    media_url = item.get("url") or item.get("preview_image_url")
    if media_url:
        return str(media_url)
    variants = item.get("variants") or []
    if isinstance(variants, list):
        candidates = [variant for variant in variants if isinstance(variant, dict) and variant.get("url")]
        candidates.sort(key=lambda variant: int(variant.get("bit_rate") or 0), reverse=True)
        if candidates:
            return str(candidates[0].get("url") or "")
    return ""


def x_status_refs_from_links(post: dict[str, Any]) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    entities = post.get("entities") if isinstance(post.get("entities"), dict) else {}
    raw_urls = entities.get("urls") if isinstance(entities, dict) else []
    text_urls = []
    if isinstance(raw_urls, list):
        for item in raw_urls:
            if not isinstance(item, dict):
                continue
            text_urls.append(str(item.get("expanded_url") or item.get("unwound_url") or item.get("url") or ""))
    text_urls.append(str(post.get("full_text") or post.get("text") or ""))
    for value in text_urls:
        for match in re.finditer(r"((?:https?://)?(?:x|twitter)\.com/[^/\s]+/status/(\d+)(?:/photo/\d+)?)", value):
            url = match.group(1)
            if not url.startswith("http"):
                url = f"https://{url}"
            status_id = match.group(2)
            is_photo = bool(re.search(r"/photo/\d+$", url))
            if status_id == str(post.get("id")) and not is_photo:
                continue
            if not any(ref["status_id"] == status_id for ref in refs):
                refs.append({"status_id": status_id, "url": url, "is_photo": is_photo})
    return refs


def x_status_ids_from_links(post: dict[str, Any]) -> list[str]:
    return [ref["status_id"] for ref in x_status_refs_from_links(post)]


def attach_linked_status_context(post: dict[str, Any]) -> None:
    refs = x_status_refs_from_links(post)
    if not refs:
        return
    existing = {item.get("url") for item in post.get("_media", []) if item.get("url")}
    attached = list(post.get("_media", []))
    linked_statuses = list(post.get("_linked_statuses", []))
    for ref in refs[:3]:
        status_id = ref["status_id"]
        try:
            payload = request_with_available_tokens(
                f"/tweets/{status_id}",
                {
                    "tweet.fields": "id,text,created_at,attachments,entities,note_tweet,article,author_id",
                    "expansions": "attachments.media_keys,author_id",
                    "media.fields": MEDIA_FIELDS,
                    "user.fields": "id,name,username",
                },
            )
        except BaseException as exc:  # noqa: BLE001 - request helper may raise SystemExit on transient failures
            if isinstance(exc, KeyboardInterrupt):
                raise
            print(f"X 链接帖媒体查询失败 {status_id}: {exc}", flush=True)
            continue
        linked_post = payload.get("data") or {}
        media_count = 0
        for item in payload.get("includes", {}).get("media", []) or []:
            media_url = media_url_from_item(item)
            if not media_url or media_url in existing:
                continue
            existing.add(media_url)
            media_count += 1
            attached.append(
                {
                    "type": item.get("type", "media"),
                    "url": media_url,
                    "width": item.get("width"),
                    "height": item.get("height"),
                    "source_status_id": status_id,
                }
            )
        users = payload.get("includes", {}).get("users", []) or []
        user_by_id = {str(user.get("id")): user for user in users if isinstance(user, dict)}
        author = user_by_id.get(str(linked_post.get("author_id")), {})
        linked_statuses.append(
            {
                "status_id": status_id,
                "url": ref.get("url") or f"https://x.com/i/status/{status_id}",
                "text": post_text(linked_post).strip(),
                "created_at": linked_post.get("created_at") or "",
                "author_name": author.get("name") or "",
                "author_username": author.get("username") or "",
                "media_count": media_count,
                "is_photo": bool(ref.get("is_photo")),
            }
        )
    post["_media"] = attached
    post["_linked_statuses"] = linked_statuses


def merge_linked_status_links(post: dict[str, Any]) -> None:
    links = list(post.get("_links") or [])
    for linked in post.get("_linked_statuses") or []:
        status_id = str(linked.get("status_id") or "")
        if not status_id:
            continue
        title_author = ""
        if linked.get("author_username"):
            title_author = f" @{linked['author_username']}"
        title = f"X 链接帖{title_author} / {status_id}"
        media_note = ""
        if linked.get("media_count"):
            media_note = f"链接帖包含 {linked['media_count']} 个媒体附件；图片会尝试内嵌到飞书卡片。"
        text = str(linked.get("text") or "").strip()
        if media_note:
            text = f"{text}\n\n{media_note}".strip()
        replacement = {
            "url": linked.get("url") or f"https://x.com/i/status/{status_id}",
            "effective_url": linked.get("url") or f"https://x.com/i/status/{status_id}",
            "title": title,
            "description": media_note,
            "text": text,
            "content_type": "x-status-api",
            "status": "ok",
            "error": "",
        }
        for index, link in enumerate(links):
            link_url = str(link.get("effective_url") or link.get("url") or "")
            if f"/status/{status_id}" in link_url:
                links[index] = {**link, **replacement}
                break
        else:
            links.append(replacement)
    post["_links"] = links


def attach_linked_status_media(post: dict[str, Any]) -> None:
    attach_linked_status_context(post)


def notify_post(post: dict[str, Any]) -> None:
    attach_linked_status_context(post)
    try:
        links = enrich_post_links(post)
        if links:
            print(f"已抓取 X 外链 {len(links)} 条。", flush=True)
    except Exception as exc:
        print(f"X 外链抓取失败：{exc}", flush=True)
        post["_links"] = []
    merge_linked_status_links(post)
    send_card(build_serenity_card(post))


def stream_url() -> str:
    params = urllib.parse.urlencode(
        {
            "tweet.fields": "id,text,created_at,author_id,public_metrics,referenced_tweets,entities,note_tweet,article,lang,conversation_id,in_reply_to_user_id",
            "expansions": "author_id,attachments.media_keys",
            "media.fields": MEDIA_FIELDS,
        }
    )
    return f"{API_BASE}/tweets/search/stream?{params}"


def fetch_recent_posts(username: str, max_results: int = 10) -> list[dict[str, Any]]:
    user = request_with_available_tokens(
        f"/users/by/username/{username}",
        {"user.fields": "id,name,username,verified"},
    )
    user_id = user["data"]["id"]
    tweets = request_with_available_tokens(
        f"/users/{user_id}/tweets",
        {
            "max_results": max_results,
            "exclude": "retweets",
            "tweet.fields": "id,text,created_at,author_id,public_metrics,referenced_tweets,entities,note_tweet,article,lang,conversation_id,in_reply_to_user_id",
            "expansions": "attachments.media_keys",
            "media.fields": MEDIA_FIELDS,
        },
    )
    media_items = tweets.get("includes", {}).get("media", [])
    media_by_key = {item.get("media_key"): item for item in media_items}
    posts = tweets.get("data", []) or []
    for post in posts:
        attached = []
        for key in post.get("attachments", {}).get("media_keys", []):
            item = media_by_key.get(key)
            if not item:
                continue
            media_url = media_url_from_item(item)
            if media_url:
                attached.append(
                    {
                        "type": item.get("type", "media"),
                        "url": media_url,
                        "width": item.get("width"),
                        "height": item.get("height"),
                    }
                )
        post["_media"] = attached
    return posts


def backfill_recent_posts(username: str) -> int:
    try:
        posts = fetch_recent_posts(username, max_results=10)
    except Exception as exc:
        print(f"X REST 补漏失败：{exc}", flush=True)
        return 0
    count = 0
    for post in sorted(posts, key=lambda item: item.get("created_at", "")):
        if save_post_with_retry(username, post):
            count += 1
            print(f"REST 补漏发现 X 新帖：{post['url']}", flush=True)
            notify_post(post)
    if count == 0:
        print("X REST 补漏：没有发现新帖。", flush=True)
    return count


def maybe_refresh_stream_token(error_text: str) -> bool:
    if "HTTP 401" not in error_text:
        return False
    refreshed = refresh_oauth2_token()
    if refreshed:
        print("X stream token 已刷新，下次重连使用新 X_ACCESS_TOKEN。", flush=True)
        return True
    return False


def stream_forever(username: str) -> None:
    url = stream_url()
    backoff = 5
    last_backfill_at = 0.0
    while True:
        token = bearer_token()
        try:
            ensure_rule(username, token)
        except Exception as exc:
            error_text = str(exc)
            print(f"X stream rule 检查失败：{error_text}", flush=True)
            if maybe_refresh_stream_token(error_text):
                backoff = 5
            print(f"{backoff} 秒后重试 X stream rule。", flush=True)
            time.sleep(backoff)
            backoff = min(backoff * 2, 300)
            continue

        request = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "User-Agent": "surveil-x-stream/0.1",
                "Accept": "application/json",
            },
        )
        print("连接 X Filtered Stream...", flush=True)
        try:
            now = time.monotonic()
            if now - last_backfill_at >= REST_BACKFILL_INTERVAL_SECONDS:
                backfill_recent_posts(username)
                last_backfill_at = now
            with urllib.request.urlopen(request, timeout=None) as response:
                print("X Filtered Stream 已连接。", flush=True)
                backoff = 5
                for raw_line in response:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    payload = json.loads(line)
                    post = payload.get("data")
                    if not post:
                        print(f"stream 非帖子消息：{line}", flush=True)
                        continue
                    attach_media(post, payload)
                    if save_post_with_retry(username, post):
                        print(f"发现 X 新帖：{post['url']}", flush=True)
                        notify_post(post)
                    else:
                        print(f"忽略已见过的帖子：{post.get('id')}", flush=True)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            print(f"X stream HTTP {exc.code}: {body}", flush=True)
            if exc.code == 429:
                backoff = max(backoff, 300)
            elif exc.code == 401 and refresh_oauth2_token():
                print("X stream token 已刷新，下次重连使用新 X_ACCESS_TOKEN。", flush=True)
                backoff = 5
        except Exception as exc:
            print(f"X stream 连接失败：{exc}", flush=True)

        print(f"{backoff} 秒后重连 X stream。", flush=True)
        time.sleep(backoff)
        backoff = min(backoff * 2, 300)


def main() -> int:
    load_env(ENV_PATH)
    config = llm_config()
    if config:
        _, base_url, model = config
        print(f"X stream LLM config: {base_url} / {model}", flush=True)
    else:
        print("X stream LLM config: 未配置", flush=True)
    username = configured_x_username()
    # Resolve the user once with the existing helper; this also proves credentials before opening stream.
    startup_backoff = 5
    while True:
        try:
            request_with_available_tokens(f"/users/by/username/{username}", {"user.fields": "id,name,username"})
            break
        except SystemExit as exc:
            print(f"X API 启动校验失败：{exc}", flush=True)
        except Exception as exc:
            print(f"X API 启动校验失败：{exc}", flush=True)
        print(f"{startup_backoff} 秒后重试 X API 启动校验。", flush=True)
        time.sleep(startup_backoff)
        startup_backoff = min(startup_backoff * 2, 300)
    stream_forever(username)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
