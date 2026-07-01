#!/usr/bin/env python3
"""Monitor Sina Finance 7x24 flash news for portfolio-related events."""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from db_utils import connect_sqlite
from env_utils import load_env
from event_pipeline import analyze_event, content_hash, load_enabled_holdings, maybe_deliver_event, upsert_event
from http_utils import http_get
from macro_policy import macro_policy_match
from market_db import DEFAULT_DB_PATH, init_db
from portfolio_import import import_holdings
from sina_zy_client import client_from_env, result_data
from source_health import record_source_failure, record_source_success
from time_utils import parse_datetime_to_utc_iso, timestamp_to_utc_iso


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = ROOT / "config" / "portfolio.json"
SOURCE = "sina_flash"
STATE_KEY = "sina_flash"
SINA_API_URL = "https://app.cj.sina.com.cn/api/news/pc"
SINA_REFERER = "https://finance.sina.com.cn/7x24/"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def env_int(name: str, default: int, minimum: int = 1) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return max(minimum, int(raw))
    except ValueError:
        return default


def news_provider() -> str:
    return os.getenv("SINA_NEWS_PROVIDER", "legacy").strip().lower() or "legacy"


def sina_symbol_to_ifind(value: str) -> str:
    raw = value.strip().lower()
    if len(raw) != 8:
        return raw.upper()
    prefix, digits = raw[:2], raw[2:]
    if not digits.isdigit():
        return raw.upper()
    if prefix == "sz":
        return f"{digits}.SZ"
    if prefix == "sh":
        return f"{digits}.SH"
    if prefix == "bj":
        return f"{digits}.BJ"
    return raw.upper()


def strip_markup(value: str) -> str:
    text = re.sub(r"<[^>]+>", "", value or "")
    return html.unescape(text).replace("\u3000", " ").strip()


