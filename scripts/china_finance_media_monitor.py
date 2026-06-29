#!/usr/bin/env python3
"""Monitor domestic finance media sources with a shared gate/push pipeline."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import feedparser

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
from china_media_sources import (
    CHINA_MEDIA_ACCESS_NOTES,
    CHINA_MEDIA_FEEDS,
    CHINA_MEDIA_LABELS,
    china_media_access_note,
    china_media_module,
    is_china_media_source,
)
from db_utils import connect_sqlite, ensure_seen_tables, ensure_source_state_table, retry_on_locked
from env_utils import load_env
from feishu import send_card
from http_utils import http_get
from llm_analysis import llm_config
from media_keyword_config import is_media_focus_item
from rss_monitor import DB_PATH, fetch_article_body, parse_date, strip_tags
from source_health import record_source_failure, record_source_success
from time_utils import parse_datetime_to_utc_iso, timestamp_to_utc_iso


ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / ".env"

DOMESTIC_FEED_SOURCES = {
    "yicai_brief": CHINA_MEDIA_FEEDS["yicai_brief"],
    "cls_telegraph_api": CHINA_MEDIA_FEEDS["cls_telegraph_api"],
    "jin10_rsshub_important": CHINA_MEDIA_FEEDS["jin10_rsshub_important"],
}

YICAI_RSSHUB_FALLBACK = CHINA_MEDIA_FEEDS["yicai_brief_rsshub"]


def connect_db() -> sqlite3.Connection:
    return connect_sqlite(DB_PATH)


def ensure_seen_table(conn: sqlite3.Connection) -> None:
    ensure_seen_tables(conn)
    conn.commit()


def canonical_url(url: str) -> str:
    if not url:
        return ""
    parsed = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    query = [(key, value) for key, value in query if key not in {"utm_source", "utm_medium", "utm_campaign", "from"}]
    return urllib.parse.urlunparse(parsed._replace(query=urllib.parse.urlencode(query)))


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", "", (value or "").replace("Ａ", "A")).casefold()


def title_similarity(a: str, b: str) -> bool:
    if not a or not b:
        return False
    na = normalize_text(a)
    nb = normalize_text(b)
    return na == nb or na in nb or nb in na


def balanced_json_prefix(raw: str) -> str | None:
    start = raw.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escape = False
    for index in range(start, len(raw)):
        char = raw[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return raw[start : index + 1]
    return None


def fetch_json(url: str) -> list[dict[str, Any]]:
    response = http_get(
        url,
        headers={
            "Accept": "application/json,text/plain,*/*",
        },
        timeout=int(os.getenv("CHINA_MEDIA_FETCH_TIMEOUT_SECONDS", "20")),
    )
    body = response.content.decode("utf-8", errors="replace")
    data = json.loads(body)
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("data", "list", "items", "result"):
            value = data.get(key)
            if isinstance(value, list):
                return value
    return []


def cls_sign(params: dict[str, str]) -> str:
    """Sign CLS public frontend API params.

    The production web/mobile frontend signs the sorted query string with
    sha1 first, then md5 over the sha1 hex digest. The sign field itself is
    intentionally excluded from params.
    """
    qs = "&".join(f"{key}={value}" for key, value in sorted(params.items()))
    return hashlib.md5(hashlib.sha1(qs.encode("utf-8")).hexdigest().encode("utf-8")).hexdigest()


def parse_cls_time(value: Any) -> str:
    if value in (None, ""):
        return ""
    raw = str(value).strip()
    if not raw:
        return ""
    if raw.isdigit():
        return timestamp_to_utc_iso(raw)
    return parse_datetime_to_utc_iso(raw)


def env_int(name: str, default: int, minimum: int = 0) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return max(minimum, int(raw))
    except ValueError:
        return default


def load_source_state(source: str) -> dict[str, Any]:
    with connect_db() as conn:
        ensure_source_state_table(conn)
        row = conn.execute("SELECT state_json FROM source_state WHERE source = ?", (source,)).fetchone()
    if not row:
        return {}
    try:
        parsed = json.loads(row[0] or "{}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def save_source_state(source: str, state: dict[str, Any]) -> None:
    with connect_db() as conn:
        ensure_source_state_table(conn)
        conn.execute(
            """
            INSERT INTO source_state (source, state_json, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(source) DO UPDATE SET
                state_json = excluded.state_json,
                updated_at = excluded.updated_at
            """,
            (source, json.dumps(state, ensure_ascii=False, sort_keys=True), datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()


def should_skip_cls_poll(source: str, *, force: bool = False) -> bool:
    if force or os.getenv("CLS_FORCE_FETCH", "").strip() == "1":
        return False
    min_seconds = env_int("CLS_MIN_POLL_SECONDS", 60, minimum=0)
    if min_seconds <= 0:
        return False
    state = load_source_state(source)
    last_fetch = str(state.get("last_fetch_at") or "")
    if not last_fetch:
        return False
    try:
        elapsed = datetime.now(timezone.utc).timestamp() - datetime.fromisoformat(last_fetch).timestamp()
    except ValueError:
        return False
    if elapsed < min_seconds:
        print(f"财联社公开前端 API 距上次抓取 {elapsed:.1f}s，小于 {min_seconds}s，跳过本轮。", flush=True)
        return True
    return False


def parse_first_finance_items() -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    try:
        rows = fetch_json(DOMESTIC_FEED_SOURCES["yicai_brief"])
    except Exception as exc:
        print(f"第一财经公开 JSON 读取失败，尝试 RSSHub：{exc}", flush=True)
        rows = fetch_json(YICAI_RSSHUB_FALLBACK)

    for row in rows:
        if not isinstance(row, dict):
            continue
        title = str(row.get("newcontent") or row.get("title") or row.get("LiveTitle") or "").strip()
        url = str(row.get("ShareUrl") or row.get("url") or row.get("link") or "").strip()
        if not title and not url:
            continue
        summary = str(row.get("LiveContent") or row.get("summary") or row.get("description") or row.get("newcontent") or "").strip()
        published_at = str(row.get("CreateDate") or row.get("published_at") or row.get("pubDate") or "").strip()
        item_id = str(row.get("LiveID") or url or title)
        items.append(
            {
                "id": item_id,
                "url": canonical_url(url),
                "title": title,
                "summary": strip_tags(summary),
                "content": "",
                "published_at": parse_date(published_at),
                "source_module": CHINA_MEDIA_LABELS["yicai_brief"],
                "access_note": CHINA_MEDIA_ACCESS_NOTES["yicai_brief"],
                "body_source": "公开 JSON",
            }
        )
    return items


def parse_cls_items() -> list[dict[str, Any]]:
    source = "cls_telegraph_api"
    if should_skip_cls_poll(source):
        return []
    params = {
        "app": "CailianpressWeb",
        "category": os.getenv("CLS_ROLL_CATEGORY", ""),
        "lastTime": os.getenv("CLS_ROLL_LAST_TIME", ""),
        "os": "web",
        "refresh_type": os.getenv("CLS_ROLL_REFRESH_TYPE", "1"),
        "rn": os.getenv("CLS_ROLL_RN", "20"),
        "sv": os.getenv("CLS_ROLL_SV", "7.7.5"),
    }
    signed_params = dict(params)
    signed_params["sign"] = cls_sign(params)
    url = f"{DOMESTIC_FEED_SOURCES['cls_telegraph_api']}?{urllib.parse.urlencode(signed_params)}"
    try:
        response = http_get(
            url,
            headers={
                "Accept": "application/json, text/plain, */*",
                "Referer": "https://m.cls.cn/telegraph",
            },
            timeout=int(os.getenv("CLS_FETCH_TIMEOUT_SECONDS", os.getenv("CHINA_MEDIA_FETCH_TIMEOUT_SECONDS", "20"))),
        )
        data = json.loads(response.content.decode("utf-8", errors="replace"))
    except Exception as exc:
        print(f"财联社公开前端 API 读取失败：{exc}", flush=True)
        raise
    save_source_state(source, {"last_fetch_at": datetime.now(timezone.utc).isoformat()})

    if not isinstance(data, dict):
        raise RuntimeError("财联社公开前端 API 响应格式异常：root 不是 JSON object")
    errno = data.get("errno", data.get("errNo", data.get("code", 0)))
    if errno not in (0, "0", None):
        message = data.get("msg") or data.get("message") or data.get("error") or ""
        raise RuntimeError(f"财联社公开前端 API 返回错误：errno={errno} message={message}")

    payload = data.get("data")
    rows = payload.get("roll_data") if isinstance(payload, dict) else []
    if not isinstance(rows, list):
        rows = []
    items: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        title = str(row.get("content") or row.get("title") or "").strip()
        title = strip_tags(title)
        url = str(row.get("shareurl") or row.get("shareUrl") or row.get("url") or "").strip()
        if not title:
            continue
        ctime = row.get("ctime") or row.get("time") or row.get("published_at") or ""
        item_id = str(row.get("id") or row.get("telegraphId") or row.get("ctime") or url or title)
        items.append(
            {
                "id": item_id,
                "url": canonical_url(url),
                "title": title,
                "summary": title,
                "content": "",
                "published_at": parse_cls_time(ctime),
                "source_module": CHINA_MEDIA_LABELS["cls_telegraph_api"],
                "access_note": CHINA_MEDIA_ACCESS_NOTES["cls_telegraph_api"],
                "body_source": "公开前端 API",
            }
        )
    return items


def parse_jin10_items() -> list[dict[str, Any]]:
    feed = CHINA_MEDIA_FEEDS["jin10_rsshub_important"]
    try:
        response = http_get(
            feed,
            headers={"Accept": "application/rss+xml, application/xml, text/xml"},
            timeout=int(os.getenv("CHINA_MEDIA_FETCH_TIMEOUT_SECONDS", "20")),
        )
        parsed = feedparser.parse(response.content)
    except Exception as exc:
        print(f"金十 RSSHub 读取失败：{exc}", flush=True)
        raise

    items: list[dict[str, Any]] = []
    for item in parsed.entries:
        title = str(item.get("title") or "").strip()
        url = str(item.get("link") or "").strip()
        summary = str(item.get("summary") or item.get("description") or "").strip()
        guid = str(item.get("id") or item.get("guid") or url or title).strip()
        published_at = parse_date(str(item.get("published") or item.get("updated") or "").strip())
        items.append(
            {
                "id": guid,
                "url": canonical_url(url),
                "title": title,
                "summary": strip_tags(summary),
                "content": "",
                "published_at": published_at,
                "source_module": CHINA_MEDIA_LABELS["jin10_rsshub_important"],
                "access_note": CHINA_MEDIA_ACCESS_NOTES["jin10_rsshub_important"],
                "body_source": "RSSHub",
            }
        )
    return items


def source_items(source: str) -> list[dict[str, Any]]:
    if source == "yicai_brief":
        return parse_first_finance_items()
    if source == "cls_telegraph_api":
        return parse_cls_items()
    if source == "jin10_rsshub_important":
        return parse_jin10_items()
    return []


def enrich_item(source: str, item: dict[str, Any]) -> dict[str, Any]:
    enriched = dict(item)
    body = ""
    body_source = str(enriched.get("body_source") or "公开页面")
    if source == "yicai_brief" and enriched.get("url"):
        try:
            body, body_source = fetch_article_body(enriched["url"])
        except Exception as exc:
            print(f"第一财经正文抓取失败，回退摘要：{exc}", flush=True)
    if source in {"cls_telegraph_api", "jin10_rsshub_important"} and enriched.get("url"):
        try:
            body, body_source = fetch_article_body(enriched["url"])
        except Exception:
            pass
    enriched["full_text"] = body or str(enriched.get("content") or enriched.get("summary") or "")
    enriched["body_source"] = body_source if body else body_source
    enriched.setdefault("source_module", china_media_module(source))
    enriched.setdefault("access_note", china_media_access_note(source, enriched["body_source"]))
    return enriched


def seen_source(conn: sqlite3.Connection, source: str) -> bool:
    row = conn.execute("SELECT 1 FROM seen_sources WHERE source = ? LIMIT 1", (source,)).fetchone()
    return row is not None


def save_new_items(
    conn: sqlite3.Connection,
    source: str,
    items: Iterable[dict[str, Any]],
    notify_baseline: bool = False,
) -> list[dict[str, Any]]:
    ensure_seen_table(conn)
    items_list = list(items)
    is_baseline = not seen_source(conn, source)
    if is_baseline:
        conn.execute(
            "INSERT OR IGNORE INTO seen_sources (source, first_seen_at) VALUES (?, ?)",
            (source, datetime.now(timezone.utc).isoformat()),
        )
    now = datetime.now(timezone.utc).isoformat()
    new_items: list[dict[str, Any]] = []
    seen_titles: list[str] = []
    for item in sorted(items_list, key=lambda row: row.get("published_at") or "", reverse=False):
        title = str(item.get("title") or "").strip()
        url = canonical_url(str(item.get("url") or "").strip())
        item_id = str(item.get("id") or url or title)
        if not title and not url:
            continue
        if any(title_similarity(title, prior) for prior in seen_titles):
            continue
        seen_titles.append(title)
        try:
            conn.execute(
                """
                INSERT INTO seen_items (source, item_id, url, title, summary, published_at, first_seen_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source,
                    item_id,
                    url,
                    title,
                    str(item.get("summary") or ""),
                    str(item.get("published_at") or ""),
                    now,
                ),
            )
        except sqlite3.IntegrityError:
            continue
        new_items.append(item)
    conn.commit()
    if is_baseline and not notify_baseline:
        print(f"{china_media_module(source)}: 首次建立基线 {len(items_list)} 条，默认不发送旧内容。", flush=True)
        return []
    return new_items


