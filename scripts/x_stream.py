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
from feishu import send_card, send_text
from link_enrichment import enrich_post_links
from llm_analysis import llm_config
from source_health import record_source_failure, record_source_success
from x_check import configured_x_username, load_env, post_text, refresh_oauth2_token, request_with_available_tokens


ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / ".env"
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "surveil.sqlite3"
API_BASE = "https://api.x.com/2"
MEDIA_FIELDS = "media_key,type,url,preview_image_url,width,height,variants"
MAX_DELIVERY_ATTEMPTS = 5
DEFAULT_ALERT_THRESHOLD = 3
DEFAULT_ALERT_COOLDOWN_SECONDS = 1800


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
            delivery_status TEXT NOT NULL DEFAULT 'pending',
            delivered_at TEXT,
            delivery_error TEXT,
            delivery_attempts INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (source, post_id)
        )
        """
    )
    ensure_seen_posts_delivery_columns(conn)
    ensure_x_stream_health_table(conn)
    conn.commit()
    return conn


def ensure_seen_posts_delivery_columns(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(seen_posts)")}
    if "delivery_status" not in columns:
        # Existing rows were produced before delivery tracking existed; avoid replaying old posts.
        conn.execute("ALTER TABLE seen_posts ADD COLUMN delivery_status TEXT NOT NULL DEFAULT 'sent'")
    if "delivered_at" not in columns:
        conn.execute("ALTER TABLE seen_posts ADD COLUMN delivered_at TEXT")
    if "delivery_error" not in columns:
        conn.execute("ALTER TABLE seen_posts ADD COLUMN delivery_error TEXT")
    if "delivery_attempts" not in columns:
        conn.execute("ALTER TABLE seen_posts ADD COLUMN delivery_attempts INTEGER NOT NULL DEFAULT 0")


def ensure_x_stream_health_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS x_stream_health (
            issue_key TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            failure_count INTEGER NOT NULL DEFAULT 0,
            first_failed_at TEXT,
            last_failed_at TEXT,
            last_error TEXT,
            last_alerted_at TEXT,
            last_recovered_at TEXT
        )
        """
    )


def rest_backfill_interval_seconds() -> int:
    raw = os.getenv("X_REST_BACKFILL_INTERVAL_SECONDS", "60").strip()
    try:
        return max(10, int(raw))
    except ValueError:
        print(f"X_REST_BACKFILL_INTERVAL_SECONDS 无效：{raw!r}，使用 60 秒。", flush=True)
        return 60


def alert_threshold() -> int:
    raw = os.getenv("X_STREAM_ALERT_THRESHOLD", "").strip()
    try:
        return max(1, int(raw)) if raw else DEFAULT_ALERT_THRESHOLD
    except ValueError:
        return DEFAULT_ALERT_THRESHOLD


def alert_cooldown_seconds() -> int:
    raw = os.getenv("X_STREAM_ALERT_COOLDOWN_SECONDS", "").strip()
    try:
        return max(60, int(raw)) if raw else DEFAULT_ALERT_COOLDOWN_SECONDS
    except ValueError:
        return DEFAULT_ALERT_COOLDOWN_SECONDS


