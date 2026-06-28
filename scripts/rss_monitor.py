#!/usr/bin/env python3
"""Poll RSS feeds and deduplicate new articles."""

from __future__ import annotations

import argparse
import html
import os
import re
import sqlite3
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Iterable

from article_gate import (
    article_gate_enabled,
    article_item_id,
    failed_review,
    gate_lines,
    mark_pushed as mark_article_pushed,
    review_article,
    review_exists as article_review_exists,
    save_review as save_article_review,
)
from cards import build_article_card
from db_utils import connect_sqlite, retry_on_locked
from feishu import send_card
from llm_analysis import llm_config
from media_sources import is_overseas_media_source, overseas_media_access_note, overseas_media_module
from media_keyword_config import is_media_focus_item
from official_news_gate import (
    analysis_lines_from_review,
    is_official_news_source,
    mark_pushed,
    official_news_enabled,
    review_exists,
    review_official_news,
    save_review,
)
from trendforce_sources import DEFAULT_RSS_FEEDS
from x_check import load_env


ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / ".env"
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "surveil.sqlite3"

DEFAULT_FEEDS = DEFAULT_RSS_FEEDS

CORE_COMPANY_FEEDS = {
    "openai_news",
    "nvidia_blog",
    "nvidia_developer_blog",
    "samsung_semiconductor_news",
    "samsung_global_semiconductor",
    "skhynix_newsroom",
    "micron_news_releases",
}


