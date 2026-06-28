#!/usr/bin/env python3
"""Daily digest for official core-company news that was not pushed instantly."""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from cards import div_markdown, md_escape, source_module
from env_utils import load_env
from feishu import send_card
from official_news_gate import ensure_official_news_table
from rss_monitor import DB_PATH


ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / ".env"
BJ = ZoneInfo("Asia/Shanghai")


def day_window(day: str) -> tuple[str, str]:
    if day:
        start_local = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=BJ)
    else:
        start_local = datetime.now(BJ).replace(hour=0, minute=0, second=0, microsecond=0)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(timezone.utc).isoformat(), end_local.astimezone(timezone.utc).isoformat()


def fetch_digest_rows(conn: sqlite3.Connection, day: str) -> list[sqlite3.Row]:
    start_utc, end_utc = day_window(day)
    conn.row_factory = sqlite3.Row
    return list(
        conn.execute(
            """
            SELECT source, item_id, url, title, published_at, importance, reason,
                   daily_summary, analysis_json, created_at
            FROM official_news_reviews
            WHERE created_at >= ? AND created_at < ?
              AND COALESCE(pushed_at, '') = ''
            ORDER BY
              CASE importance WHEN 'medium' THEN 0 WHEN 'low' THEN 1 ELSE 2 END,
              published_at DESC,
              created_at DESC
            """,
            (start_utc, end_utc),
        )
    )


def analysis_field(row: sqlite3.Row, key: str) -> str:
    try:
        parsed = json.loads(row["analysis_json"] or "{}")
    except json.JSONDecodeError:
        return ""
    value = parsed.get(key)
    if isinstance(value, str):
        return value.strip()
    if key == "incremental_view" and isinstance(value, dict):
        return "；".join(str(value.get(part) or "").strip() for part in ("classification", "surprise_level", "priced_in") if value.get(part))
    return ""


def build_digest_card(rows: list[sqlite3.Row], day: str) -> dict:
    display_day = day or datetime.now(BJ).strftime("%Y-%m-%d")
    elements = [
        div_markdown(f"**日期**：{md_escape(display_day)}"),
        div_markdown(f"**范围**：OpenAI / NVIDIA / Samsung Semiconductor / SK hynix / Micron 官网新闻中未即时推送的条目"),
        div_markdown(f"**条数**：{len(rows)}"),
        {"tag": "hr"},
    ]
    if not rows:
        elements.append(div_markdown("今日暂无需要汇总的官网新闻。"))
    for index, row in enumerate(rows[:30], start=1):
        module = source_module(row["source"], row["url"] or "")
        incremental = analysis_field(row, "incremental_view")
        core = analysis_field(row, "core_content")
        parts = [
            f"**{index}. {md_escape(row['title'])}**",
            f"来源：{md_escape(module)}",
            f"重要性：{md_escape(row['importance'])}",
        ]
        if row["daily_summary"]:
            parts.append(f"摘要：{md_escape(row['daily_summary'])}")
        elif core:
            parts.append(f"摘要：{md_escape(core)}")
        if incremental:
            parts.append(f"增量判断：{md_escape(incremental)}")
        if row["reason"]:
            parts.append(f"分流理由：{md_escape(row['reason'])}")
        if row["url"]:
            parts.append(f"[打开原文]({row['url']})")
        elements.append(div_markdown("\n".join(parts)))
    if len(rows) > 30:
        elements.append(div_markdown(f"其余 {len(rows) - 30} 条已省略，可在 SQLite official_news_reviews 表查看。"))
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": "核心公司官网新闻日报"},
        },
        "elements": elements,
    }


def main() -> int:
    load_env(ENV_PATH)
    parser = argparse.ArgumentParser(description="发送核心公司官网新闻日报")
    parser.add_argument("--date", default="", help="北京时间日期 YYYY-MM-DD，默认今天")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    with sqlite3.connect(DB_PATH) as conn:
        ensure_official_news_table(conn)
        rows = fetch_digest_rows(conn, args.date)
    card = build_digest_card(rows, args.date)
    if args.dry_run:
        print(json.dumps(card, ensure_ascii=False, indent=2))
        return 0
    send_card(card)
    print(f"已发送核心公司官网新闻日报：{len(rows)} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