def save_new_items_with_retry(
    source: str,
    items: Iterable[dict[str, Any]],
    notify_baseline: bool = False,
) -> list[dict[str, Any]]:
    def operation() -> list[dict[str, Any]]:
        with connect_db() as conn:
            return save_new_items(conn, source, items, notify_baseline=notify_baseline)

    return retry_on_locked(operation)


def should_focus_item(item: dict[str, Any]) -> bool:
    return is_media_focus_item(
        str(item.get("title") or ""),
        str(item.get("summary") or ""),
        str(item.get("full_text") or ""),
        str(item.get("source_module") or ""),
    )


def notify_item(source: str, item: dict[str, Any]) -> None:
    enriched = enrich_item(source, item)
    if not should_focus_item(enriched):
        return
    if article_gate_enabled():
        item_id = article_item_id(enriched)
        with connect_db() as conn:
            existing = article_review_exists(conn, source, item_id)
        if existing:
            review = existing
        else:
            try:
                review = review_article(source, enriched)
            except Exception as exc:  # noqa: BLE001
                print(f"{source} 文章门控失败：{exc}", flush=True)
                review = failed_review(enriched, exc)
            with connect_db() as conn:
                save_article_review(conn, source, enriched, review)
        print(
            f"{source} 文章门控：importance={review.get('importance')} push={review.get('push_now')} title={enriched.get('title', '')}",
            flush=True,
        )
        if not review.get("push_now") or review.get("pushed_at"):
            return
        enriched["analysis_thinking"] = "enabled"
        enriched["analysis_max_tokens"] = int(os.getenv("LLM_HIGH_IMPORTANCE_MAX_OUTPUT_TOKENS", "1800"))
        enriched["analysis_lines_prefix"] = gate_lines(review)
    sent = send_card(build_article_card(source, enriched))
    if sent and article_gate_enabled():
        with connect_db() as conn:
            mark_article_pushed(conn, source, article_item_id(enriched))


