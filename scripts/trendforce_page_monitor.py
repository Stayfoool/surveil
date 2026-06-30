#!/usr/bin/env python3
"""Low-frequency monitors for TrendForce official list pages without usable RSS."""

from __future__ import annotations

import argparse
import html
import os
import re
import sqlite3
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

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
from db_utils import ensure_trendforce_page_seen_table, retry_on_locked
from feishu import send_card
from http_utils import http_get
from llm_analysis import llm_config
from rss_monitor import connect_db, fetch_article_body, parse_date, strip_tags
from source_health import record_source_failure, record_source_success
from skeptic_evaluator import apply_skeptic_review
from trendforce_sources import PageSource, TREND_FORCE_PAGE_SOURCES, is_focus_item
from x_check import load_env


ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / ".env"
PAGE_SOURCE_KEY = "trendforce_page"


def fetch_html(url: str) -> str:
    response = http_get(
        url,
        headers={"Accept": "text/html,application/xhtml+xml"},
        timeout=int(os.getenv("TRENDFORCE_PAGE_TIMEOUT_SECONDS", "35")),
        retries=int(os.getenv("TRENDFORCE_PAGE_RETRY_COUNT", os.getenv("SURVEIL_HTTP_RETRY_COUNT", "2"))),
    )
    return response.content.decode("utf-8", errors="replace")


def clean_text(value: str) -> str:
    value = strip_tags(value)
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def first_match(patterns: list[str], text: str) -> str:
    for pattern in patterns:
        match = re.search(pattern, text, re.I | re.S)
        if match:
            cleaned = clean_text(match.group(1))
            if cleaned:
                return cleaned
    return ""


def parse_page_date(value: str) -> str:
    if not value:
        return ""
    normalized = value.strip().replace("/", "-")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", normalized):
        return f"{normalized}T00:00:00+08:00"
    parsed = parse_date(value)
    return parsed if parsed != value else normalized


def article_id(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))


def item_from_anchor(
    source: PageSource,
    href: str,
    anchor_html: str,
    context_html: str,
    title_patterns: list[str],
    summary_patterns: list[str],
) -> dict | None:
    url = urllib.parse.urljoin(source.url, href)
    title = first_match(title_patterns, anchor_html) or first_match(title_patterns, context_html)
    if not title:
        title = clean_text(anchor_html)
    if not title or len(title) < 8:
        return None

    date_match = re.search(r"\b20\d{2}[/-]\d{2}[/-]\d{2}\b", anchor_html) or re.search(
        r"\b20\d{2}[/-]\d{2}[/-]\d{2}\b", context_html
    )
    summary = first_match(summary_patterns, anchor_html) or first_match(summary_patterns, context_html)
    published_at = parse_page_date(date_match.group(0) if date_match else "")
    if not is_focus_item(title, summary, source.module, url):
        return None

    return {
        "id": article_id(url),
        "url": url,
        "title": title,
        "summary": summary,
        "published_at": published_at,
        "source_module": source.module,
        "source_display": source.module,
        "access_note": source.access_note,
        "body_source": "TrendForce 官方列表页摘要",
        "page_source": source.name,
    }


def extract_research_items(source: PageSource, html_text: str) -> list[dict]:
    items: list[dict] = []
    seen: set[str] = set()
    pattern = re.compile(
        r"<a\b(?=[^>]*href=[\"']([^\"']*/research/download/RP[^\"']+)[\"'])[^>]*>(.*?)</a>",
        re.I | re.S,
    )
    for match in pattern.finditer(html_text):
        start = max(0, match.start() - 1800)
        end = min(len(html_text), match.end() + 4200)
        item = item_from_anchor(
            source,
            match.group(1),
            match.group(2),
            html_text[start:end],
            [
                r"<strong[^>]*>(.*?)</strong>",
                r"<h2[^>]*class=[\"'][^\"']*card-title[^\"']*[\"'][^>]*>(.*?)</h2>",
                r"<h3[^>]*>(.*?)</h3>",
            ],
            [
                r"<p[^>]*class=[\"'][^\"']*card-desc[^\"']*[\"'][^>]*>(.*?)</p>",
                r"<p[^>]*class=[\"'][^\"']*text-ellipsis-2[^\"']*[\"'][^>]*>(.*?)</p>",
            ],
        )
        if item and item["id"] not in seen:
            seen.add(item["id"])
            items.append(item)
    return items


