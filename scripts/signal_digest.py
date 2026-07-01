#!/usr/bin/env python3
"""Send daily/weekly signal outcome and review digest to Feishu."""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from cards import div_markdown, md_escape
from env_utils import load_env
from feishu import send_card
from market_db import DEFAULT_DB_PATH, init_db
from pipeline_health import record_pipeline_failure, record_pipeline_success


ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / ".env"
BJ = ZoneInfo("Asia/Shanghai")


def day_window(day: str) -> tuple[str, str, str]:
    if day:
        start_local = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=BJ)
    else:
        start_local = datetime.now(BJ).replace(hour=0, minute=0, second=0, microsecond=0)
    end_local = start_local + timedelta(days=1)
    return (
        start_local.astimezone(timezone.utc).isoformat(),
        end_local.astimezone(timezone.utc).isoformat(),
        start_local.strftime("%Y-%m-%d"),
    )


def window_start(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=max(1, days))).isoformat()


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1", (table,)).fetchone()
    return row is not None


def fetch_new_signals(conn: sqlite3.Connection, start_utc: str, end_utc: str) -> list[sqlite3.Row]:
    if not table_exists(conn, "signals"):
        return []
    return list(
        conn.execute(
            """
            SELECT s.id, s.source, s.title, s.url, s.importance, s.direction,
                   s.incremental_classification, s.thesis, s.created_at,
                   COUNT(t.id) AS target_count
            FROM signals s
            LEFT JOIN signal_targets t ON t.signal_id = s.id
            WHERE s.created_at >= ? AND s.created_at < ?
            GROUP BY s.id
            ORDER BY CASE s.importance WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END,
                     s.created_at DESC
            LIMIT 12
            """,
            (start_utc, end_utc),
        ).fetchall()
    )


def fetch_reviews(conn: sqlite3.Connection, start_utc: str, end_utc: str) -> list[sqlite3.Row]:
    if not table_exists(conn, "signal_reviews"):
        return []
    rows = list(
        conn.execute(
            """
            WITH latest_review AS (
                SELECT signal_id, COALESCE(symbol, '') AS symbol, MAX(id) AS review_id
                FROM signal_reviews
                WHERE created_at >= ? AND created_at < ?
                GROUP BY signal_id, COALESCE(symbol, '')
            )
            SELECT r.signal_id, r.review_type, r.verdict, r.error_type, r.review_text, r.lessons_json, r.created_at,
                   s.source, s.title, s.url, s.importance
            FROM latest_review lr
            JOIN signal_reviews r ON r.id = lr.review_id
            JOIN signals s ON s.id = r.signal_id
            ORDER BY
              CASE r.review_type WHEN 'manual' THEN 0 ELSE 1 END,
              CASE r.verdict WHEN 'miss' THEN 0 WHEN 'partial' THEN 1 WHEN 'hit' THEN 2 ELSE 3 END,
              r.created_at DESC
            LIMIT 20
            """,
            (start_utc, end_utc),
        ).fetchall()
    )
    return rows


def fetch_source_scores(conn: sqlite3.Connection, window_days: int = 30) -> list[sqlite3.Row]:
    if not table_exists(conn, "source_scores"):
        return []
    return list(
        conn.execute(
            """
            SELECT source, window_days, signal_count, hit_rate, false_positive_rate,
                   avg_excess_return, stale_news_rate, updated_at
            FROM source_scores
            WHERE window_days = ?
            ORDER BY signal_count DESC, hit_rate DESC
            LIMIT 10
            """,
            (window_days,),
        ).fetchall()
    )


def row_url_title(row: sqlite3.Row) -> str:
    title = md_escape(str(row["title"] or ""))
    url = str(row["url"] or "")
    if url:
        return f"[{title}]({url})"
    return title


def percent(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value * 100:.0f}%"