def alerts_enabled() -> bool:
    return os.getenv("X_STREAM_ALERT_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def classify_stream_error(error_text: str, *, status_code: int | None = None, phase: str = "stream") -> tuple[str, str, bool]:
    text = error_text.lower()
    if status_code == 429 or "toomanyconnections" in text or "maximum allowed connection limit" in text:
        return "too_many_connections", "X API stream 连接数超限", True
    if status_code in {401, 403} or "http 401" in text or "http 403" in text:
        return "auth", "X API 鉴权异常", True
    if "connection refused" in text or "errno 61" in text or "errno 111" in text:
        return "proxy_refused", "X 代理连接被拒绝", False
    if "network is unreachable" in text or "errno 101" in text:
        return "network_unreachable", "X 网络不可达", False
    if "timed out" in text or "timeout" in text:
        return "timeout", "X API 请求超时", False
    if status_code in {500, 502, 503, 504} or "http 503" in text or "service unavailable" in text:
        return "x_api_unavailable", "X API 服务异常", False
    if "connection reset" in text:
        return "connection_reset", "X stream 连接被重置", False
    return f"{phase}_error", "X stream 异常", False


def notify_health_alert(issue_title: str, lines: list[str]) -> None:
    if not alerts_enabled():
        return
    try:
        sent = send_text(issue_title, lines)
        if not sent:
            print(f"X stream 告警未发送：FEISHU_WEBHOOK 未配置。{issue_title}", flush=True)
    except Exception as exc:
        print(f"X stream 告警发送失败：{exc}", flush=True)


def record_stream_failure(error_text: str, *, status_code: int | None = None, phase: str = "stream") -> None:
    issue_key, issue_name, immediate = classify_stream_error(error_text, status_code=status_code, phase=phase)
    now = utc_now_iso()
    with connect_db() as conn:
        record_source_failure(conn, "x_stream", issue_key, error_text)

    def operation() -> tuple[int, str, str]:
        with connect_db() as conn:
            row = conn.execute(
                """
                SELECT status, failure_count, first_failed_at, last_alerted_at
                FROM x_stream_health
                WHERE issue_key = ?
                """,
                (issue_key,),
            ).fetchone()
            if row:
                status, previous_count, first_failed_at, last_alerted_at = row
                failure_count = int(previous_count or 0) + 1
                first_failed_at = first_failed_at or now
                conn.execute(
                    """
                    UPDATE x_stream_health
                    SET status = 'failing',
                        failure_count = ?,
                        first_failed_at = ?,
                        last_failed_at = ?,
                        last_error = ?
                    WHERE issue_key = ?
                    """,
                    (failure_count, first_failed_at, now, error_text[:1000], issue_key),
                )
            else:
                failure_count = 1
                first_failed_at = now
                last_alerted_at = None
                conn.execute(
                    """
                    INSERT INTO x_stream_health (
                        issue_key, status, failure_count, first_failed_at, last_failed_at, last_error
                    ) VALUES (?, 'failing', ?, ?, ?, ?)
                    """,
                    (issue_key, failure_count, now, now, error_text[:1000]),
                )
            conn.commit()
            return failure_count, first_failed_at, str(last_alerted_at or "")

    failure_count, first_failed_at, last_alerted_at = retry_on_locked(operation)
    threshold_reached = failure_count >= alert_threshold()
    cooldown_ok = True
    if last_alerted_at:
        try:
            elapsed = datetime.fromisoformat(now).timestamp() - datetime.fromisoformat(last_alerted_at).timestamp()
            cooldown_ok = elapsed >= alert_cooldown_seconds()
        except ValueError:
            cooldown_ok = True
    if not (immediate or threshold_reached) or not cooldown_ok:
        return

    notify_health_alert(
        f"Surveil 告警：{issue_name}",
        [
            f"模块：X/Serenity stream",
            f"阶段：{phase}",
            f"连续次数：{failure_count}",
            f"首次失败：{first_failed_at}",
            f"最近失败：{now}",
            f"错误摘要：{error_text[:500]}",
            "系统会继续自动重试；恢复后会发送恢复通知。",
        ],
    )

    def mark_alerted() -> None:
        with connect_db() as conn:
            conn.execute(
                "UPDATE x_stream_health SET last_alerted_at = ? WHERE issue_key = ?",
                (now, issue_key),
            )
            conn.commit()

    retry_on_locked(mark_alerted)


def record_stream_recovery(phase: str = "stream") -> None:
    now = utc_now_iso()

    def operation() -> list[tuple[str, int, str, str, bool]]:
        with connect_db() as conn:
            rows = conn.execute(
                """
                SELECT issue_key, failure_count, first_failed_at, last_error, last_alerted_at
                FROM x_stream_health
                WHERE status = 'failing'
                """
            ).fetchall()
            conn.execute(
                """
                UPDATE x_stream_health
                SET status = 'ok',
                    failure_count = 0,
                    last_recovered_at = ?
                WHERE status = 'failing'
                """,
                (now,),
            )
            conn.commit()
        return [
            (str(key), int(count or 0), str(first or ""), str(error or ""), bool(alerted))
            for key, count, first, error, alerted in rows
        ]

    recovered = retry_on_locked(operation)
    with connect_db() as conn:
        record_source_success(conn, "x_stream", phase)
        for key, *_ in recovered:
            record_source_success(conn, "x_stream", key)
    alerted_recovered = [row for row in recovered if row[4]]
    if not alerted_recovered:
        return
    names = {
        "too_many_connections": "X API stream 连接数超限",
        "auth": "X API 鉴权异常",
        "proxy_refused": "X 代理连接被拒绝",
        "network_unreachable": "X 网络不可达",
        "timeout": "X API 请求超时",
        "x_api_unavailable": "X API 服务异常",
        "connection_reset": "X stream 连接被重置",
        "rule_error": "X stream rule 异常",
        "stream_error": "X stream 异常",
        "startup_error": "X API 启动校验异常",
        "rest_backfill_error": "X REST 补漏异常",
    }
    issue_lines = [
        f"- {names.get(key, key)}：失败 {count} 次，首次 {first or 'unknown'}"
        for key, count, first, _, _alerted in alerted_recovered
    ]
    notify_health_alert(
        "Surveil 恢复：X/Serenity stream 已恢复",
        [
            f"模块：X/Serenity stream",
            f"恢复阶段：{phase}",
            f"恢复时间：{now}",
            *issue_lines,
        ],
    )


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
                source, post_id, url, text, published_at, first_seen_at, delivery_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"x:{username}",
                post_id,
                url,
                text,
                post.get("created_at"),
                now,
                "pending",
            ),
        )
    except sqlite3.IntegrityError:
        return False
    conn.commit()
    post["url"] = url
    post["full_text"] = text
    return True