def connect_db() -> sqlite3.Connection:
    conn = connect_sqlite(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS seen_items (
            source TEXT NOT NULL,
            item_id TEXT NOT NULL,
            url TEXT NOT NULL,
            title TEXT NOT NULL,
            summary TEXT,
            published_at TEXT,
            first_seen_at TEXT NOT NULL,
            PRIMARY KEY (source, item_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS seen_sources (
            source TEXT PRIMARY KEY,
            first_seen_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO seen_sources (source, first_seen_at)
        SELECT source, MIN(first_seen_at)
        FROM seen_items
        GROUP BY source
        """
    )
    conn.commit()
    return conn


def text_of(parent: ET.Element, tag: str) -> str:
    child = parent.find(tag)
    return (child.text or "").strip() if child is not None else ""


def namespaced_text(parent: ET.Element, namespace: str, tag: str) -> str:
    child = parent.find(f"{{{namespace}}}{tag}")
    return (child.text or "").strip() if child is not None else ""


def element_text(element: ET.Element | None) -> str:
    if element is None:
        return ""
    return "".join(element.itertext()).strip()


def atom_link(entry: ET.Element, atom_ns: str) -> str:
    fallback = ""
    for link in entry.findall(f"{{{atom_ns}}}link"):
        href = (link.attrib.get("href") or "").strip()
        if not href:
            continue
        rel = (link.attrib.get("rel") or "alternate").strip().lower()
        if rel == "alternate":
            return href
        if not fallback:
            fallback = href
    return fallback


def parse_atom_date(value: str) -> str:
    if not value:
        return ""
    try:
        normalized = value.replace("Z", "+00:00")
        return datetime.fromisoformat(normalized).astimezone(timezone.utc).isoformat()
    except ValueError:
        return parse_date(value)


def parse_date(value: str) -> str:
    if not value:
        return ""
    try:
        return parsedate_to_datetime(value).astimezone(timezone.utc).isoformat()
    except (TypeError, ValueError, IndexError, AttributeError):
        return value


def fetch_feed(url: str) -> list[dict]:
    timeout = int(os.getenv("RSS_FETCH_TIMEOUT_SECONDS", "15"))
    retries = int(os.getenv("RSS_FETCH_RETRY_COUNT", "1"))
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "surveil-rss-monitor/0.1",
            "Accept": "application/rss+xml, application/xml, text/xml;q=0.9, */*;q=0.8",
        },
    )
    last_error: Exception | None = None
    for attempt in range(max(1, retries + 1)):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                content = response.read()
            break
        except Exception as exc:
            last_error = exc
            if attempt >= retries:
                raise
            time.sleep(2 + attempt * 3)
    else:
        raise RuntimeError(f"RSS 抓取失败：{last_error}")
    root = ET.fromstring(content)
    if root.tag.endswith("feed"):
        return parse_atom_feed(root)
    if root.tag.endswith("RDF"):
        return parse_rdf_feed(root)
    channel = root.find("channel")
    if channel is None:
        return []
    items = []
    for item in channel.findall("item"):
        title = text_of(item, "title")
        link = text_of(item, "link")
        guid = text_of(item, "guid") or link or title
        summary = text_of(item, "description")
        content = namespaced_text(item, "http://purl.org/rss/1.0/modules/content/", "encoded")
        categories = [category.text.strip() for category in item.findall("category") if category.text]
        published_at = parse_date(text_of(item, "pubDate"))
        items.append(
            {
                "id": guid,
                "url": link,
                "title": title,
                "summary": summary,
                "content": content,
                "categories": categories,
                "published_at": published_at,
            }
        )
    return items


def parse_rdf_feed(root: ET.Element) -> list[dict]:
    rdf_ns = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
    rss_ns = "http://purl.org/rss/1.0/"
    dc_ns = "http://purl.org/dc/elements/1.1/"
    items = []
    for item in root.findall(f"{{{rss_ns}}}item"):
        title = element_text(item.find(f"{{{rss_ns}}}title"))
        link = element_text(item.find(f"{{{rss_ns}}}link"))
        guid = (item.attrib.get(f"{{{rdf_ns}}}about") or link or title).strip()
        summary = element_text(item.find(f"{{{rss_ns}}}description"))
        published_at = parse_atom_date(element_text(item.find(f"{{{dc_ns}}}date")))
        items.append(
            {
                "id": guid,
                "url": link,
                "title": title,
                "summary": summary,
                "content": "",
                "categories": [],
                "published_at": published_at,
            }
        )
    return items


def parse_atom_feed(root: ET.Element) -> list[dict]:
    atom_ns = "http://www.w3.org/2005/Atom"
    entries = root.findall(f"{{{atom_ns}}}entry")
    if not entries and root.tag == "feed":
        entries = root.findall("entry")
        atom_ns = ""

    def find(entry: ET.Element, tag: str) -> ET.Element | None:
        if atom_ns:
            return entry.find(f"{{{atom_ns}}}{tag}")
        return entry.find(tag)

    items = []
    for entry in entries:
        title = element_text(find(entry, "title"))
        link = atom_link(entry, atom_ns) if atom_ns else ""
        if not link:
            link_el = find(entry, "link")
            link = (link_el.attrib.get("href") or element_text(link_el)).strip() if link_el is not None else ""
        guid = element_text(find(entry, "id")) or link or title
        summary = element_text(find(entry, "summary")) or element_text(find(entry, "subtitle"))
        content = element_text(find(entry, "content"))
        category_nodes = entry.findall(f"{{{atom_ns}}}category") if atom_ns else entry.findall("category")
        categories = [category.attrib.get("term", "").strip() for category in category_nodes]
        categories = [category for category in categories if category]
        published_at = parse_atom_date(element_text(find(entry, "published")) or element_text(find(entry, "updated")))
        items.append(
            {
                "id": guid,
                "url": link,
                "title": title,
                "summary": summary,
                "content": content,
                "categories": categories,
                "published_at": published_at,
            }
        )
    return items


def strip_tags(value: str) -> str:
    value = re.sub(r"(?is)<script.*?</script>|<style.*?</style>", "", value)
    value = re.sub(r"(?i)<br\s*/?>", "\n", value)
    value = re.sub(r"(?i)</p>", "\n\n", value)
    value = re.sub(r"(?s)<[^>]+>", "", value)
    return html.unescape(value).strip()


def fetch_article_body(url: str) -> tuple[str, str]:
    if not url:
        return "", "RSS"
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "surveil-rss-monitor/0.1",
            "Accept": "text/html,application/xhtml+xml",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        html_text = response.read().decode("utf-8", errors="replace")

    paragraphs = re.findall(r"(?is)<p[^>]*>(.*?)</p>", html_text)
    cleaned = [strip_tags(p) for p in paragraphs]
    cleaned = [
        p
        for p in cleaned
        if len(p) > 40
        and not p.lower().startswith(("copyright", "related", "for more information"))
        and "cookie" not in p.lower()
    ]
    if cleaned:
        return "\n\n".join(cleaned), "页面正文"

    meta = re.search(r'<meta\s+name=["\']description["\']\s+content=["\'](.*?)["\']', html_text, re.I | re.S)
    if meta:
        return html.unescape(meta.group(1)).strip(), "页面 meta description"
    return "", "RSS"


def source_has_seen(conn: sqlite3.Connection, source: str) -> bool:
    row = conn.execute("SELECT 1 FROM seen_sources WHERE source = ? LIMIT 1", (source,)).fetchone()
    return row is not None


def save_new_items(
    conn: sqlite3.Connection,
    source: str,
    items: Iterable[dict],
    notify_baseline: bool = False,
    source_label: str | None = None,
) -> list[dict]:
    items_list = list(items)
    new_items: list[dict] = []
    is_baseline = not source_has_seen(conn, source)
    if is_baseline:
        conn.execute(
            "INSERT OR IGNORE INTO seen_sources (source, first_seen_at) VALUES (?, ?)",
            (source, datetime.now(timezone.utc).isoformat()),
        )
    now = datetime.now(timezone.utc).isoformat()
    for item in sorted(items_list, key=lambda entry: entry.get("published_at") or ""):
        item_id = str(item["id"])
        try:
            conn.execute(
                """
                INSERT INTO seen_items (
                    source, item_id, url, title, summary, published_at, first_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source,
                    item_id,
                    item.get("url", ""),
                    item.get("title", ""),
                    item.get("summary", ""),
                    item.get("published_at", ""),
                    now,
                ),
            )
        except sqlite3.IntegrityError:
            continue
        new_items.append(item)
    conn.commit()
    if is_baseline and not notify_baseline:
        label = source_label or source
        print(f"{label}: 首次建立基线 {len(items_list)} 条，默认不发送旧内容。")
        return []
    return new_items


def save_new_items_with_retry(
    source: str,
    items: Iterable[dict],
    notify_baseline: bool = False,
    source_label: str | None = None,
) -> list[dict]:
    def operation() -> list[dict]:
        with connect_db() as conn:
            return save_new_items(
                conn,
                source,
                items,
                notify_baseline=notify_baseline,
                source_label=source_label,
            )

    return retry_on_locked(operation)


def enrich_item(source: str, item: dict) -> dict:
    body = ""
    body_source = "RSS"
    should_fetch_body = True
    if source.startswith("digitimes_") and os.getenv("DIGITIMES_FETCH_BODY", "").strip() != "1":
        should_fetch_body = False
        body_source = "RSS description"
    if should_fetch_body:
        try:
            body, body_source = fetch_article_body(item.get("url", ""))
        except Exception as exc:
            print(f"{source} 正文抓取失败，回退 RSS：{exc}")
    item = dict(item)
    item["full_text"] = body or strip_tags(item.get("content") or item.get("summary", ""))
    item["body_source"] = body_source if body else "RSS description"
    if is_overseas_media_source(source):
        item.setdefault("source_module", overseas_media_module(source))
        item.setdefault("access_note", overseas_media_access_note(source, item["body_source"]))
    return item


def notify_item(source: str, item: dict) -> None:
    item = enrich_item(source, item)
    if article_gate_enabled():
        item_id = article_item_id(item)
        with connect_db() as conn:
            existing = article_review_exists(conn, source, item_id)
        if existing:
            review = existing
        else:
            try:
                review = review_article(source, item)
            except Exception as exc:  # noqa: BLE001 - keep item in daily digest
                print(f"{source} 文章门控失败：{exc}", flush=True)
                review = failed_review(item, exc)
            with connect_db() as conn:
                save_article_review(conn, source, item, review)
        print(
            f"{source} 文章门控：importance={review.get('importance')} "
            f"push={review.get('push_now')} title={item.get('title', '')}",
            flush=True,
        )
        if not review.get("push_now") or review.get("pushed_at"):
            return
        item["analysis_thinking"] = "enabled"
        item["analysis_max_tokens"] = int(os.getenv("LLM_HIGH_IMPORTANCE_MAX_OUTPUT_TOKENS", "1800"))
        item["analysis_lines_prefix"] = gate_lines(review)
    sent = send_card(build_article_card(source, item))
    if sent and article_gate_enabled():
        with connect_db() as conn:
            mark_article_pushed(conn, source, article_item_id(item))


def handle_official_news_item(source: str, item: dict) -> None:
    enriched = enrich_item(source, item)
    item_id = str(enriched.get("id") or enriched.get("url") or enriched.get("title") or "")
    with connect_db() as conn:
        existing = review_exists(conn, source, item_id)
    if existing:
        review = existing
    elif not official_news_enabled():
        review = {
            "importance": "medium",
            "should_push_now": False,
            "reason": "LLM 未配置，无法判定是否需要即时推送；先进入日报池。",
            "daily_summary": str(enriched.get("title") or ""),
            "analysis": {},
        }
        with connect_db() as conn:
            save_review(conn, source, enriched, review)
    else:
        review = review_official_news(source, enriched)
        with connect_db() as conn:
            save_review(conn, source, enriched, review)

    print(
        f"{source} 官网新闻分流：importance={review.get('importance')} "
        f"push={review.get('should_push_now')} title={enriched.get('title', '')}",
        flush=True,
    )
    if not review.get("should_push_now") or review.get("pushed_at"):
        return
    enriched["analysis_lines"] = analysis_lines_from_review(review)
    sent = send_card(build_article_card(source, enriched))
    if sent:
        with connect_db() as conn:
            mark_pushed(conn, source, item_id)


def filter_items(source: str, items: list[dict]) -> list[dict]:
    if not source.startswith("trendforce_") and source not in CORE_COMPANY_FEEDS and not is_overseas_media_source(source):
        return items
    filtered = []
    for item in items:
        if is_media_focus_item(
            item.get("title", ""),
            item.get("summary", ""),
            " ".join(item.get("categories", [])),
            item.get("url", ""),
        ):
            filtered.append(item)
    return filtered


def run_once(feeds: dict[str, str], notify_baseline: bool = False) -> int:
    total_new = 0
    for source, url in feeds.items():
        try:
            items = filter_items(source, fetch_feed(url))
            new_items = save_new_items_with_retry(source, items, notify_baseline=notify_baseline)
        except Exception as exc:
            print(f"{source} 抓取失败：{exc}")
            continue
        if not new_items:
            print(f"{source}: 没有发现新文章。")
            continue
        total_new += len(new_items)
        print(f"{source}: 发现 {len(new_items)} 篇新文章。")
        for item in new_items:
            print("=" * 80)
            print(item.get("title", ""))
            print(item.get("url", ""))
            print(item.get("published_at", ""))
            try:
                if is_official_news_source(source):
                    handle_official_news_item(source, item)
                else:
                    notify_item(source, item)
            except Exception as exc:  # noqa: BLE001 - keep other feeds alive
                print(f"{source} 通知失败：{exc}")
    return total_new


def parse_feed_args(feed_args: list[str]) -> dict[str, str]:
    if not feed_args:
        return dict(DEFAULT_FEEDS)
    feeds: dict[str, str] = {}
    for raw in feed_args:
        if "=" not in raw:
            raise SystemExit("--feed 格式必须是 name=url")
        name, url = raw.split("=", 1)
        feeds[name.strip()] = url.strip()
    return feeds


def main() -> int:
    load_env(ENV_PATH)
    config = llm_config()
    if config:
        _, base_url, model = config
        print(f"RSS monitor LLM config: {base_url} / {model}", flush=True)
    else:
        print("RSS monitor LLM config: 未配置", flush=True)
    parser = argparse.ArgumentParser(description="Monitor RSS feeds.")
    parser.add_argument("--feed", action="append", default=[], help="RSS feed as name=url. Repeatable.")
    parser.add_argument("--interval", type=int, default=0, help="Polling interval in seconds. 0 means run once.")
    parser.add_argument("--notify-baseline", action="store_true", help="首次建立基线时也发送通知。默认不发送旧条目。")
    args = parser.parse_args()
    feeds = parse_feed_args(args.feed)
    notify_baseline = args.notify_baseline or os.getenv("SURVEIL_NOTIFY_BASELINE", "") == "1"

    if args.interval <= 0:
        run_once(feeds, notify_baseline=notify_baseline)
        return 0

    print(f"开始监控 {len(feeds)} 个 RSS feed，轮询间隔 {args.interval} 秒。")
    while True:
        run_once(feeds, notify_baseline=notify_baseline)
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