def parse_ext(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        parsed = json.loads(str(raw))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def stock_symbols_from_ext(ext: dict[str, Any]) -> set[str]:
    symbols: set[str] = set()
    for item in ext.get("stocks") or []:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol") or "").strip()
        normalized = sina_symbol_to_ifind(symbol)
        if normalized:
            symbols.add(normalized)
    return symbols


def holding_terms(holding: dict[str, Any]) -> list[str]:
    terms = [
        str(holding.get("symbol") or ""),
        str(holding.get("symbol") or "").split(".")[0],
        str(holding.get("name") or ""),
        str(holding.get("full_name") or ""),
    ]
    terms.extend(str(item) for item in holding.get("aliases") or [])
    return [term.strip() for term in terms if term and term.strip()]


def match_holdings(text: str, ext_symbols: set[str], holdings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    matched: list[dict[str, Any]] = []
    seen: set[str] = set()
    for holding in holdings:
        symbol = str(holding.get("symbol") or "").upper()
        terms = holding_terms(holding)
        if symbol in ext_symbols or any(term and term in text for term in terms):
            if symbol and symbol not in seen:
                matched.append(holding)
                seen.add(symbol)
    return matched


def fetch_sina_feed(tag: str, size: int) -> list[dict[str, Any]]:
    query = urllib.parse.urlencode({"page": 1, "size": size, "tag": tag})
    response = http_get(
        f"{SINA_API_URL}?{query}",
        headers={
            "Accept": "application/json,text/plain,*/*",
            "Referer": SINA_REFERER,
            "User-Agent": "surveil-sina-flash/0.1",
        },
        timeout=15,
    )
    body = response.content.decode("utf-8", errors="replace")
    parsed = json.loads(body)
    status = (((parsed.get("result") or {}).get("status") or {}) if isinstance(parsed, dict) else {})
    if str(status.get("code", "0")) not in {"0", ""}:
        raise RuntimeError(f"新浪快讯接口返回异常：{body[:500]}")
    feed = (((parsed.get("result") or {}).get("data") or {}).get("feed") or {})
    rows = feed.get("list") or []
    return [row for row in rows if isinstance(row, dict)]


def fetch_sina_zy_feed(size: int) -> list[dict[str, Any]]:
    payload = client_from_env(news_provider()).news_flash_list(page=1, num=size)
    data = result_data(payload)
    if isinstance(data, dict):
        rows = data.get("items") or data.get("list") or data.get("data") or []
    elif isinstance(data, list):
        rows = data
    else:
        rows = []
    return [row for row in rows if isinstance(row, dict)]


def format_published_at(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, (int, float)) or (isinstance(value, str) and re.fullmatch(r"\d{10,13}", value.strip())):
        return timestamp_to_utc_iso(value)
    return parse_datetime_to_utc_iso(value)


def slim_raw(row: dict[str, Any], ext: dict[str, Any]) -> dict[str, Any]:
    multimedia = row.get("multimedia") if isinstance(row.get("multimedia"), dict) else {}
    return {
        "id": row.get("id"),
        "docid": row.get("docid"),
        "zhibo_id": row.get("zhibo_id"),
        "type": row.get("type"),
        "rich_text": row.get("rich_text"),
        "content": row.get("content"),
        "create_time": row.get("create_time"),
        "cTime": row.get("cTime"),
        "update_time": row.get("update_time"),
        "tag": row.get("tag"),
        "multimedia": {
            "img_url": multimedia.get("img_url") if isinstance(multimedia.get("img_url"), list) else [],
        },
        "ext": ext,
    }


def event_from_row(row: dict[str, Any], holdings: list[dict[str, Any]]) -> dict[str, Any] | None:
    text = strip_markup(str(row.get("rich_text") or row.get("content") or row.get("title") or ""))
    if not text:
        return None
    ext = parse_ext(row.get("ext"))
    ext_symbols = stock_symbols_from_ext(ext)
    matched = match_holdings(text, ext_symbols, holdings)
    macro_match = macro_policy_match({"title": text, "summary": text, "full_text": text})
    if not matched and not macro_match.get("matched"):
        return None
    symbols = [str(holding.get("symbol") or "").upper() for holding in matched if holding.get("symbol")]
    title = text[:80]
    source_id = str(row.get("id") or row.get("docid") or content_hash(text)[:16])
    published_at = format_published_at(
        row.get("create_time") or row.get("cTime") or row.get("ctime") or row.get("update_time") or ""
    )
    return {
        "source": SOURCE,
        "source_event_id": source_id,
        "event_type": "flash_news",
        "title": title,
        "summary": text,
        "full_text": text,
        "url": SINA_REFERER,
        "published_at": published_at,
        "symbols": symbols,
        "themes": ["新浪财经快讯", "宏观流动性/美联储政策"] if macro_match.get("matched") else ["新浪财经快讯"],
        "raw": {**slim_raw(row, ext), "macro_policy_line": macro_match if macro_match.get("matched") else {}},
        "content_hash": content_hash(SOURCE, source_id, text, published_at),
    }


def load_state() -> dict[str, Any]:
    init_db(DEFAULT_DB_PATH).close()
    with connect_sqlite(DEFAULT_DB_PATH) as conn:
        row = conn.execute("SELECT state_json FROM source_state WHERE source = ?", (STATE_KEY,)).fetchone()
    if not row:
        return {}
    try:
        parsed = json.loads(row[0] or "{}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def save_state(state: dict[str, Any]) -> None:
    with connect_sqlite(DEFAULT_DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO source_state (source, state_json, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(source) DO UPDATE SET state_json = excluded.state_json, updated_at = excluded.updated_at
            """,
            (STATE_KEY, json.dumps(state, ensure_ascii=False, sort_keys=True), utc_now()),
        )
        conn.commit()


def tags_from_env() -> list[str]:
    raw = os.getenv("SINA_FLASH_TAGS", os.getenv("SINA_FLASH_TAG", "10")).strip()
    tags = [tag.strip() for tag in raw.split(",") if tag.strip()]
    return tags or ["10"]


def is_verbose() -> bool:
    return os.getenv("SINA_FLASH_VERBOSE", "").strip() == "1"


def run_once(*, dry_run: bool = False, limit: int | None = None) -> int:
    init_db(DEFAULT_DB_PATH).close()
    import_holdings(DEFAULT_CONFIG_PATH, DEFAULT_DB_PATH)
    holdings = load_enabled_holdings(DEFAULT_DB_PATH)
    if not holdings:
        print("没有启用的持仓，新浪快讯仍会处理宏观政策线事件。")

    state = load_state()
    notify_baseline = os.getenv("SURVEIL_NOTIFY_BASELINE", "").strip() == "1"
    baseline_only = not state.get("initialized") and not notify_baseline
    verbose = is_verbose()
    size = env_int("SINA_FLASH_PAGE_SIZE", 20, minimum=1)
    events: dict[str, dict[str, Any]] = {}
    provider = news_provider()
    provider_rows: list[tuple[str, list[dict[str, Any]]]] = []
    if provider in {"zy_api", "api", "openapi", "official_api", "zy_mcp", "mcp"}:
        source_key = f"provider:{provider}"
        try:
            provider_rows.append((provider, fetch_sina_zy_feed(size=size)))
            with connect_sqlite(DEFAULT_DB_PATH) as conn:
                record_source_success(conn, "sina_flash", source_key)
        except Exception as exc:
            with connect_sqlite(DEFAULT_DB_PATH) as conn:
                record_source_failure(conn, "sina_flash", source_key, exc)
            raise
    else:
        for tag in tags_from_env():
            source_key = f"tag:{tag}"
            try:
                provider_rows.append((tag, fetch_sina_feed(tag=tag, size=size)))
                with connect_sqlite(DEFAULT_DB_PATH) as conn:
                    record_source_success(conn, "sina_flash", source_key)
            except Exception as exc:
                with connect_sqlite(DEFAULT_DB_PATH) as conn:
                    record_source_failure(conn, "sina_flash", source_key, exc)
                print(f"Sina flash fetch failed {source_key}: {exc}", file=sys.stderr, flush=True)
                continue
    for tag, rows in provider_rows:
        for row in rows:
            event = event_from_row(row, holdings)
            if event:
                event["raw"]["provider"] = provider
                event["raw"]["tag"] = tag
                events[event["source_event_id"]] = event

    processed = 0
    new_count = 0
    for event in reversed(list(events.values())):
        if limit is not None and processed >= limit:
            break
        processed += 1
        if baseline_only:
            event["baseline_only"] = True
        if dry_run:
            print(f"[dry-run] {event['source_event_id']} {event['title']} symbols={event.get('symbols')}")
            continue
        event_id, inserted = upsert_event(event, DEFAULT_DB_PATH)
        if not inserted:
            if verbose:
                print(f"seen event #{event_id}: {event['title']}", flush=True)
            continue
        new_count += 1
        if baseline_only:
            print(f"baseline event #{event_id}: {event['title']}", flush=True)
            continue
        print(f"new event #{event_id}: {event['title']}", flush=True)
        analysis = analyze_event(event_id, task="sina_flash_portfolio", db_path=DEFAULT_DB_PATH)
        print(f"analysis #{event_id}: {analysis.get('core_content', '')}", flush=True)
        status = maybe_deliver_event(event_id, analysis, db_path=DEFAULT_DB_PATH)
        print(f"delivery #{event_id}: {status}", flush=True)

    if not dry_run:
        save_state(
            {
                "initialized": True,
                "last_run_at": utc_now(),
                "last_event_ids": list(events.keys())[:100],
                "tags": tags_from_env(),
            }
        )
    if dry_run or verbose or new_count or baseline_only:
        print(
            f"Sina flash finished: matched={len(events)}, processed={processed}, "
            f"new={new_count}, baseline={baseline_only}",
            flush=True,
        )
    return new_count


def run_loop(interval: int) -> int:
    print(
        f"Sina flash monitor started: interval={interval}s provider={news_provider()} "
        f"tags={','.join(tags_from_env())}",
        flush=True,
    )
    while True:
        try:
            run_once()
        except Exception as exc:  # noqa: BLE001 - keep long-running monitor alive
            print(f"Sina flash monitor error: {exc}", file=sys.stderr, flush=True)
        time.sleep(interval)


def main() -> int:
    parser = argparse.ArgumentParser(description="新浪财经 7x24 持仓快讯监控")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--interval", type=int, default=0)
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()

    load_env(ROOT / ".env")
    if args.once or args.dry_run:
        run_once(dry_run=args.dry_run, limit=args.limit)
        return 0
    interval = args.interval or env_int("SINA_FLASH_POLL_SECONDS", 15, minimum=5)
    return run_loop(interval)


if __name__ == "__main__":
    raise SystemExit(main())