def extract_news_items(source: PageSource, html_text: str) -> list[dict]:
    items: list[dict] = []
    seen: set[str] = set()
    pattern = re.compile(
        r"<a\b(?=[^>]*href=[\"']([^\"']*/news/\d{4}/\d{2}/\d{2}/[^\"']+)[\"'])[^>]*>(.*?)</a>",
        re.I | re.S,
    )
    for match in pattern.finditer(html_text):
        start = max(0, match.start() - 1300)
        end = min(len(html_text), match.end() + 2600)
        item = item_from_anchor(
            source,
            match.group(1),
            match.group(2),
            html_text[start:end],
            [
                r"<strong[^>]*>(.*?)</strong>",
                r"<h2[^>]*>(.*?)</h2>",
            ],
            [
                r"<p[^>]*>(.*?)</p>",
            ],
        )
        if item and item["id"] not in seen:
            seen.add(item["id"])
            if not item.get("published_at"):
                date_match = re.search(r"/news/(\d{4})/(\d{2})/(\d{2})/", item["url"])
                if date_match:
                    item["published_at"] = f"{date_match.group(1)}-{date_match.group(2)}-{date_match.group(3)}T00:00:00+08:00"
            item["body_source"] = "TrendForce News 页面正文"
            items.append(item)
    return items


def extract_press_analysis_items(source: PageSource, html_text: str) -> list[dict]:
    items: list[dict] = []
    seen: set[str] = set()
    pattern = re.compile(
        r"<a\b(?=[^>]*class=[\"'][^\"']*title-link[^\"']*[\"'])(?=[^>]*href=[\"']([^\"']+)[\"'])[^>]*>(.*?)</a>",
        re.I | re.S,
    )
    for match in pattern.finditer(html_text):
        href = match.group(1)
        if "presscenter/analysis?page=" in href or href.startswith("#"):
            continue
        start = max(0, match.start() - 1300)
        end = min(len(html_text), match.end() + 2600)
        item = item_from_anchor(
            source,
            href,
            match.group(2),
            html_text[start:end],
            [
                r"<strong[^>]*>(.*?)</strong>",
                r"<h3[^>]*>(.*?)</h3>",
            ],
            [
                r"<p[^>]*>(.*?)</p>",
            ],
        )
        if item and item["id"] not in seen:
            seen.add(item["id"])
            item["body_source"] = "TrendForce Press Centre 列表页摘要"
            items.append(item)
    return items


def extract_items(source: PageSource) -> list[dict]:
    html_text = fetch_html(source.url)
    if source.kind in {"research", "selected_topics"}:
        return extract_research_items(source, html_text)
    if source.kind == "news":
        return extract_news_items(source, html_text)
    if source.kind == "press_analysis":
        return extract_press_analysis_items(source, html_text)
    raise ValueError(f"未知 TrendForce 页面类型：{source.kind}")


def ensure_page_seen_table(conn: sqlite3.Connection) -> None:
    ensure_trendforce_page_seen_table(conn)


def source_initialized(conn: sqlite3.Connection, source_name: str) -> bool:
    row = conn.execute("SELECT 1 FROM seen_sources WHERE source = ? LIMIT 1", (source_name,)).fetchone()
    return row is not None


def save_new_page_items(
    conn: sqlite3.Connection,
    source: PageSource,
    items: list[dict],
    notify_baseline: bool = False,
) -> list[dict]:
    ensure_page_seen_table(conn)
    source_name = source.name
    is_baseline = not source_initialized(conn, source_name)
    now = datetime.now(timezone.utc).isoformat()
    if is_baseline:
        conn.execute(
            "INSERT OR IGNORE INTO seen_sources (source, first_seen_at) VALUES (?, ?)",
            (source_name, now),
        )

    new_items: list[dict] = []
    for item in sorted(items, key=lambda entry: entry.get("published_at") or ""):
        item_id = str(item["id"])
        try:
            conn.execute(
                """
                INSERT INTO seen_items (
                    source, item_id, url, title, summary, published_at, first_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source_name,
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

        globally_new = True
        try:
            conn.execute(
                """
                INSERT INTO trendforce_page_seen_items (
                    item_id, url, title, first_source, first_seen_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    item_id,
                    item.get("url", ""),
                    item.get("title", ""),
                    source_name,
                    now,
                ),
            )
        except sqlite3.IntegrityError:
            globally_new = False

        if is_baseline and not notify_baseline:
            continue
        if globally_new:
            new_items.append(item)

    conn.commit()
    if is_baseline and not notify_baseline:
        print(f"{source.name}: 首次建立基线 {len(items)} 条，默认不发送旧内容。")
    return new_items


def save_new_page_items_with_retry(
    source: PageSource,
    items: list[dict],
    notify_baseline: bool = False,
) -> list[dict]:
    def operation() -> list[dict]:
        with connect_db() as conn:
            return save_new_page_items(conn, source, items, notify_baseline=notify_baseline)

    return retry_on_locked(operation)