def run_once(sources: list[str], notify_baseline: bool = False) -> int:
    if not sources:
        return 0
    total_new = 0
    fetched: dict[str, list[dict[str, Any]]] = {}
    max_workers = min(len(sources), env_int("CHINA_MEDIA_FETCH_MAX_WORKERS", 3, minimum=1))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(source_items, source): source for source in sources}
        for future in as_completed(futures):
            source = futures[future]
            try:
                fetched[source] = future.result()
                with connect_db() as conn:
                    record_source_success(conn, "china_finance_media", source)
            except Exception as exc:
                with connect_db() as conn:
                    record_source_failure(conn, "china_finance_media", source, exc)
                print(f"{china_media_module(source)} 抓取失败：{exc}", flush=True)
    for source in sources:
        if source not in fetched:
            continue
        items = fetched[source]
        try:
            new_items = save_new_items_with_retry(source, items, notify_baseline=notify_baseline)
        except Exception as exc:
            with connect_db() as conn:
                record_source_failure(conn, "china_finance_media", source, exc)
            print(f"{china_media_module(source)} 处理失败：{exc}", flush=True)
            continue
        if not new_items:
            print(f"{china_media_module(source)}：没有发现新条目。", flush=True)
            continue
        total_new += len(new_items)
        print(f"{china_media_module(source)}：发现 {len(new_items)} 条新条目。", flush=True)
        for item in new_items:
            notify_item(source, item)
    return total_new


