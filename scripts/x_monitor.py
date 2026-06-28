#!/usr/bin/env python3
"""Poll X for new posts from a configured account and deduplicate them."""

from __future__ import annotations

import argparse
import os
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from cards import build_serenity_card
from db_utils import connect_sqlite, retry_on_locked
from feishu import send_card
from link_enrichment import enrich_post_links
from x_check import configured_x_username, load_env, post_text, request_with_available_tokens


ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / ".env"
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "surveil.sqlite3"
MEDIA_FIELDS = "media_key,type,url,preview_image_url,width,height,variants"


def media_url_from_item(item: dict) -> str:
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


def x_status_refs_from_links(post: dict) -> list[dict]:
    refs: list[dict] = []
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


def x_status_ids_from_links(post: dict) -> list[str]:
    return [ref["status_id"] for ref in x_status_refs_from_links(post)]


def attach_linked_status_context(post: dict) -> None:
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
            print(f"X 链接帖媒体查询失败 {status_id}: {exc}")
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


def merge_linked_status_links(post: dict) -> None:
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


def attach_linked_status_media(post: dict) -> None:
    attach_linked_status_context(post)


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


def fetch_recent_posts(username: str, max_results: int) -> list[dict]:
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
    posts = tweets.get("data", [])
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


def save_new_posts(conn: sqlite3.Connection, username: str, posts: list[dict]) -> list[dict]:
    new_posts: list[dict] = []
    now = datetime.now(timezone.utc).isoformat()
    for post in sorted(posts, key=lambda item: item.get("created_at", "")):
        post_id = str(post["id"])
        text = post_text(post).strip()
        url = f"https://x.com/{username}/status/{post_id}"
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
            continue
        post = dict(post)
        post["url"] = url
        post["full_text"] = text
        new_posts.append(post)
    conn.commit()
    return new_posts


def save_new_posts_with_retry(username: str, posts: list[dict]) -> list[dict]:
    def operation() -> list[dict]:
        with connect_db() as conn:
            return save_new_posts(conn, username, posts)

    return retry_on_locked(operation)


def print_post(post: dict) -> None:
    print("=" * 80)
    print(post["url"])
    print(post.get("created_at", "unknown time"))
    metrics = post.get("public_metrics") or {}
    if metrics:
        print(
            "metrics: "
            f"replies={metrics.get('reply_count')} "
            f"reposts={metrics.get('retweet_count')} "
            f"likes={metrics.get('like_count')} "
            f"quotes={metrics.get('quote_count')}"
        )
    print()
    print(post.get("full_text") or post.get("text") or "")


def notify_post(post: dict) -> None:
    attach_linked_status_context(post)
    try:
        links = enrich_post_links(post)
        if links:
            print(f"已抓取 X 外链 {len(links)} 条。")
    except Exception as exc:
        print(f"X 外链抓取失败：{exc}")
        post["_links"] = []
    merge_linked_status_links(post)
    send_card(build_serenity_card(post))


def run_once(username: str, max_results: int) -> int:
    posts = fetch_recent_posts(username, max_results)
    new_posts = save_new_posts_with_retry(username, posts)
    if not new_posts:
        print("没有发现新帖。")
        return 0
    print(f"发现 {len(new_posts)} 条新帖：")
    for post in new_posts:
        print_post(post)
        notify_post(post)
    return len(new_posts)


def main() -> int:
    load_env(ENV_PATH)
    parser = argparse.ArgumentParser(description="Monitor X posts from one account.")
    parser.add_argument("--username", default=None)
    parser.add_argument("--max-results", type=int, default=10)
    parser.add_argument("--interval", type=int, default=0, help="Polling interval in seconds. 0 means run once.")
    args = parser.parse_args()

    username = args.username.lstrip("@") if args.username else configured_x_username()

    if args.max_results < 5 or args.max_results > 100:
        raise SystemExit("--max-results 必须在 5 到 100 之间。")

    if args.interval <= 0:
        run_once(username, args.max_results)
        return 0

    print(f"开始监控 @{username}，轮询间隔 {args.interval} 秒。")
    while True:
        try:
            run_once(username, args.max_results)
        except Exception as exc:
            print(f"本轮监控失败：{exc}")
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