def mark_post_delivery(username: str, post_id: str, status: str, error: str = "") -> None:
    source = f"x:{username}"
    delivered_at = datetime.now(timezone.utc).isoformat() if status == "sent" else None

    def operation() -> None:
        with connect_db() as conn:
            conn.execute(
                """
                UPDATE seen_posts
                SET delivery_status = ?,
                    delivered_at = COALESCE(?, delivered_at),
                    delivery_error = ?,
                    delivery_attempts = delivery_attempts + 1
                WHERE source = ? AND post_id = ?
                """,
                (status, delivered_at, error[:1000], source, str(post_id)),
            )
            conn.commit()

    retry_on_locked(operation)


def load_pending_deliveries(username: str, limit: int = 10) -> list[dict[str, Any]]:
    source = f"x:{username}"

    def operation() -> list[dict[str, Any]]:
        with connect_db() as conn:
            rows = conn.execute(
                """
                SELECT post_id, url, text, published_at
                FROM seen_posts
                WHERE source = ?
                  AND delivery_status IN ('pending', 'failed')
                  AND delivery_attempts < ?
                ORDER BY first_seen_at ASC
                LIMIT ?
                """,
                (source, MAX_DELIVERY_ATTEMPTS, limit),
            ).fetchall()
        posts = []
        for post_id, url, text, published_at in rows:
            posts.append(
                {
                    "id": str(post_id),
                    "url": url,
                    "text": text,
                    "full_text": text,
                    "created_at": published_at,
                    "_media": [],
                    "_links": [],
                }
            )
        return posts

    return retry_on_locked(operation)


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