def parse_sources_arg(raw: list[str]) -> list[str]:
    if not raw:
        return ["yicai_brief", "cls_telegraph_api", "jin10_rsshub_important"]
    sources = []
    for part in raw:
        for name in part.split(","):
            name = name.strip()
            if name:
                sources.append(name)
    invalid = [name for name in sources if not is_china_media_source(name)]
    if invalid:
        raise SystemExit(f"未知中国财经媒体源：{', '.join(invalid)}")
    return sources


def main() -> int:
    load_env(ENV_PATH)
    config = llm_config()
    if config:
        _, base_url, model = config
        print(f"China finance media monitor LLM config: {base_url} / {model}", flush=True)
    else:
        print("China finance media monitor LLM config: 未配置", flush=True)

    parser = argparse.ArgumentParser(description="Monitor domestic finance media sources.")
    parser.add_argument("--source", action="append", default=[], help="Source name, repeatable or comma separated.")
    parser.add_argument("--interval", type=int, default=0, help="Polling interval in seconds. 0 means run once.")
    parser.add_argument("--notify-baseline", action="store_true", help="首次建立基线时也发送通知。默认不发送旧条目。")
    args = parser.parse_args()
    sources = parse_sources_arg(args.source)
    notify_baseline = args.notify_baseline or os.getenv("SURVEIL_NOTIFY_BASELINE", "") == "1"

    if args.interval <= 0:
        run_once(sources, notify_baseline=notify_baseline)
        return 0

    print(f"开始监控 {len(sources)} 个中国财经媒体源，轮询间隔 {args.interval} 秒。", flush=True)
    while True:
        run_once(sources, notify_baseline=notify_baseline)
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