def parse_lessons(value: str) -> dict:
    try:
        parsed = json.loads(value or "{}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def build_card(
    *,
    new_signals: list[sqlite3.Row],
    reviews: list[sqlite3.Row],
    scores: list[sqlite3.Row],
    display_day: str,
    mode: str,
) -> dict:
    verdict_counts: dict[str, int] = {}
    for row in reviews:
        verdict = str(row["verdict"] or "unknown")
        verdict_counts[verdict] = verdict_counts.get(verdict, 0) + 1
    title = "信号复盘周报" if mode == "weekly" else "信号复盘日报"
    elements = [
        div_markdown(f"**日期**：{md_escape(display_day)}"),
        div_markdown(
            "**总览**："
            f"新增信号 {len(new_signals)} 条；"
            f"新增复盘 {len(reviews)} 条；"
            f"命中 {verdict_counts.get('hit', 0)} / "
            f"部分 {verdict_counts.get('partial', 0)} / "
            f"未兑现 {verdict_counts.get('miss', 0)} / "
            f"过早或无法验证 {verdict_counts.get('too_early', 0) + verdict_counts.get('unverifiable', 0)}。"
        ),
        {"tag": "hr"},
    ]
    elements.append(div_markdown("**新增高/中重要性信号**"))
    if not new_signals:
        elements.append(div_markdown("暂无新增信号。"))
    for row in new_signals[:8]:
        elements.append(
            div_markdown(
                "\n".join(
                    [
                        f"- {row_url_title(row)}",
                        f"  来源：{md_escape(str(row['source'] or ''))}；重要性：{md_escape(str(row['importance'] or ''))}；方向：{md_escape(str(row['direction'] or ''))}",
                        f"  标的数：{row['target_count']}；判断：{md_escape(str(row['incremental_classification'] or ''))}",
                    ]
                )
            )
        )
    elements.append({"tag": "hr"})
    elements.append(div_markdown("**到期/新增复盘**"))
    if not reviews:
        elements.append(div_markdown("暂无新增复盘。"))
    for row in reviews[:12]:
        lessons = parse_lessons(str(row["lessons_json"] or "{}"))
        lesson_text = "；".join(str(item) for item in lessons.get("lessons", [])[:2]) if isinstance(lessons, dict) else ""
        elements.append(
            div_markdown(
                "\n".join(
                    [
                        f"- {row_url_title(row)}",
                        f"  结论：{md_escape(str(row['verdict'] or ''))}；错误类型：{md_escape(str(row['error_type'] or ''))}；复盘类型：{md_escape(str(row['review_type'] or ''))}",
                        f"  {md_escape(str(row['review_text'] or ''))}",
                        f"  经验：{md_escape(lesson_text or '-')}",
                    ]
                )
            )
        )
    elements.append({"tag": "hr"})
    elements.append(div_markdown("**来源评分（近 30 日）**"))
    if not scores:
        elements.append(div_markdown("暂无来源评分样本。"))
    for row in scores[:8]:
        elements.append(
            div_markdown(
                f"- {md_escape(str(row['source'] or 'unknown'))}：样本 {row['signal_count']}，"
                f"命中率 {percent(row['hit_rate'])}，未兑现率 {percent(row['false_positive_rate'])}，"
                f"平均方向收益 {row['avg_excess_return'] if row['avg_excess_return'] is not None else '-'}"
            )
        )
    elements.append(div_markdown("说明：本报告用于个人研究复盘，不构成自动交易指令；门控阈值反馈阶段暂未启用。"))
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "purple" if mode == "weekly" else "blue",
            "title": {"tag": "plain_text", "content": title},
        },
        "elements": elements,
    }


def main() -> int:
    load_env(ENV_PATH)
    parser = argparse.ArgumentParser(description="Send signal outcome/review digest.")
    parser.add_argument("--date", default="", help="Beijing date YYYY-MM-DD. Default: today.")
    parser.add_argument("--mode", choices=["daily", "weekly"], default="daily")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    db_path = Path(args.db)
    try:
        start_utc, end_utc, display_day = day_window(args.date)
        if args.mode == "weekly":
            start_utc = window_start(7)
        conn = init_db(db_path)
        conn.row_factory = sqlite3.Row
        with conn:
            new_signals = fetch_new_signals(conn, start_utc, end_utc)
            reviews = fetch_reviews(conn, start_utc, end_utc)
            scores = fetch_source_scores(conn, 30)
        card = build_card(new_signals=new_signals, reviews=reviews, scores=scores, display_day=display_day, mode=args.mode)
        if args.dry_run:
            print(json.dumps(card, ensure_ascii=False, indent=2))
            return 0
        send_card(card)
        print(f"已发送{args.mode}信号复盘报告：signals={len(new_signals)} reviews={len(reviews)}", flush=True)
        record_pipeline_success(f"signal_digest_{args.mode}", db_path=db_path)
        return 0
    except Exception as exc:  # noqa: BLE001
        if not args.dry_run:
            record_pipeline_failure(f"signal_digest_{args.mode}", exc, db_path=db_path)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