def notify_post(post: dict[str, Any]) -> bool:
    attach_linked_status_context(post)
    try:
        links = enrich_post_links(post)
        if links:
            print(f"已抓取 X 外链 {len(links)} 条。", flush=True)
    except Exception as exc:
        print(f"X 外链抓取失败：{exc}", flush=True)
        with connect_db() as conn:
            record_source_failure(conn, "x_stream", "link_enrichment", exc)
        post["_links"] = []
    else:
        with connect_db() as conn:
            record_source_success(conn, "x_stream", "link_enrichment")
    merge_linked_status_links(post)
    return send_card(build_serenity_card(post))


def deliver_post(username: str, post: dict[str, Any]) -> bool:
    post_id = str(post["id"])
    try:
        sent = notify_post(post)
    except Exception as exc:
        error = str(exc)
        print(f"X 新帖飞书发送失败：{post.get('url') or post_id} {error}", flush=True)
        mark_post_delivery(username, post_id, "failed", error)
        return False
    if sent:
        mark_post_delivery(username, post_id, "sent")
        return True
    print(f"X 新帖飞书发送跳过：FEISHU_WEBHOOK 未配置 {post.get('url') or post_id}", flush=True)
    mark_post_delivery(username, post_id, "skipped", "FEISHU_WEBHOOK 未配置")
    return False


def retry_pending_deliveries(username: str) -> int:
    posts = load_pending_deliveries(username)
    if not posts:
        return 0
    sent_count = 0
    for post in posts:
        print(f"重试 X 新帖飞书发送：{post.get('url')}", flush=True)
        if deliver_post(username, post):
            sent_count += 1
    return sent_count


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
        error_text = str(exc)
        print(f"X REST 补漏失败：{error_text}", flush=True)
        record_stream_failure(error_text, phase="rest_backfill")
        return 0
    count = 0
    for post in sorted(posts, key=lambda item: item.get("created_at", "")):
        if save_post_with_retry(username, post):
            count += 1
            print(f"REST 补漏发现 X 新帖：{post['url']}", flush=True)
            deliver_post(username, post)
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
    backfill_interval = rest_backfill_interval_seconds()
    while True:
        token = bearer_token()
        try:
            ensure_rule(username, token)
        except Exception as exc:
            error_text = str(exc)
            print(f"X stream rule 检查失败：{error_text}", flush=True)
            record_stream_failure(error_text, phase="rule")
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
            if now - last_backfill_at >= backfill_interval:
                retry_pending_deliveries(username)
                backfill_recent_posts(username)
                last_backfill_at = now
            with urllib.request.urlopen(request, timeout=None) as response:
                print("X Filtered Stream 已连接。", flush=True)
                record_stream_recovery(phase="stream_connected")
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
                        deliver_post(username, post)
                    else:
                        print(f"忽略已见过的帖子：{post.get('id')}", flush=True)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            error_text = f"HTTP {exc.code}: {body}"
            print(f"X stream HTTP {exc.code}: {body}", flush=True)
            record_stream_failure(error_text, status_code=exc.code, phase="stream")
            if exc.code == 429:
                backoff = max(backoff, 300)
            elif exc.code == 401 and refresh_oauth2_token():
                print("X stream token 已刷新，下次重连使用新 X_ACCESS_TOKEN。", flush=True)
                backoff = 5
        except Exception as exc:
            error_text = str(exc)
            print(f"X stream 连接失败：{error_text}", flush=True)
            record_stream_failure(error_text, phase="stream")

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
            record_stream_recovery(phase="startup_check")
            break
        except SystemExit as exc:
            error_text = str(exc)
            print(f"X API 启动校验失败：{error_text}", flush=True)
            record_stream_failure(error_text, phase="startup")
        except Exception as exc:
            error_text = str(exc)
            print(f"X API 启动校验失败：{error_text}", flush=True)
            record_stream_failure(error_text, phase="startup")
        print(f"{startup_backoff} 秒后重试 X API 启动校验。", flush=True)
        time.sleep(startup_backoff)
        startup_backoff = min(startup_backoff * 2, 300)
    stream_forever(username)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