def enrich_item(item: dict) -> dict:
    enriched = dict(item)
    summary = clean_text(enriched.get("summary", ""))
    full_text = summary
    body_source = enriched.get("body_source", "TrendForce 官方列表页摘要")

    if enriched.get("page_source", "").startswith("trendforce_news_"):
        try:
            body, fetched_source = fetch_article_body(enriched.get("url", ""))
            if body:
                full_text = body
                body_source = fetched_source
        except Exception as exc:
            print(f"{enriched.get('url')} 正文抓取失败，回退列表摘要：{exc}")

    enriched["summary"] = summary
    enriched["full_text"] = full_text or summary
    enriched["body_source"] = body_source
    enriched.setdefault("source_display", enriched.get("source_module") or enriched.get("page_source") or "TrendForce 官方页面")
    return enriched


def notify_item(item: dict) -> None:
    enriched = enrich_item(item)
    if article_gate_enabled():
        item_id = article_item_id(enriched)
        with connect_db() as conn:
            existing = article_review_exists(conn, PAGE_SOURCE_KEY, item_id)
        if existing:
            review = existing
        else:
            try:
                review = review_article(PAGE_SOURCE_KEY, enriched)
            except Exception as exc:  # noqa: BLE001 - keep item in daily digest
                print(f"{enriched.get('page_source') or PAGE_SOURCE_KEY} 文章门控失败：{exc}", flush=True)
                review = failed_review(enriched, exc)
            with connect_db() as conn:
                review = apply_skeptic_review(
                    conn,
                    source=PAGE_SOURCE_KEY,
                    item=enriched,
                    review=review,
                    push_key="push_now",
                )
                save_article_review(conn, PAGE_SOURCE_KEY, enriched, review)
        print(
            f"{enriched.get('page_source') or PAGE_SOURCE_KEY} 文章门控：importance={review.get('importance')} "
            f"push={review.get('push_now')} title={enriched.get('title', '')}",
            flush=True,
        )
        if not review.get("push_now") or review.get("pushed_at"):
            return
        enriched["analysis_thinking"] = "enabled"
        enriched["analysis_max_tokens"] = int(os.getenv("LLM_HIGH_IMPORTANCE_MAX_OUTPUT_TOKENS", "1800"))
        enriched["analysis_lines_prefix"] = gate_lines(review)
    sent = send_card(build_article_card(PAGE_SOURCE_KEY, enriched))
    if sent and article_gate_enabled():
        with connect_db() as conn:
            mark_article_pushed(conn, PAGE_SOURCE_KEY, article_item_id(enriched))


def run_once(sources: list[PageSource], notify_baseline: bool = False) -> int:
    total_new = 0
    for source in sources:
        try:
            items = extract_items(source)
            with connect_db() as conn:
                record_source_success(conn, "trendforce_page", source.name)
            new_items = save_new_page_items_with_retry(source, items, notify_baseline=notify_baseline)
        except Exception as exc:
            with connect_db() as conn:
                record_source_failure(conn, "trendforce_page", source.name, exc)
            print(f"{source.name} 页面监控失败：{exc}")
            continue

        if not new_items:
            print(f"{source.name}: 没有发现需通知的新条目。")
            continue
        total_new += len(new_items)
        print(f"{source.name}: 发现 {len(new_items)} 条新条目。")
        for item in new_items:
            print("=" * 80)
            print(item.get("title", ""))
            print(item.get("url", ""))
            print(item.get("published_at", ""))
            notify_item(item)
    return total_new


def selected_sources(names: list[str]) -> list[PageSource]:
    if not names:
        return list(TREND_FORCE_PAGE_SOURCES)
    by_name = {source.name: source for source in TREND_FORCE_PAGE_SOURCES}
    missing = [name for name in names if name not in by_name]
    if missing:
        raise SystemExit(f"未知 TrendForce 页面源：{', '.join(missing)}")
    return [by_name[name] for name in names]


def main() -> int:
    load_env(ENV_PATH)
    config = llm_config()
    if config:
        _, base_url, model = config
        print(f"TrendForce page monitor LLM config: {base_url} / {model}", flush=True)
    else:
        print("TrendForce page monitor LLM config: 未配置", flush=True)
    parser = argparse.ArgumentParser(description="Monitor TrendForce official list pages.")
    parser.add_argument("--source", action="append", default=[], help="只监控指定 PageSource name，可重复。")
    parser.add_argument("--interval", type=int, default=int(os.getenv("TRENDFORCE_PAGE_INTERVAL", "0")))
    parser.add_argument("--notify-baseline", action="store_true", help="首次建立基线时也发送通知。默认不发送旧条目。")
    args = parser.parse_args()
    sources = selected_sources(args.source)
    notify_baseline = args.notify_baseline or os.getenv("SURVEIL_NOTIFY_BASELINE", "") == "1"

    if args.interval <= 0:
        run_once(sources, notify_baseline=notify_baseline)
        return 0

    print(f"开始监控 {len(sources)} 个 TrendForce 官方页面，轮询间隔 {args.interval} 秒。")
    while True:
        run_once(sources, notify_baseline=notify_baseline)
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
