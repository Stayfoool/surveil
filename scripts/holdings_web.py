#!/usr/bin/env python3
"""Local-only web UI for portfolio holdings management."""

from __future__ import annotations

import argparse
import html
import json
import os
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

from db_utils import connect_sqlite
from env_utils import load_env
from holdings_store import (
    HoldingsError,
    holdings_diff,
    normalized_holdings,
    normalize_holdings_for_save,
    save_holdings,
    validate_holdings,
)
from market_db import DEFAULT_DB_PATH
from media_keyword_config import media_keyword_payload, save_media_keyword_config
from settings_store import save_settings, settings_payload
from signals_extract import extract_signals
from stock_relations import (
    DEFAULT_CONFIG_PATH as STOCK_RELATIONS_CONFIG_PATH,
    accept_relation_suggestion,
    delete_relation,
    diff_relations,
    export_relations,
    import_relations,
    list_relation_suggestions,
    list_relations,
    reject_relation_suggestion,
    save_relation,
    set_relation_enabled,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787
BJ = ZoneInfo("Asia/Shanghai")

SERVICE_UNITS = [
    "surveil-x-stream.service",
    "surveil-rss-monitor.service",
    "surveil-trendforce-page-monitor.service",
    "surveil-sina-flash.service",
    "surveil-holdings-web.service",
    "surveil-proxy.service",
]

TIMER_UNITS = [
    "surveil-sina-stock-news.timer",
    "surveil-overseas-media.timer",
    "surveil-article-daily.timer",
    "surveil-signal-review.timer",
    "surveil-signal-digest.timer",
    "surveil-ifind-notice.timer",
    "surveil-ifind-report.timer",
    "surveil-jygs-actions.timer",
]

LOG_FILES = [
    "x-stream.err.log",
    "rss-monitor.err.log",
    "trendforce-page-monitor.err.log",
    "overseas-media.err.log",
    "sina-flash.err.log",
    "sina-stock-news.err.log",
    "ifind-notice.err.log",
    "jygs-actions.err.log",
    "holdings-web.err.log",
    "signal-review.err.log",
    "signal-digest.err.log",
    "stock-relations-import.err.log",
]

SIGNAL_FEEDBACK_VERDICTS = {"hit", "partial", "miss", "too_early", "unverifiable"}
SIGNAL_FEEDBACK_ERROR_TYPES = {
    "stale_or_price_in",
    "counter_supply_news",
    "supply_expansion_bearish",
    "wrong_relation",
    "wrong_direction",
    "timing_error",
    "low_market_attention",
    "quote_unavailable",
    "window_not_ready",
    "direction_uncertain",
    "weak_follow_through",
    "direction_or_relevance_error",
    "timing_or_duration_error",
    "none",
    "unverifiable",
    "other",
}


def env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    return raw.lower() in {"1", "true", "yes", "y", "on", "是"}


def utc_window_for_day(day: str = "") -> tuple[str, str, str]:
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


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1", (table,)).fetchone()
    return row is not None


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def normalize_time(value: str) -> str:
    return str(value or "")


def count_rows(conn: sqlite3.Connection, table: str, where: str = "", params: tuple[Any, ...] = ()) -> int:
    if not table_exists(conn, table):
        return 0
    query = f"SELECT COUNT(*) FROM {table}"
    if where:
        query += f" WHERE {where}"
    return int(conn.execute(query, params).fetchone()[0])


def grouped_counts(conn: sqlite3.Connection, table: str, field: str, where: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
    if not table_exists(conn, table):
        return []
    rows = conn.execute(
        f"""
        SELECT COALESCE({field}, '') AS key, COUNT(*) AS count
        FROM {table}
        WHERE {where}
        GROUP BY COALESCE({field}, '')
        ORDER BY count DESC, key
        """,
        params,
    ).fetchall()
    return [{"key": row["key"] or "unknown", "count": int(row["count"])} for row in rows]


def fetch_events_rows(day: str = "", source: str = "", kind: str = "", q: str = "", limit: int = 100) -> list[dict[str, Any]]:
    start_utc, end_utc, _ = utc_window_for_day(day)
    q_lower = q.strip().lower()
    source_lower = source.strip().lower()
    kind_lower = kind.strip().lower()
    rows: list[dict[str, Any]] = []
    with connect_sqlite(DEFAULT_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        if table_exists(conn, "events"):
            for row in conn.execute(
                """
                SELECT e.id, e.source, e.event_type, e.title, e.summary, e.url, e.published_at,
                       e.first_seen_at, e.baseline_only,
                       (
                         SELECT importance FROM event_analyses a
                         WHERE a.event_id = e.id
                         ORDER BY a.id DESC LIMIT 1
                       ) AS importance,
                       (
                         SELECT classification FROM event_analyses a
                         WHERE a.event_id = e.id
                         ORDER BY a.id DESC LIMIT 1
                       ) AS classification,
                       (
                         SELECT should_push FROM event_analyses a
                         WHERE a.event_id = e.id
                         ORDER BY a.id DESC LIMIT 1
                       ) AS should_push,
                       (
                         SELECT status FROM deliveries d
                         WHERE d.event_id = e.id
                         ORDER BY d.id DESC LIMIT 1
                       ) AS delivery_status
                FROM events e
                WHERE e.first_seen_at >= ? AND e.first_seen_at < ?
                ORDER BY e.first_seen_at DESC
                LIMIT 300
                """,
                (start_utc, end_utc),
            ):
                rows.append(
                    {
                        "kind": row["event_type"] or "event",
                        "source": row["source"],
                        "id": row["id"],
                        "title": row["title"],
                        "summary": row["summary"] or "",
                        "url": row["url"] or "",
                        "published_at": normalize_time(row["published_at"]),
                        "seen_at": normalize_time(row["first_seen_at"]),
                        "importance": row["importance"] or "",
                        "classification": row["classification"] or "",
                        "push": bool(row["should_push"]),
                        "delivery_status": row["delivery_status"] or "",
                        "baseline_only": bool(row["baseline_only"]),
                    }
                )
        if table_exists(conn, "article_reviews"):
            for row in conn.execute(
                """
                SELECT source, item_id, url, title, source_module, published_at, importance,
                       push_now, incremental_classification, daily_summary, reason, pushed_at, created_at
                FROM article_reviews
                WHERE created_at >= ? AND created_at < ?
                ORDER BY created_at DESC
                LIMIT 300
                """,
                (start_utc, end_utc),
            ):
                rows.append(
                    {
                        "kind": "article",
                        "source": row["source_module"] or row["source"],
                        "id": row["item_id"],
                        "title": row["title"],
                        "summary": row["daily_summary"] or row["reason"] or "",
                        "url": row["url"] or "",
                        "published_at": normalize_time(row["published_at"]),
                        "seen_at": normalize_time(row["created_at"]),
                        "importance": row["importance"] or "",
                        "classification": row["incremental_classification"] or "",
                        "push": bool(row["push_now"]),
                        "delivery_status": "sent" if row["pushed_at"] else "daily",
                        "baseline_only": False,
                    }
                )
        if table_exists(conn, "official_news_reviews"):
            for row in conn.execute(
                """
                SELECT source, item_id, url, title, published_at, importance, daily_summary,
                       reason, pushed_at, created_at
                FROM official_news_reviews
                WHERE created_at >= ? AND created_at < ?
                ORDER BY created_at DESC
                LIMIT 200
                """,
                (start_utc, end_utc),
            ):
                rows.append(
                    {
                        "kind": "official_news",
                        "source": row["source"],
                        "id": row["item_id"],
                        "title": row["title"],
                        "summary": row["daily_summary"] or row["reason"] or "",
                        "url": row["url"] or "",
                        "published_at": normalize_time(row["published_at"]),
                        "seen_at": normalize_time(row["created_at"]),
                        "importance": row["importance"] or "",
                        "classification": "",
                        "push": bool(row["pushed_at"]),
                        "delivery_status": "sent" if row["pushed_at"] else "daily",
                        "baseline_only": False,
                    }
                )
        if table_exists(conn, "seen_posts"):
            seen_columns = table_columns(conn, "seen_posts")
            delivery_expr = "delivery_status" if "delivery_status" in seen_columns else "'sent'"
            for row in conn.execute(
                f"""
                SELECT source, post_id, url, text, published_at, first_seen_at,
                       {delivery_expr} AS delivery_status
                FROM seen_posts
                WHERE first_seen_at >= ? AND first_seen_at < ?
                ORDER BY first_seen_at DESC
                LIMIT 100
                """,
                (start_utc, end_utc),
            ):
                text = row["text"] or ""
                rows.append(
                    {
                        "kind": "x_post",
                        "source": row["source"],
                        "id": row["post_id"],
                        "title": text.splitlines()[0][:120] if text else f"X post {row['post_id']}",
                        "summary": text[:300],
                        "url": row["url"] or "",
                        "published_at": normalize_time(row["published_at"]),
                        "seen_at": normalize_time(row["first_seen_at"]),
                        "importance": "",
                        "classification": "",
                        "push": row["delivery_status"] == "sent",
                        "delivery_status": row["delivery_status"] or "",
                        "baseline_only": False,
                    }
                )
        if table_exists(conn, "jygs_events"):
            for row in conn.execute(
                """
                SELECT id, trade_date, run_slot, symbol, name, themes, reason, url, first_seen_at
                FROM jygs_events
                WHERE (first_seen_at >= ? AND first_seen_at < ?)
                   OR trade_date = ?
                ORDER BY first_seen_at DESC
                LIMIT 100
                """,
                (start_utc, end_utc, utc_window_for_day(day)[2]),
            ):
                rows.append(
                    {
                        "kind": "jygs",
                        "source": f"jygs/{row['run_slot']}",
                        "id": row["id"],
                        "title": f"{row['name']} {row['symbol'] or ''}".strip(),
                        "summary": row["reason"] or row["themes"] or "",
                        "url": row["url"] or "",
                        "published_at": row["trade_date"] or "",
                        "seen_at": normalize_time(row["first_seen_at"]),
                        "importance": "",
                        "classification": "",
                        "push": False,
                        "delivery_status": "",
                        "baseline_only": False,
                    }
                )

    def matches(item: dict[str, Any]) -> bool:
        if source_lower and source_lower not in str(item["source"]).lower():
            return False
        if kind_lower and kind_lower != str(item["kind"]).lower():
            return False
        if q_lower:
            hay = json.dumps(item, ensure_ascii=False).lower()
            if q_lower not in hay:
                return False
        return True

    rows = [item for item in rows if matches(item)]
    rows.sort(key=lambda item: str(item.get("seen_at") or item.get("published_at") or ""), reverse=True)
    return rows[: max(1, min(limit, 300))]


def fetch_signal_rows(
    *,
    q: str = "",
    source: str = "",
    symbol: str = "",
    verdict: str = "",
    importance: str = "",
    limit: int = 100,
) -> list[dict[str, Any]]:
    q_lower = q.strip().lower()
    source_lower = source.strip().lower()
    symbol_upper = symbol.strip().upper()
    verdict_lower = verdict.strip().lower()
    importance_lower = importance.strip().lower()
    rows: list[dict[str, Any]] = []
    with connect_sqlite(DEFAULT_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        if not table_exists(conn, "signals"):
            return []
        for row in conn.execute(
            """
            WITH latest_outcome AS (
                SELECT signal_id, symbol, MAX(as_of_date) AS as_of_date
                FROM signal_outcomes
                GROUP BY signal_id, symbol
            ), latest_review AS (
                SELECT signal_id, COALESCE(symbol, '') AS symbol, MAX(id) AS review_id
                FROM signal_reviews
                GROUP BY signal_id, COALESCE(symbol, '')
            )
            SELECT s.id, s.source, s.title, s.url, s.published_at, s.created_at,
                   s.importance, s.incremental_classification, s.direction,
                   s.confidence, s.thesis,
                   t.id AS target_id, t.symbol, t.name, t.target_role, t.relation_type, t.relation_reason,
                   t.expected_direction, t.confidence AS target_confidence,
                   o.as_of_date, o.return_1d, o.return_3d, o.return_5d, o.return_10d,
                   o.return_20d, o.max_drawdown, o.max_runup, o.volume_change,
                   o.outcome_status,
                   r.review_type, r.verdict, r.error_type, r.review_text, r.lessons_json, r.created_at AS reviewed_at
            FROM signals s
            LEFT JOIN signal_targets t ON t.signal_id = s.id
            LEFT JOIN latest_outcome lo ON lo.signal_id = s.id AND lo.symbol = t.symbol
            LEFT JOIN signal_outcomes o
              ON o.signal_id = lo.signal_id AND o.symbol = lo.symbol AND o.as_of_date = lo.as_of_date
            LEFT JOIN latest_review lr ON lr.signal_id = s.id AND lr.symbol = COALESCE(t.symbol, '')
            LEFT JOIN signal_reviews r ON r.id = lr.review_id
            ORDER BY s.created_at DESC, s.id DESC
            LIMIT 600
            """
        ):
            item = {
                "id": row["id"],
                "target_id": row["target_id"],
                "source": row["source"] or "",
                "title": row["title"] or "",
                "url": row["url"] or "",
                "published_at": normalize_time(row["published_at"]),
                "created_at": normalize_time(row["created_at"]),
                "importance": row["importance"] or "",
                "incremental_classification": row["incremental_classification"] or "",
                "direction": row["direction"] or "",
                "confidence": row["confidence"] or "",
                "thesis": row["thesis"] or "",
                "symbol": row["symbol"] or "",
                "name": row["name"] or "",
                "target_role": row["target_role"] or "",
                "relation_type": row["relation_type"] or "",
                "relation_reason": row["relation_reason"] or "",
                "expected_direction": row["expected_direction"] or "",
                "target_confidence": row["target_confidence"] or "",
                "as_of_date": row["as_of_date"] or "",
                "returns": {
                    "1d": row["return_1d"],
                    "3d": row["return_3d"],
                    "5d": row["return_5d"],
                    "10d": row["return_10d"],
                    "20d": row["return_20d"],
                },
                "max_drawdown": row["max_drawdown"],
                "max_runup": row["max_runup"],
                "volume_change": row["volume_change"],
                "outcome_status": row["outcome_status"] or "",
                "review_type": row["review_type"] or "",
                "verdict": row["verdict"] or "",
                "error_type": row["error_type"] or "",
                "review_text": row["review_text"] or "",
                "lessons_json": row["lessons_json"] or "",
                "reviewed_at": normalize_time(row["reviewed_at"]),
            }
            hay = json.dumps(item, ensure_ascii=False).lower()
            if q_lower and q_lower not in hay:
                continue
            if source_lower and source_lower not in str(item["source"]).lower():
                continue
            if symbol_upper and symbol_upper not in str(item["symbol"]).upper() and symbol_upper not in str(item["name"]).upper():
                continue
            if verdict_lower and verdict_lower != str(item["verdict"]).lower():
                continue
            if importance_lower and importance_lower != str(item["importance"]).lower():
                continue
            rows.append(item)
            if len(rows) >= max(1, min(limit, 300)):
                break
    return rows


def save_signal_feedback(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        signal_id = int(payload.get("signal_id") or 0)
    except (TypeError, ValueError):
        signal_id = 0
    if signal_id <= 0:
        raise HoldingsError("请求缺少有效 signal_id")

    target_id_raw = payload.get("target_id")
    target_id: int | None = None
    if target_id_raw not in (None, ""):
        try:
            target_id = int(target_id_raw)
        except (TypeError, ValueError):
            target_id = None

    verdict = str(payload.get("verdict") or "miss").strip().lower()
    if verdict not in SIGNAL_FEEDBACK_VERDICTS:
        raise HoldingsError("复盘结论无效")

    error_type = str(payload.get("error_type") or "other").strip().lower()
    if error_type not in SIGNAL_FEEDBACK_ERROR_TYPES:
        error_type = "other"

    symbol = str(payload.get("symbol") or "").strip().upper()
    review_text = str(payload.get("review_text") or "").strip()
    if not review_text:
        raise HoldingsError("请填写反馈原因")
    if len(review_text) > 3000:
        raise HoldingsError("反馈原因过长")

    lessons_raw = payload.get("lessons")
    if isinstance(lessons_raw, list):
        lessons = [str(item).strip() for item in lessons_raw if str(item).strip()]
    else:
        lessons = [
            item.strip()
            for item in str(lessons_raw or "").replace("；", "\n").replace(";", "\n").splitlines()
            if item.strip()
        ]
    if not lessons:
        lessons = [review_text]
    lessons = lessons[:8]

    tags_raw = payload.get("tags")
    tags = [str(item).strip() for item in tags_raw if str(item).strip()] if isinstance(tags_raw, list) else []
    now = datetime.now(timezone.utc).isoformat()
    lessons_json = {
        "manual": True,
        "symbol": symbol,
        "target_id": target_id,
        "lessons": lessons,
        "feedback_tags": tags,
        "user_feedback": review_text,
        "created_from": "holdings_web",
    }
    with connect_sqlite(DEFAULT_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        existing = conn.execute("SELECT id FROM signals WHERE id = ?", (signal_id,)).fetchone()
        if not existing:
            raise HoldingsError("signal_id 不存在")
        if target_id is None and symbol:
            target_row = conn.execute(
                """
                SELECT id FROM signal_targets
                WHERE signal_id = ? AND UPPER(COALESCE(symbol, '')) = ?
                ORDER BY id DESC LIMIT 1
                """,
                (signal_id, symbol),
            ).fetchone()
            if target_row:
                target_id = int(target_row["id"])
                lessons_json["target_id"] = target_id
        cur = conn.execute(
            """
            INSERT INTO signal_reviews (
                signal_id, target_id, symbol, review_type, verdict, error_type, review_text,
                lessons_json, model, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                signal_id,
                target_id,
                symbol,
                "manual",
                verdict,
                error_type,
                review_text,
                json.dumps(lessons_json, ensure_ascii=False, sort_keys=True),
                "human",
                now,
            ),
        )
        conn.commit()
        return {"id": int(cur.lastrowid), "created_at": now}


def fetch_signal_summary() -> dict[str, Any]:
    with connect_sqlite(DEFAULT_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cards = [
            {"label": "信号", "value": count_rows(conn, "signals")},
            {"label": "影响标的", "value": count_rows(conn, "signal_targets")},
            {"label": "行情结果", "value": count_rows(conn, "signal_outcomes")},
            {"label": "复盘记录", "value": count_rows(conn, "signal_reviews")},
            {"label": "关系映射", "value": count_rows(conn, "stock_relations", "enabled = 1")},
        ]
        verdicts = grouped_counts(conn, "signal_reviews", "verdict", "1=1", ())
        source_scores: list[dict[str, Any]] = []
        if table_exists(conn, "source_scores"):
            for row in conn.execute(
                """
                SELECT source, window_days, signal_count, hit_rate, false_positive_rate,
                       avg_excess_return, updated_at
                FROM source_scores
                WHERE window_days = 30
                ORDER BY signal_count DESC, hit_rate DESC
                LIMIT 12
                """
            ):
                source_scores.append(
                    {
                        "source": row["source"] or "",
                        "window_days": row["window_days"],
                        "signal_count": row["signal_count"],
                        "hit_rate": row["hit_rate"],
                        "false_positive_rate": row["false_positive_rate"],
                        "avg_excess_return": row["avg_excess_return"],
                        "updated_at": row["updated_at"] or "",
                    }
                )
    return {"cards": cards, "verdicts": verdicts, "source_scores": source_scores}


def fetch_relation_rows(q: str = "", limit: int = 100, enabled: str = "all") -> list[dict[str, Any]]:
    return list_relations(db_path=DEFAULT_DB_PATH, q=q, enabled=enabled, limit=limit)


def relation_snapshot_payload() -> dict[str, Any]:
    exported = export_relations(db_path=DEFAULT_DB_PATH, config_path=STOCK_RELATIONS_CONFIG_PATH)
    return {"snapshot": exported}


def run_relation_backfill(days: int) -> dict[str, Any]:
    safe_days = max(1, min(int(days or 7), 60))
    counts = extract_signals(db_path=DEFAULT_DB_PATH, days=safe_days, dry_run=False)
    return {"days": safe_days, "counts": counts}


def overview_payload(day: str = "") -> dict[str, Any]:
    start_utc, end_utc, display_day = utc_window_for_day(day)
    with connect_sqlite(DEFAULT_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        deliveries_failed = count_rows(conn, "deliveries", "sent_at >= ? AND sent_at < ? AND status = 'failed'", (start_utc, end_utc))
        article_failures = 0
        if table_exists(conn, "article_reviews"):
            article_failures = int(
                conn.execute(
                    """
                    SELECT COUNT(*) FROM article_reviews
                    WHERE created_at >= ? AND created_at < ?
                      AND (reason LIKE '%失败%' OR gate_json LIKE '%error%')
                    """,
                    (start_utc, end_utc),
                ).fetchone()[0]
            )
        cards = [
            {"label": "统一事件", "value": count_rows(conn, "events", "first_seen_at >= ? AND first_seen_at < ?", (start_utc, end_utc))},
            {"label": "文章门控", "value": count_rows(conn, "article_reviews", "created_at >= ? AND created_at < ?", (start_utc, end_utc))},
            {"label": "X 新帖", "value": count_rows(conn, "seen_posts", "first_seen_at >= ? AND first_seen_at < ?", (start_utc, end_utc))},
            {"label": "韭研异动", "value": count_rows(conn, "jygs_events", "first_seen_at >= ? AND first_seen_at < ?", (start_utc, end_utc))},
            {"label": "飞书失败", "value": deliveries_failed + article_failures},
        ]
        by_source = grouped_counts(conn, "events", "source", "first_seen_at >= ? AND first_seen_at < ?", (start_utc, end_utc))
        article_importance = grouped_counts(conn, "article_reviews", "importance", "created_at >= ? AND created_at < ?", (start_utc, end_utc))
        deliveries = grouped_counts(conn, "deliveries", "status", "sent_at >= ? AND sent_at < ?", (start_utc, end_utc))
    return {
        "ok": True,
        "date": display_day,
        "cards": cards,
        "by_source": by_source[:12],
        "article_importance": article_importance,
        "deliveries": deliveries,
        "latest": fetch_events_rows(day=day, limit=10),
    }


def systemctl_show(unit: str) -> dict[str, str]:
    try:
        result = subprocess.run(
            ["systemctl", "show", unit, "--no-pager"],
            check=False,
            text=True,
            capture_output=True,
            timeout=8,
        )
    except Exception as exc:  # noqa: BLE001
        return {"Id": unit, "error": str(exc)}
    values: dict[str, str] = {"Id": unit}
    for line in result.stdout.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key in {
            "ActiveState",
            "SubState",
            "Result",
            "ExecMainStatus",
            "ExecMainPID",
            "NRestarts",
            "ExecMainStartTimestamp",
            "NextElapseUSecRealtime",
            "LastTriggerUSec",
            "LoadState",
        }:
            values[key] = value
    if result.returncode != 0:
        values["error"] = result.stderr.strip() or result.stdout.strip()
    return values


def tail_file(path: Path, max_lines: int = 8) -> str:
    if not path.exists():
        return ""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception as exc:  # noqa: BLE001
        return f"读取失败：{exc}"
    return "\n".join(lines[-max_lines:])


def health_payload() -> dict[str, Any]:
    units = [systemctl_show(unit) for unit in [*SERVICE_UNITS, *TIMER_UNITS]]
    sources: list[dict[str, Any]] = []
    with connect_sqlite(DEFAULT_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        if table_exists(conn, "source_health"):
            for row in conn.execute(
                """
                SELECT monitor, source, consecutive_failures, last_success_at, last_failure_at,
                       last_error, last_alerted_at, updated_at
                FROM source_health
                ORDER BY consecutive_failures DESC, updated_at DESC
                LIMIT 200
                """
            ):
                sources.append(
                    {
                        "monitor": row["monitor"],
                        "source": row["source"],
                        "status": "failing" if int(row["consecutive_failures"] or 0) else "ok",
                        "consecutive_failures": int(row["consecutive_failures"] or 0),
                        "last_success_at": row["last_success_at"] or "",
                        "last_failure_at": row["last_failure_at"] or "",
                        "last_error": row["last_error"] or "",
                        "last_alerted_at": row["last_alerted_at"] or "",
                        "updated_at": row["updated_at"] or "",
                    }
                )
        if table_exists(conn, "x_stream_health"):
            for row in conn.execute(
                """
                SELECT issue_key, status, failure_count, first_failed_at, last_failed_at,
                       last_error, last_alerted_at, last_recovered_at
                FROM x_stream_health
                ORDER BY CASE WHEN status = 'failing' THEN 0 ELSE 1 END, failure_count DESC, last_failed_at DESC
                LIMIT 80
                """
            ):
                sources.append(
                    {
                        "monitor": "x_stream_detail",
                        "source": row["issue_key"],
                        "status": row["status"] or "",
                        "consecutive_failures": int(row["failure_count"] or 0),
                        "last_success_at": row["last_recovered_at"] or "",
                        "last_failure_at": row["last_failed_at"] or "",
                        "last_error": row["last_error"] or "",
                        "last_alerted_at": row["last_alerted_at"] or "",
                        "updated_at": row["last_failed_at"] or row["last_recovered_at"] or "",
                    }
                )
    logs_dir = ROOT / "logs"
    logs = []
    for name in LOG_FILES:
        tail = tail_file(logs_dir / name)
        if tail:
            logs.append({"name": name, "tail": tail})
    return {"ok": True, "units": units, "sources": sources, "logs": logs}


def html_page(token_required: bool) -> str:
    token_hint = "需要访问令牌" if token_required else "未配置访问令牌，仅限 SSH 隧道使用"
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Surveil 工作台</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f6f7f9;
      --panel: #ffffff;
      --text: #1f2933;
      --muted: #6b7280;
      --line: #d8dde6;
      --accent: #176b87;
      --danger: #b42318;
      --ok: #0f766e;
    }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: var(--bg); color: var(--text); }}
    header {{ height: 56px; display: flex; align-items: center; gap: 16px; padding: 0 20px; background: #102a43; color: white; }}
    header h1 {{ font-size: 18px; margin: 0; font-weight: 650; }}
    nav.tabs {{ display: flex; gap: 8px; padding: 10px 20px 0; background: var(--bg); }}
    nav.tabs button {{ background: transparent; border-color: transparent; border-radius: 6px 6px 0 0; }}
    nav.tabs button.active {{ background: white; border-color: var(--line); border-bottom-color: white; color: var(--accent); }}
    main {{ padding: 18px 20px 32px; }}
    .view {{ display: none; }}
    .view.active {{ display: block; }}
    .toolbar {{ display: flex; flex-wrap: wrap; align-items: center; gap: 10px; margin-bottom: 14px; }}
    .panel {{ background: var(--panel); border: 1px solid var(--line); border-radius: 8px; overflow: hidden; }}
    .section-title {{ display: flex; align-items: center; justify-content: space-between; gap: 10px; margin: 0 0 12px; }}
    .section-title h2 {{ margin: 0; font-size: 16px; }}
    .metric-grid {{ display: grid; grid-template-columns: repeat(5, minmax(120px, 1fr)); gap: 10px; margin-bottom: 14px; }}
    .metric {{ background: white; border: 1px solid var(--line); border-radius: 8px; padding: 12px; }}
    .metric .label {{ color: var(--muted); font-size: 12px; }}
    .metric .value {{ font-size: 24px; font-weight: 700; margin-top: 6px; }}
    .split {{ display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 12px; }}
    .list {{ padding: 10px 12px; }}
    .list-row {{ border-bottom: 1px solid var(--line); padding: 9px 0; font-size: 13px; }}
    .list-row:last-child {{ border-bottom: 0; }}
    .badge {{ display: inline-block; border: 1px solid var(--line); border-radius: 999px; padding: 1px 7px; font-size: 12px; color: var(--muted); background: #fbfcfd; }}
    .badge.high {{ color: #9f1239; border-color: #fecdd3; background: #fff1f2; }}
    .badge.medium {{ color: #92400e; border-color: #fed7aa; background: #fff7ed; }}
    .badge.low {{ color: #166534; border-color: #bbf7d0; background: #f0fdf4; }}
    .log {{ white-space: pre-wrap; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; background: #0f172a; color: #dbeafe; padding: 10px; border-radius: 6px; overflow: auto; }}
    .summary {{ color: var(--muted); font-size: 13px; margin-left: auto; }}
    button {{ border: 1px solid var(--line); background: white; color: var(--text); height: 34px; padding: 0 12px; border-radius: 6px; cursor: pointer; font-weight: 550; }}
    button.primary {{ background: var(--accent); color: white; border-color: var(--accent); }}
    button.danger {{ color: var(--danger); border-color: #f1b7b0; }}
    button:disabled {{ opacity: .5; cursor: not-allowed; }}
    input, textarea, select {{ width: 100%; border: 1px solid var(--line); border-radius: 6px; padding: 8px; font: inherit; background: white; }}
    input[type="checkbox"] {{ width: auto; }}
    table {{ width: 100%; border-collapse: collapse; table-layout: fixed; }}
    th, td {{ border-bottom: 1px solid var(--line); padding: 8px; vertical-align: top; font-size: 13px; }}
    th {{ text-align: left; background: #eef2f6; color: #334e68; position: sticky; top: 0; z-index: 1; }}
    td.symbol {{ width: 112px; }}
    td.enabled {{ width: 70px; text-align: center; }}
    td.actions {{ width: 82px; text-align: center; }}
    td.name {{ width: 110px; }}
    td.full {{ width: 170px; }}
    td textarea {{ min-height: 38px; resize: vertical; }}
    .events-table td.summary-cell {{ color: var(--muted); }}
    .events-table a {{ color: var(--accent); text-decoration: none; }}
    .table-wrap {{ max-height: calc(100vh - 190px); overflow: auto; }}
    .status {{ white-space: pre-wrap; font-size: 13px; padding: 10px 12px; border: 1px solid var(--line); border-radius: 8px; background: white; margin-bottom: 12px; display: none; }}
    .status.ok {{ display: block; border-color: #99d6cc; color: var(--ok); }}
    .status.err {{ display: block; border-color: #f1b7b0; color: var(--danger); }}
    .modal-backdrop {{ position: fixed; inset: 0; background: rgba(15, 23, 42, .35); display: none; align-items: center; justify-content: center; padding: 20px; }}
    .modal {{ width: min(760px, 100%); background: white; border-radius: 8px; border: 1px solid var(--line); box-shadow: 0 20px 60px rgba(15, 23, 42, .25); }}
    .modal h2 {{ font-size: 16px; margin: 0; padding: 14px 16px; border-bottom: 1px solid var(--line); }}
    .modal .body {{ padding: 16px; }}
    .modal .foot {{ padding: 12px 16px; border-top: 1px solid var(--line); display: flex; justify-content: flex-end; gap: 10px; }}
    .grid {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }}
    .field label {{ display: block; font-size: 12px; color: var(--muted); margin-bottom: 4px; }}
    .settings-grid {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }}
    .settings-card {{ background: white; border: 1px solid var(--line); border-radius: 8px; padding: 12px; }}
    .settings-card h3 {{ margin: 0 0 8px; font-size: 15px; }}
    .setting-field {{ margin-top: 9px; }}
    .setting-field label {{ display: flex; align-items: center; justify-content: space-between; gap: 8px; font-size: 12px; color: var(--muted); margin-bottom: 4px; }}
    .setting-field input {{ height: 34px; }}
    .setting-mask {{ font-size: 12px; color: var(--muted); white-space: nowrap; }}
    .diff {{ max-height: 360px; overflow: auto; border: 1px solid var(--line); border-radius: 6px; padding: 10px; background: #fbfcfd; font-size: 13px; white-space: pre-wrap; }}
    .hint {{ color: var(--muted); font-size: 12px; margin-top: 6px; }}
    @media (max-width: 1000px) {{
      .metric-grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      .split {{ grid-template-columns: 1fr; }}
      .settings-grid {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body>
  <header>
    <h1>Surveil 工作台</h1>
    <span class="hint">{html.escape(token_hint)}</span>
  </header>
  <nav class="tabs">
    <button id="tab-overview" onclick="showView('overview')">今日总览</button>
    <button id="tab-events" onclick="showView('events')">事件中心</button>
    <button id="tab-signals" onclick="showView('signals')">信号复盘</button>
    <button id="tab-relations" onclick="showView('relations')">关系映射</button>
    <button id="tab-health" onclick="showView('health')">任务健康</button>
    <button id="tab-keywords" onclick="showView('keywords')">媒体关键词</button>
    <button id="tab-settings" onclick="showView('settings')">配置中心</button>
    <button id="tab-holdings" onclick="showView('holdings')">持仓管理</button>
  </nav>
  <main>
    <div id="status" class="status"></div>
    <section id="view-overview" class="view">
      <div class="section-title">
        <h2>今日总览</h2>
        <button onclick="loadOverview()">刷新</button>
      </div>
      <div id="overviewMetrics" class="metric-grid"></div>
      <div class="split">
        <section class="panel">
          <div class="list" id="overviewBreakdown"></div>
        </section>
        <section class="panel">
          <div class="list" id="overviewLatest"></div>
        </section>
      </div>
    </section>

    <section id="view-events" class="view">
      <div class="toolbar">
        <input id="eventDate" type="date" style="width:160px">
        <input id="eventSource" placeholder="来源过滤" style="width:180px">
        <input id="eventQuery" placeholder="搜索标题、摘要、标的" style="width:260px">
        <button class="primary" onclick="loadEvents()">查询</button>
      </div>
      <section class="panel">
        <div class="table-wrap">
          <table class="events-table">
            <thead>
              <tr>
                <th style="width:130px">时间</th>
                <th style="width:130px">来源</th>
                <th style="width:90px">类型</th>
                <th>标题/摘要</th>
                <th style="width:110px">重要性</th>
                <th style="width:130px">状态</th>
              </tr>
            </thead>
            <tbody id="eventRows"></tbody>
          </table>
        </div>
      </section>
    </section>

    <section id="view-signals" class="view">
      <div class="section-title">
        <h2>信号复盘</h2>
        <button onclick="loadSignals()">刷新</button>
      </div>
      <div id="signalMetrics" class="metric-grid"></div>
      <div class="toolbar">
        <input id="signalSource" placeholder="来源" style="width:160px">
        <input id="signalSymbol" placeholder="代码/名称" style="width:160px">
        <select id="signalVerdict" style="width:150px">
          <option value="">全部结论</option>
          <option value="hit">hit</option>
          <option value="partial">partial</option>
          <option value="miss">miss</option>
          <option value="too_early">too_early</option>
          <option value="unverifiable">unverifiable</option>
        </select>
        <select id="signalImportance" style="width:150px">
          <option value="">全部重要性</option>
          <option value="high">high</option>
          <option value="medium">medium</option>
          <option value="low">low</option>
        </select>
        <input id="signalQuery" placeholder="搜索标题、原因、复盘" style="width:260px">
        <button class="primary" onclick="loadSignals()">查询</button>
      </div>
      <section class="panel">
        <div class="table-wrap">
          <table class="events-table">
            <thead>
              <tr>
                <th style="width:90px">结论</th>
                <th style="width:130px">标的</th>
                <th style="width:130px">收益</th>
                <th>信号/复盘</th>
                <th style="width:120px">来源</th>
                <th style="width:120px">关系</th>
                <th style="width:86px">反馈</th>
              </tr>
            </thead>
            <tbody id="signalRows"></tbody>
          </table>
        </div>
      </section>
      <div class="split" style="margin-top:12px">
        <section class="panel">
          <div class="list" id="signalSourceScores"></div>
        </section>
        <section class="panel">
          <div class="list-row" style="padding:10px 12px"><strong>产业链/关联关系</strong></div>
          <div class="toolbar" style="padding:0 12px 10px">
            <input id="relationQuery" placeholder="搜索关系、主题、股票" style="width:260px">
            <button onclick="loadRelations()">查询</button>
          </div>
          <div class="table-wrap" style="max-height:360px">
            <table>
              <thead>
                <tr>
                  <th style="width:120px">触发</th>
                  <th style="width:120px">映射</th>
                  <th style="width:120px">方向</th>
                  <th>原因</th>
                </tr>
              </thead>
              <tbody id="relationRows"></tbody>
            </table>
          </div>
        </section>
      </div>
    </section>

    <section id="view-relations" class="view">
      <div class="section-title">
        <h2>关系映射</h2>
        <div>
          <button onclick="loadRelationManager()">刷新</button>
          <button class="primary" onclick="openRelationModal()">新增关系</button>
        </div>
      </div>
      <div class="toolbar">
        <input id="relationManageQuery" placeholder="搜索起点、终点、主题、原因" style="width:260px">
        <select id="relationManageEnabled" style="width:130px">
          <option value="all">全部</option>
          <option value="enabled">启用</option>
          <option value="disabled">停用</option>
        </select>
        <button class="primary" onclick="loadRelationManager()">查询</button>
        <button onclick="exportRelationJson()">导出 JSON</button>
        <button onclick="importRelationJson()">从 JSON 导入</button>
        <button onclick="diffRelationJson()">检测差异</button>
        <input id="relationBackfillDays" type="number" min="1" max="60" value="7" style="width:86px">
        <button onclick="backfillRelations()">回填最近 N 天</button>
      </div>
      <section class="panel">
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th style="width:80px">状态</th>
                <th style="width:150px">触发</th>
                <th style="width:150px">映射</th>
                <th style="width:130px">方向/强度</th>
                <th>关系与原因</th>
                <th style="width:130px">复盘</th>
                <th style="width:150px">操作</th>
              </tr>
            </thead>
            <tbody id="relationManageRows"></tbody>
          </table>
        </div>
      </section>
      <section class="panel" style="margin-top:12px">
        <div class="list-row" style="padding:10px 12px"><strong>候选关系</strong><span class="summary">大模型或人工沉淀的候选，确认后才正式生效</span></div>
        <div class="toolbar" style="padding:0 12px 10px">
          <select id="relationSuggestionStatus" style="width:140px">
            <option value="pending">待确认</option>
            <option value="accepted">已确认</option>
            <option value="rejected">已拒绝</option>
            <option value="all">全部</option>
          </select>
          <button onclick="loadRelationSuggestions()">刷新候选</button>
        </div>
        <div class="table-wrap" style="max-height:360px">
          <table>
            <thead>
              <tr>
                <th style="width:90px">状态</th>
                <th style="width:150px">触发</th>
                <th style="width:150px">映射</th>
                <th>理由</th>
                <th style="width:130px">操作</th>
              </tr>
            </thead>
            <tbody id="relationSuggestionRows"></tbody>
          </table>
        </div>
      </section>
    </section>

    <section id="view-health" class="view">
      <div class="section-title">
        <h2>任务健康</h2>
        <button onclick="loadHealth()">刷新</button>
      </div>
      <section class="panel">
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Unit</th>
                <th style="width:120px">Active</th>
                <th style="width:120px">Sub</th>
                <th style="width:120px">Result</th>
                <th style="width:110px">Restarts</th>
                <th style="width:220px">最近启动/触发</th>
              </tr>
            </thead>
            <tbody id="healthRows"></tbody>
          </table>
        </div>
      </section>
      <section class="panel" style="margin-top:12px">
        <div class="list-row" style="padding:10px 12px"><strong>来源健康</strong></div>
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th style="width:160px">模块</th>
                <th style="width:210px">来源</th>
                <th style="width:90px">状态</th>
                <th style="width:90px">失败</th>
                <th style="width:180px">最近成功</th>
                <th style="width:180px">最近失败</th>
                <th>错误</th>
              </tr>
            </thead>
            <tbody id="sourceHealthRows"></tbody>
          </table>
        </div>
      </section>
      <div id="healthLogs" style="margin-top:12px"></div>
    </section>

    <section id="view-keywords" class="view">
      <div class="section-title">
        <h2>媒体关键词</h2>
        <div>
          <button onclick="loadKeywords()">刷新</button>
          <button onclick="resetBaseKeywords()">恢复代码默认词</button>
          <button class="primary" onclick="saveKeywords()">保存</button>
        </div>
      </div>
      <div class="split">
        <section class="panel">
          <div class="list">
            <div class="list-row"><strong>基础关键词</strong></div>
            <div class="list-row">
              <textarea id="baseKeywords" style="min-height:260px" placeholder="每行一个基础关键词"></textarea>
              <div class="hint">实际粗筛使用“基础关键词 + 额外包含关键词 - 排除关键词”。基础关键词可编辑；留空保存时会回到代码默认词。</div>
            </div>
            <div class="list-row"><strong>额外包含关键词</strong></div>
            <div class="list-row">
              <textarea id="includeKeywords" style="min-height:220px" placeholder="每行一个关键词，例如：金刚石散热"></textarea>
              <div class="hint">这些词会叠加到基础关键词上；RSS、DIGITIMES、The Elec、日经 xTECH 下一轮轮询会自动生效。</div>
            </div>
            <div class="list-row"><strong>排除关键词</strong></div>
            <div class="list-row">
              <textarea id="excludeKeywords" style="min-height:120px" placeholder="每行一个排除词"></textarea>
              <div class="hint">命中排除词的条目会在进入 LLM 门控前被过滤。</div>
            </div>
          </div>
        </section>
        <section class="panel">
          <div class="list">
            <div class="list-row"><strong>代码默认关键词</strong></div>
            <div class="hint">这是代码内置默认词，用于恢复基线；当前是否覆盖：<span id="baseOverrideStatus">-</span></div>
            <div id="defaultKeywords" class="list-row" style="max-height:560px; overflow:auto"></div>
          </div>
        </section>
      </div>
    </section>

    <section id="view-settings" class="view">
      <div class="section-title">
        <h2>配置中心</h2>
        <div>
          <button onclick="loadSettings()">刷新</button>
          <button class="primary" onclick="saveSettings()">保存</button>
        </div>
      </div>
      <div class="status ok" style="display:block">
敏感配置不会回显明文。敏感输入框留空表示保留现有值；输入新值才会覆盖服务器 .env。
保存后如需立即生效，请按页面提示重启对应服务。
      </div>
      <div id="settingsGrid" class="settings-grid"></div>
    </section>

    <section id="view-holdings" class="view">
      <div class="toolbar">
        <button class="primary" onclick="addRow()">新增</button>
        <button onclick="openBatch()">批量导入</button>
        <button onclick="reloadData()">刷新</button>
        <button class="primary" onclick="previewSave()">保存</button>
        <input id="filter" placeholder="搜索代码、名称、关键词" style="width:260px" oninput="renderTable()">
        <span id="summary" class="summary"></span>
      </div>
      <section class="panel">
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th style="width:70px">启用</th>
                <th style="width:118px">代码</th>
                <th style="width:120px">简称</th>
                <th style="width:190px">全称</th>
                <th>别名</th>
                <th>业务简介</th>
                <th>新闻关键词</th>
                <th>排除关键词</th>
                <th style="width:86px">操作</th>
              </tr>
            </thead>
            <tbody id="rows"></tbody>
          </table>
        </div>
      </section>
    </section>
  </main>

  <div id="batchModal" class="modal-backdrop">
    <div class="modal">
      <h2>批量导入</h2>
      <div class="body">
        <textarea id="batchText" style="min-height:240px" placeholder="每行一只股票，例如：&#10;源杰科技,688498.SH&#10;中际旭创,300308.SZ&#10;长飞光纤"></textarea>
        <div class="hint">支持“名称,代码”或“代码,名称”；只填一个值时会自动判断是否像股票代码。</div>
      </div>
      <div class="foot">
        <button onclick="closeBatch()">取消</button>
        <button class="primary" onclick="applyBatch()">加入列表</button>
      </div>
    </div>
  </div>

  <div id="diffModal" class="modal-backdrop">
    <div class="modal">
      <h2>保存前确认</h2>
      <div class="body">
        <div id="diffText" class="diff"></div>
      </div>
      <div class="foot">
        <button onclick="closeDiff()">取消</button>
        <button class="primary" onclick="confirmSave()">确认保存</button>
      </div>
    </div>
  </div>

  <div id="relationModal" class="modal-backdrop">
    <div class="modal">
      <h2 id="relationModalTitle">编辑关系</h2>
      <div class="body">
        <div class="grid">
          <div class="field"><label>触发代码/主题</label><input id="relSymbol" placeholder="例如 NVDA、HBM、人造钻石散热"></div>
          <div class="field"><label>触发名称</label><input id="relSymbolName" placeholder="例如 NVIDIA、HBM、金刚石散热"></div>
          <div class="field"><label>映射股票代码</label><input id="relRelatedSymbol" placeholder="例如 300308.SZ"></div>
          <div class="field"><label>映射股票名称</label><input id="relRelatedName" placeholder="例如 中际旭创"></div>
          <div class="field"><label>关系类型</label><input id="relRelationType" placeholder="例如 AI optical interconnect supply chain"></div>
          <div class="field"><label>影响方向</label>
            <select id="relImpactDirection">
              <option value="positive">positive</option>
              <option value="negative">negative</option>
              <option value="neutral">neutral</option>
              <option value="uncertain">uncertain</option>
            </select>
          </div>
          <div class="field"><label>主题</label><input id="relTheme" placeholder="例如 光模块/CPO/AI 数据中心"></div>
          <div class="field"><label>置信度</label><input id="relConfidence" placeholder="高 / 中 / 低 或 0-100"></div>
          <div class="field"><label>强度</label><input id="relStrength" placeholder="1-5 或 高/中/低"></div>
          <div class="field"><label>来源</label><input id="relSource" placeholder="web / Serenity / 机构研报 / UP主蒸馏"></div>
          <div class="field"><label>生效日期</label><input id="relValidFrom" type="date"></div>
          <div class="field"><label>失效日期</label><input id="relValidTo" type="date"></div>
        </div>
        <div class="field" style="margin-top:12px"><label>映射原因 / 证据</label><textarea id="relReason" style="min-height:110px" placeholder="说明为什么这个事件会传导到该股票，最好写清一阶/二阶逻辑。"></textarea></div>
        <label style="display:flex; align-items:center; gap:8px; margin-top:10px"><input id="relEnabled" type="checkbox" checked> 启用</label>
      </div>
      <div class="foot">
        <button onclick="closeRelationModal()">取消</button>
        <button class="primary" onclick="saveRelationFromModal()">保存关系</button>
      </div>
    </div>
  </div>

  <div id="signalFeedbackModal" class="modal-backdrop">
    <div class="modal">
      <h2>修正复盘</h2>
      <div class="body">
        <div class="grid">
          <div class="field">
            <label>结论</label>
            <select id="signalFeedbackVerdict">
              <option value="miss">miss</option>
              <option value="partial">partial</option>
              <option value="hit">hit</option>
              <option value="too_early">too_early</option>
              <option value="unverifiable">unverifiable</option>
            </select>
          </div>
          <div class="field">
            <label>错误类型</label>
            <select id="signalFeedbackErrorType">
              <option value="stale_or_price_in">旧闻/已定价</option>
              <option value="counter_supply_news">后续反向消息</option>
              <option value="supply_expansion_bearish">供给扩张利空</option>
              <option value="wrong_relation">关联错误</option>
              <option value="wrong_direction">方向错误</option>
              <option value="timing_error">时点错误</option>
              <option value="low_market_attention">关注度不足</option>
              <option value="quote_unavailable">行情缺失</option>
              <option value="window_not_ready">窗口未到</option>
              <option value="direction_uncertain">方向不明</option>
              <option value="weak_follow_through">持续性不足</option>
              <option value="direction_or_relevance_error">方向或相关性错误</option>
              <option value="timing_or_duration_error">时点或持有期错误</option>
              <option value="none">无错误</option>
              <option value="unverifiable">无法验证</option>
              <option value="other">其他</option>
            </select>
          </div>
        </div>
        <div class="field" style="margin-top:12px">
          <label>反馈原因</label>
          <textarea id="signalFeedbackText" rows="5"></textarea>
        </div>
        <div class="field" style="margin-top:12px">
          <label>经验</label>
          <textarea id="signalFeedbackLessons" rows="4"></textarea>
        </div>
        <div id="signalFeedbackMeta" class="hint"></div>
      </div>
      <div class="foot">
        <button onclick="closeSignalFeedback()">取消</button>
        <button class="primary" onclick="saveSignalFeedback()">保存</button>
      </div>
    </div>
  </div>

<script>
let token = localStorage.getItem('surveil_holdings_token') || '';
let holdings = [];
let pendingPayload = null;
let loadedHoldings = false;
let codeDefaultKeywords = [];
let managedRelations = [];
let editingRelationId = null;
let signalRowsCache = [];
let editingSignalFeedback = null;

function headers() {{
  const h = {{'Content-Type': 'application/json'}};
  if (token) h['X-Holdings-Token'] = token;
  return h;
}}

async function api(path, options={{}}) {{
  const res = await fetch(path, {{...options, headers: {{...headers(), ...(options.headers || {{}})}}}});
  if (res.status === 401) {{
    token = prompt('请输入 HOLDINGS_WEB_TOKEN') || '';
    localStorage.setItem('surveil_holdings_token', token);
    return api(path, options);
  }}
  const data = await res.json();
  if (!res.ok || data.ok === false) throw new Error(data.error || res.statusText);
  return data;
}}

function showStatus(text, kind='ok') {{
  const el = document.getElementById('status');
  el.className = 'status ' + kind;
  el.textContent = text;
}}

function splitList(value) {{
  return String(value || '').split(/[，,;；\\n]+/).map(s => s.trim()).filter(Boolean);
}}

function joinList(value) {{
  return Array.isArray(value) ? value.join('，') : '';
}}

function escapeHtml(value) {{
  return String(value ?? '').replace(/[&<>"']/g, c => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]));
}}

function badge(value) {{
  const raw = String(value || '').trim();
  if (!raw) return '<span class="badge">-</span>';
  const lower = raw.toLowerCase();
  const cls = ['high', 'medium', 'low'].includes(lower) ? lower : '';
  return `<span class="badge ${{cls}}">${{escapeHtml(raw)}}</span>`;
}}

function shortText(value, limit=160) {{
  const text = String(value || '').replace(/\\s+/g, ' ').trim();
  if (text.length <= limit) return text;
  return text.slice(0, limit - 3) + '...';
}}

function formatTime(value) {{
  if (!value) return '-';
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return String(value).slice(0, 19);
  return d.toLocaleString('zh-CN', {{hour12: false}});
}}

function todayString() {{
  const d = new Date();
  const year = d.getFullYear();
  const month = String(d.getMonth() + 1).padStart(2, '0');
  const day = String(d.getDate()).padStart(2, '0');
  return `${{year}}-${{month}}-${{day}}`;
}}

function showView(name) {{
  document.querySelectorAll('.view').forEach(el => el.classList.remove('active'));
  document.querySelectorAll('nav.tabs button').forEach(el => el.classList.remove('active'));
  document.getElementById(`view-${{name}}`).classList.add('active');
  document.getElementById(`tab-${{name}}`).classList.add('active');
  if (name === 'overview') loadOverview();
  if (name === 'events') loadEvents();
  if (name === 'signals') loadSignals();
  if (name === 'relations') loadRelationManager();
  if (name === 'health') loadHealth();
  if (name === 'keywords') loadKeywords();
  if (name === 'settings') loadSettings();
  if (name === 'holdings' && !loadedHoldings) reloadData();
}}

function formatPct(value) {{
  if (value === null || value === undefined || value === '') return '-';
  const num = Number(value);
  if (Number.isNaN(num)) return String(value);
  return `${{num.toFixed(2)}}%`;
}}

function formatRate(value) {{
  if (value === null || value === undefined || value === '') return '-';
  const num = Number(value);
  if (Number.isNaN(num)) return String(value);
  return `${{(num * 100).toFixed(0)}}%`;
}}

async function loadOverview() {{
  try {{
    const data = await api('/api/overview');
    const metrics = document.getElementById('overviewMetrics');
    metrics.innerHTML = (data.cards || []).map(item => `
      <div class="metric">
        <div class="label">${{escapeHtml(item.label)}}</div>
        <div class="value">${{escapeHtml(item.value)}}</div>
      </div>
    `).join('');
    const breakdown = [];
    breakdown.push('<div class="list-row"><strong>来源分布</strong></div>');
    (data.by_source || []).forEach(item => breakdown.push(`<div class="list-row">${{escapeHtml(item.key)}} <span class="summary">${{item.count}}</span></div>`));
    breakdown.push('<div class="list-row"><strong>文章重要性</strong></div>');
    (data.article_importance || []).forEach(item => breakdown.push(`<div class="list-row">${{badge(item.key)}} <span class="summary">${{item.count}}</span></div>`));
    breakdown.push('<div class="list-row"><strong>飞书状态</strong></div>');
    (data.deliveries || []).forEach(item => breakdown.push(`<div class="list-row">${{escapeHtml(item.key)}} <span class="summary">${{item.count}}</span></div>`));
    document.getElementById('overviewBreakdown').innerHTML = breakdown.join('') || '<div class="list-row">暂无统计。</div>';
    document.getElementById('overviewLatest').innerHTML = ['<div class="list-row"><strong>最近事件</strong></div>', ...(data.latest || []).map(item => `
      <div class="list-row">
        <div>${{badge(item.importance)}} <strong>${{escapeHtml(shortText(item.title, 120))}}</strong></div>
        <div class="hint">${{escapeHtml(item.source)}} / ${{escapeHtml(item.kind)}} / ${{formatTime(item.seen_at)}}</div>
      </div>
    `)].join('');
  }} catch (err) {{
    showStatus(err.message, 'err');
  }}
}}

async function loadEvents() {{
  try {{
    const params = new URLSearchParams();
    const date = document.getElementById('eventDate').value;
    const source = document.getElementById('eventSource').value.trim();
    const q = document.getElementById('eventQuery').value.trim();
    if (date) params.set('date', date);
    if (source) params.set('source', source);
    if (q) params.set('q', q);
    const data = await api('/api/events?' + params.toString());
    const rows = document.getElementById('eventRows');
    rows.innerHTML = (data.events || []).map(item => `
      <tr>
        <td>${{formatTime(item.seen_at || item.published_at)}}</td>
        <td>${{escapeHtml(item.source || '')}}</td>
        <td>${{escapeHtml(item.kind || '')}}</td>
        <td class="summary-cell">
          <div><strong>${{item.url ? `<a href="${{escapeHtml(item.url)}}" target="_blank" rel="noreferrer">${{escapeHtml(item.title || '')}}</a>` : escapeHtml(item.title || '')}}</strong></div>
          <div>${{escapeHtml(shortText(item.summary || '', 220))}}</div>
        </td>
        <td>${{badge(item.importance)}}<div class="hint">${{escapeHtml(item.classification || '')}}</div></td>
        <td>${{escapeHtml(item.delivery_status || '')}}${{item.push ? '<div class="hint">push</div>' : ''}}</td>
      </tr>
    `).join('') || '<tr><td colspan="6">没有匹配事件。</td></tr>';
  }} catch (err) {{
    showStatus(err.message, 'err');
  }}
}}

async function loadSignals() {{
  try {{
    const params = new URLSearchParams();
    const source = document.getElementById('signalSource').value.trim();
    const symbol = document.getElementById('signalSymbol').value.trim();
    const verdict = document.getElementById('signalVerdict').value.trim();
    const importance = document.getElementById('signalImportance').value.trim();
    const q = document.getElementById('signalQuery').value.trim();
    if (source) params.set('source', source);
    if (symbol) params.set('symbol', symbol);
    if (verdict) params.set('verdict', verdict);
    if (importance) params.set('importance', importance);
    if (q) params.set('q', q);
    const data = await api('/api/signals?' + params.toString());
    document.getElementById('signalMetrics').innerHTML = ((data.summary || {{}}).cards || []).map(item => `
      <div class="metric">
        <div class="label">${{escapeHtml(item.label)}}</div>
        <div class="value">${{escapeHtml(item.value)}}</div>
      </div>
    `).join('');
    signalRowsCache = data.signals || [];
    document.getElementById('signalRows').innerHTML = signalRowsCache.map((item, index) => {{
      const returns = item.returns || {{}};
      const returnText = [`1d ${{formatPct(returns['1d'])}}`, `3d ${{formatPct(returns['3d'])}}`, `5d ${{formatPct(returns['5d'])}}`].join('<br>');
      const title = item.url ? `<a href="${{escapeHtml(item.url)}}" target="_blank" rel="noreferrer">${{escapeHtml(item.title || '')}}</a>` : escapeHtml(item.title || '');
      return `
        <tr>
          <td>${{badge(item.verdict || item.outcome_status || '-')}}<div class="hint">${{escapeHtml(item.error_type || '')}}</div><div class="hint">${{escapeHtml(item.review_type || '')}}</div></td>
          <td><strong>${{escapeHtml(item.symbol || item.name || '-')}}</strong><div class="hint">${{escapeHtml(item.name || '')}}</div></td>
          <td>${{returnText}}<div class="hint">runup ${{formatPct(item.max_runup)}} / dd ${{formatPct(item.max_drawdown)}}</div></td>
          <td class="summary-cell">
            <div><strong>${{title}}</strong></div>
            <div>${{escapeHtml(shortText(item.thesis || '', 180))}}</div>
            <div class="hint">${{escapeHtml(shortText(item.review_text || '', 220))}}</div>
          </td>
          <td>${{escapeHtml(item.source || '')}}<div>${{badge(item.importance || '')}}</div><div class="hint">${{formatTime(item.created_at)}}</div></td>
          <td>${{escapeHtml(item.target_role || '')}}<div class="hint">${{escapeHtml(shortText(item.relation_type || item.relation_reason || '', 120))}}</div></td>
          <td><button onclick="openSignalFeedback(${{index}})">修正</button></td>
        </tr>
      `;
    }}).join('') || '<tr><td colspan="7">没有匹配信号。</td></tr>';
    const scores = ((data.summary || {{}}).source_scores || []);
    document.getElementById('signalSourceScores').innerHTML = ['<div class="list-row"><strong>来源评分（近 30 日）</strong></div>', ...scores.map(item => `
      <div class="list-row">
        <strong>${{escapeHtml(item.source || '')}}</strong>
        <span class="summary">样本 ${{item.signal_count}} / 命中 ${{formatRate(item.hit_rate)}} / 未兑现 ${{formatRate(item.false_positive_rate)}}</span>
        <div class="hint">平均方向收益：${{escapeHtml(item.avg_excess_return ?? '-')}}</div>
      </div>
    `)].join('');
    await loadRelations();
  }} catch (err) {{
    showStatus(err.message, 'err');
  }}
}}

function openSignalFeedback(index) {{
  const item = signalRowsCache[index];
  if (!item) return;
  editingSignalFeedback = item;
  document.getElementById('signalFeedbackVerdict').value = item.verdict || 'miss';
  document.getElementById('signalFeedbackErrorType').value = item.error_type || 'stale_or_price_in';
  document.getElementById('signalFeedbackText').value = item.review_text || '';
  let lessons = '';
  try {{
    const parsed = item.lessons_json ? JSON.parse(item.lessons_json) : {{}};
    if (Array.isArray(parsed.lessons)) lessons = parsed.lessons.join('\n');
  }} catch (err) {{}}
  document.getElementById('signalFeedbackLessons').value = lessons;
  document.getElementById('signalFeedbackMeta').textContent = `${{item.symbol || '-'}} / ${{item.title || ''}}`;
  document.getElementById('signalFeedbackModal').style.display = 'flex';
}}

function closeSignalFeedback() {{
  editingSignalFeedback = null;
  document.getElementById('signalFeedbackModal').style.display = 'none';
}}

async function saveSignalFeedback() {{
  if (!editingSignalFeedback) return;
  try {{
    const payload = {{
      signal_id: editingSignalFeedback.id,
      target_id: editingSignalFeedback.target_id || null,
      symbol: editingSignalFeedback.symbol || '',
      verdict: document.getElementById('signalFeedbackVerdict').value,
      error_type: document.getElementById('signalFeedbackErrorType').value,
      review_text: document.getElementById('signalFeedbackText').value.trim(),
      lessons: document.getElementById('signalFeedbackLessons').value.trim()
    }};
    await api('/api/signal-feedback', {{method: 'POST', body: JSON.stringify(payload)}});
    closeSignalFeedback();
    await loadSignals();
    showStatus('已保存人工复盘反馈。');
  }} catch (err) {{
    showStatus(err.message, 'err');
  }}
}}

async function loadRelations() {{
  try {{
    const params = new URLSearchParams();
    const q = document.getElementById('relationQuery') ? document.getElementById('relationQuery').value.trim() : '';
    if (q) params.set('q', q);
    const data = await api('/api/signal-relations?' + params.toString());
    document.getElementById('relationRows').innerHTML = (data.relations || []).map(item => `
      <tr>
        <td><strong>${{escapeHtml(item.symbol || '')}}</strong><div class="hint">${{escapeHtml(item.symbol_name || '')}}</div></td>
        <td><strong>${{escapeHtml(item.related_symbol || '')}}</strong><div class="hint">${{escapeHtml(item.related_name || '')}}</div></td>
        <td>${{badge(item.impact_direction || '')}}<div class="hint">${{escapeHtml(item.confidence || '')}}</div></td>
        <td class="summary-cell">
          <div>${{escapeHtml(item.relation_type || '')}} / ${{escapeHtml(item.theme || '')}}</div>
          <div class="hint">${{escapeHtml(shortText(item.reason || '', 180))}}</div>
        </td>
      </tr>
    `).join('') || '<tr><td colspan="4">暂无关系配置。可复制 config/stock_relations.example.json 为私有 config/stock_relations.json 后导入。</td></tr>';
  }} catch (err) {{
    showStatus(err.message, 'err');
  }}
}}

async function loadRelationManager() {{
  try {{
    const params = new URLSearchParams();
    const q = document.getElementById('relationManageQuery') ? document.getElementById('relationManageQuery').value.trim() : '';
    const enabled = document.getElementById('relationManageEnabled') ? document.getElementById('relationManageEnabled').value : 'all';
    if (q) params.set('q', q);
    if (enabled) params.set('enabled', enabled);
    const data = await api('/api/relations?' + params.toString());
    managedRelations = data.relations || [];
    document.getElementById('relationManageRows').innerHTML = managedRelations.map(item => `
      <tr>
        <td>${{badge(item.enabled ? '启用' : '停用')}}<div class="hint">${{formatTime(item.updated_at)}}</div></td>
        <td><strong>${{escapeHtml(item.symbol || '')}}</strong><div class="hint">${{escapeHtml(item.symbol_name || '')}}</div></td>
        <td><strong>${{escapeHtml(item.related_symbol || '')}}</strong><div class="hint">${{escapeHtml(item.related_name || '')}}</div></td>
        <td>${{badge(item.impact_direction || '')}}<div class="hint">强度 ${{escapeHtml(item.relation_strength || '-')}} / 置信 ${{escapeHtml(item.confidence || '-')}}</div></td>
        <td class="summary-cell">
          <div>${{escapeHtml(item.relation_type || '')}} / ${{escapeHtml(item.theme || '')}}</div>
          <div class="hint">${{escapeHtml(shortText(item.reason || '', 220))}}</div>
          <div class="hint">${{escapeHtml(item.source || '')}} ${{item.valid_to ? ' / 有效至 ' + escapeHtml(item.valid_to) : ''}}</div>
        </td>
        <td>${{escapeHtml(item.last_review_verdict || '-')}}<div class="hint">hit ${{item.hit_count || 0}} / miss ${{item.miss_count || 0}}</div></td>
        <td>
          <button onclick="editRelation(${{item.id}})">编辑</button>
          <button onclick="toggleRelation(${{item.id}}, ${{item.enabled ? 'false' : 'true'}})">${{item.enabled ? '停用' : '启用'}}</button>
          <button class="danger" onclick="deleteRelationRow(${{item.id}})">删除</button>
        </td>
      </tr>
    `).join('') || '<tr><td colspan="7">暂无关系映射。</td></tr>';
    await loadRelationSuggestions();
  }} catch (err) {{
    showStatus(err.message, 'err');
  }}
}}

function clearRelationForm() {{
  editingRelationId = null;
  document.getElementById('relationModalTitle').textContent = '新增关系';
  ['relSymbol','relSymbolName','relRelatedSymbol','relRelatedName','relRelationType','relTheme','relConfidence','relStrength','relSource','relValidFrom','relValidTo','relReason'].forEach(id => {{
    document.getElementById(id).value = '';
  }});
  document.getElementById('relImpactDirection').value = 'positive';
  document.getElementById('relEnabled').checked = true;
}}

function openRelationModal(item=null) {{
  clearRelationForm();
  if (item) {{
    editingRelationId = item.id;
    document.getElementById('relationModalTitle').textContent = '编辑关系';
    document.getElementById('relSymbol').value = item.symbol || '';
    document.getElementById('relSymbolName').value = item.symbol_name || '';
    document.getElementById('relRelatedSymbol').value = item.related_symbol || '';
    document.getElementById('relRelatedName').value = item.related_name || '';
    document.getElementById('relRelationType').value = item.relation_type || '';
    document.getElementById('relImpactDirection').value = item.impact_direction || 'uncertain';
    document.getElementById('relTheme').value = item.theme || '';
    document.getElementById('relConfidence').value = item.confidence || '';
    document.getElementById('relStrength').value = item.relation_strength || '';
    document.getElementById('relSource').value = item.source || 'web';
    document.getElementById('relValidFrom').value = item.valid_from || '';
    document.getElementById('relValidTo').value = item.valid_to || '';
    document.getElementById('relReason').value = item.reason || '';
    document.getElementById('relEnabled').checked = item.enabled !== false;
  }} else {{
    document.getElementById('relSource').value = 'web';
  }}
  document.getElementById('relationModal').style.display = 'flex';
}}

function closeRelationModal() {{
  document.getElementById('relationModal').style.display = 'none';
}}

function editRelation(id) {{
  const item = managedRelations.find(row => Number(row.id) === Number(id));
  if (!item) {{
    showStatus('没有找到这条关系。', 'err');
    return;
  }}
  openRelationModal(item);
}}

function relationFormPayload() {{
  return {{
    symbol: document.getElementById('relSymbol').value.trim(),
    symbol_name: document.getElementById('relSymbolName').value.trim(),
    related_symbol: document.getElementById('relRelatedSymbol').value.trim(),
    related_name: document.getElementById('relRelatedName').value.trim(),
    relation_type: document.getElementById('relRelationType').value.trim() || 'related',
    impact_direction: document.getElementById('relImpactDirection').value.trim(),
    theme: document.getElementById('relTheme').value.trim(),
    confidence: document.getElementById('relConfidence').value.trim(),
    relation_strength: document.getElementById('relStrength').value.trim(),
    source: document.getElementById('relSource').value.trim() || 'web',
    valid_from: document.getElementById('relValidFrom').value.trim(),
    valid_to: document.getElementById('relValidTo').value.trim(),
    reason: document.getElementById('relReason').value.trim(),
    enabled: document.getElementById('relEnabled').checked
  }};
}}

async function saveRelationFromModal() {{
  try {{
    const payload = {{id: editingRelationId, relation: relationFormPayload()}};
    const data = await api('/api/relations/save', {{method: 'POST', body: JSON.stringify(payload)}});
    closeRelationModal();
    await loadRelationManager();
    showStatus(`关系已保存并同步 JSON 快照：${{(data.snapshot || {{}}).path || ''}}`);
  }} catch (err) {{
    showStatus(err.message, 'err');
  }}
}}

async function deleteRelationRow(id) {{
  if (!confirm('确认删除这条关系映射？')) return;
  try {{
    const data = await api('/api/relations/delete', {{method: 'POST', body: JSON.stringify({{id}})}});
    await loadRelationManager();
    showStatus(`关系已删除并同步 JSON 快照：${{(data.snapshot || {{}}).path || ''}}`);
  }} catch (err) {{
    showStatus(err.message, 'err');
  }}
}}

async function toggleRelation(id, enabled) {{
  try {{
    const data = await api('/api/relations/toggle', {{method: 'POST', body: JSON.stringify({{id, enabled}})}});
    await loadRelationManager();
    showStatus(`关系已${{enabled ? '启用' : '停用'}}并同步 JSON 快照：${{(data.snapshot || {{}}).path || ''}}`);
  }} catch (err) {{
    showStatus(err.message, 'err');
  }}
}}

async function exportRelationJson() {{
  try {{
    const data = await api('/api/relations/export', {{method: 'POST', body: JSON.stringify({{}})}});
    showStatus(`已导出 ${{(data.snapshot || {{}}).count || 0}} 条关系到 ${{(data.snapshot || {{}}).path || ''}}`);
  }} catch (err) {{
    showStatus(err.message, 'err');
  }}
}}

async function importRelationJson() {{
  if (!confirm('确认从私有 config/stock_relations.json 导入并覆盖同 key 关系？')) return;
  try {{
    const data = await api('/api/relations/import', {{method: 'POST', body: JSON.stringify({{}})}});
    await loadRelationManager();
    showStatus(`导入完成：读取 ${{data.counts.read}} 条，写入 ${{data.counts.imported}} 条，跳过 ${{data.counts.skipped}} 条。`);
  }} catch (err) {{
    showStatus(err.message, 'err');
  }}
}}

async function diffRelationJson() {{
  try {{
    const data = await api('/api/relations/diff');
    const diff = data.diff || {{}};
    const text = [
      `数据库：${{diff.db_count || 0}} 条`,
      `JSON：${{diff.json_count || 0}} 条`,
      `JSON 无效行：${{diff.invalid_json_rows || 0}}`,
      '',
      `仅数据库存在：${{(diff.only_in_db || []).length}}`,
      JSON.stringify(diff.only_in_db || [], null, 2),
      '',
      `仅 JSON 存在：${{(diff.only_in_json || []).length}}`,
      JSON.stringify(diff.only_in_json || [], null, 2),
      '',
      `内容不同：${{(diff.changed || []).length}}`,
      JSON.stringify(diff.changed || [], null, 2)
    ].join('\\n');
    document.getElementById('diffText').textContent = text;
    document.getElementById('diffModal').style.display = 'flex';
  }} catch (err) {{
    showStatus(err.message, 'err');
  }}
}}

async function backfillRelations() {{
  if (!confirm('确认重跑最近 N 天信号抽取？这会按当前关系映射补充 related_stock。')) return;
  try {{
    const days = Number(document.getElementById('relationBackfillDays').value || 7);
    const data = await api('/api/relations/backfill', {{method: 'POST', body: JSON.stringify({{days}})}});
    showStatus(`回填完成：最近 ${{data.days}} 天，${{JSON.stringify(data.counts)}}`);
  }} catch (err) {{
    showStatus(err.message, 'err');
  }}
}}

async function loadRelationSuggestions() {{
  try {{
    const status = document.getElementById('relationSuggestionStatus') ? document.getElementById('relationSuggestionStatus').value : 'pending';
    const params = new URLSearchParams();
    if (status) params.set('status', status);
    const data = await api('/api/relation-suggestions?' + params.toString());
    document.getElementById('relationSuggestionRows').innerHTML = (data.suggestions || []).map(item => `
      <tr>
        <td>${{badge(item.status || '')}}<div class="hint">${{formatTime(item.updated_at)}}</div></td>
        <td><strong>${{escapeHtml(item.symbol || '')}}</strong><div class="hint">${{escapeHtml(item.symbol_name || '')}}</div></td>
        <td><strong>${{escapeHtml(item.related_symbol || '')}}</strong><div class="hint">${{escapeHtml(item.related_name || '')}}</div></td>
        <td class="summary-cell">
          <div>${{escapeHtml(item.relation_type || '')}} / ${{escapeHtml(item.theme || '')}} / ${{escapeHtml(item.confidence || '')}}</div>
          <div class="hint">${{escapeHtml(shortText(item.reason || '', 220))}}</div>
        </td>
        <td>
          ${{item.status === 'pending' ? `<button onclick="acceptSuggestion(${{item.id}})">确认</button><button class="danger" onclick="rejectSuggestion(${{item.id}})">拒绝</button>` : '-'}}
        </td>
      </tr>
    `).join('') || '<tr><td colspan="5">暂无候选关系。</td></tr>';
  }} catch (err) {{
    showStatus(err.message, 'err');
  }}
}}

async function acceptSuggestion(id) {{
  try {{
    const data = await api('/api/relation-suggestions/accept', {{method: 'POST', body: JSON.stringify({{id}})}});
    await loadRelationManager();
    showStatus(`候选关系已确认并同步 JSON 快照：${{(data.snapshot || {{}}).path || ''}}`);
  }} catch (err) {{
    showStatus(err.message, 'err');
  }}
}}

async function rejectSuggestion(id) {{
  try {{
    await api('/api/relation-suggestions/reject', {{method: 'POST', body: JSON.stringify({{id}})}});
    await loadRelationSuggestions();
    showStatus('候选关系已拒绝。');
  }} catch (err) {{
    showStatus(err.message, 'err');
  }}
}}

async function loadHealth() {{
  try {{
    const data = await api('/api/health');
    document.getElementById('healthRows').innerHTML = (data.units || []).map(unit => `
      <tr>
        <td>${{escapeHtml(unit.Id || '')}}</td>
        <td>${{badge(unit.ActiveState || unit.LoadState || '')}}</td>
        <td>${{escapeHtml(unit.SubState || '')}}</td>
        <td>${{escapeHtml(unit.Result || unit.error || '')}}</td>
        <td>${{escapeHtml(unit.NRestarts || '')}}</td>
        <td>${{escapeHtml(unit.ExecMainStartTimestamp || unit.LastTriggerUSec || unit.NextElapseUSecRealtime || '')}}</td>
      </tr>
    `).join('');
    document.getElementById('sourceHealthRows').innerHTML = (data.sources || []).map(source => `
      <tr>
        <td>${{escapeHtml(source.monitor || '')}}</td>
        <td>${{escapeHtml(source.source || '')}}</td>
        <td>${{badge(source.status || '')}}</td>
        <td>${{escapeHtml(String(source.consecutive_failures || 0))}}</td>
        <td>${{formatTime(source.last_success_at || '')}}</td>
        <td>${{formatTime(source.last_failure_at || '')}}</td>
        <td class="summary-cell">${{escapeHtml(shortText(source.last_error || '', 180))}}</td>
      </tr>
    `).join('') || '<tr><td colspan="7">暂无来源健康记录。</td></tr>';
    document.getElementById('healthLogs').innerHTML = (data.logs || []).map(log => `
      <section class="panel" style="margin-top:12px">
        <div class="list-row" style="padding:10px 12px"><strong>${{escapeHtml(log.name)}}</strong></div>
        <div class="log">${{escapeHtml(log.tail || '')}}</div>
      </section>
    `).join('');
  }} catch (err) {{
    showStatus(err.message, 'err');
  }}
}}

function keywordTextToList(value) {{
  return String(value || '').split(/[，,;；\\n]+/).map(s => s.trim()).filter(Boolean);
}}

function keywordListToText(value) {{
  return Array.isArray(value) ? value.join('\\n') : '';
}}

function sameKeywordList(a, b) {{
  const left = (a || []).map(item => String(item || '').trim()).filter(Boolean);
  const right = (b || []).map(item => String(item || '').trim()).filter(Boolean);
  if (left.length !== right.length) return false;
  return left.every((item, index) => item === right[index]);
}}

async function loadKeywords() {{
  try {{
    const data = await api('/api/media-keywords');
    codeDefaultKeywords = data.code_default_keywords || data.default_keywords || [];
    document.getElementById('baseKeywords').value = keywordListToText(data.base_keywords || data.default_keywords || []);
    document.getElementById('includeKeywords').value = keywordListToText(data.include_keywords || []);
    document.getElementById('excludeKeywords').value = keywordListToText(data.exclude_keywords || []);
    document.getElementById('baseOverrideStatus').textContent = data.base_keywords_overridden ? '已自定义' : '使用代码默认';
    document.getElementById('defaultKeywords').innerHTML = codeDefaultKeywords.map(item => `<span class="badge" style="margin:2px">${{escapeHtml(item)}}</span>`).join('');
  }} catch (err) {{
    showStatus(err.message, 'err');
  }}
}}

function resetBaseKeywords() {{
  document.getElementById('baseKeywords').value = keywordListToText(codeDefaultKeywords);
  showStatus('已把基础关键词恢复为代码默认词，点击保存后生效。');
}}

async function saveKeywords() {{
  try {{
    const baseKeywords = keywordTextToList(document.getElementById('baseKeywords').value);
    const payload = {{
      base_keywords: sameKeywordList(baseKeywords, codeDefaultKeywords) ? [] : baseKeywords,
      include_keywords: keywordTextToList(document.getElementById('includeKeywords').value),
      exclude_keywords: keywordTextToList(document.getElementById('excludeKeywords').value)
    }};
    const data = await api('/api/media-keywords', {{method: 'POST', body: JSON.stringify(payload)}});
    codeDefaultKeywords = data.code_default_keywords || data.default_keywords || codeDefaultKeywords;
    document.getElementById('baseKeywords').value = keywordListToText(data.base_keywords || data.default_keywords || []);
    document.getElementById('includeKeywords').value = keywordListToText(data.include_keywords || []);
    document.getElementById('excludeKeywords').value = keywordListToText(data.exclude_keywords || []);
    document.getElementById('baseOverrideStatus').textContent = data.base_keywords_overridden ? '已自定义' : '使用代码默认';
    showStatus(`媒体关键词已保存。基础 ${{(data.base_keywords || data.default_keywords || []).length}} 个，额外包含 ${{(data.include_keywords || []).length}} 个，排除 ${{(data.exclude_keywords || []).length}} 个。`);
  }} catch (err) {{
    showStatus(err.message, 'err');
  }}
}}

async function loadSettings() {{
  try {{
    const data = await api('/api/settings');
    const grid = document.getElementById('settingsGrid');
    grid.innerHTML = (data.groups || []).map(group => `
      <section class="settings-card">
        <h3>${{escapeHtml(group.title || group.id || '')}}</h3>
        <div class="hint">${{escapeHtml(group.restart_hint || '')}}</div>
        ${{(group.fields || []).map(field => `
          <div class="setting-field">
            <label>
              <span>${{escapeHtml(field.label || field.key || '')}}</span>
              <span class="setting-mask">${{field.sensitive ? (field.configured ? '已配置 ' + escapeHtml(field.masked || '') : '未配置') : ''}}</span>
            </label>
            <input
              data-setting-key="${{escapeHtml(field.key || '')}}"
              data-sensitive="${{field.sensitive ? '1' : '0'}}"
              value="${{field.sensitive ? '' : escapeHtml(field.value || '')}}"
              placeholder="${{escapeHtml(field.sensitive ? '留空保留现有值；输入新值覆盖' : (field.placeholder || ''))}}"
              autocomplete="off"
            >
            ${{field.help ? `<div class="hint">${{escapeHtml(field.help)}}</div>` : ''}}
          </div>
        `).join('')}}
      </section>
    `).join('');
  }} catch (err) {{
    showStatus(err.message, 'err');
  }}
}}

async function saveSettings() {{
  try {{
    const values = {{}};
    document.querySelectorAll('[data-setting-key]').forEach(input => {{
      const key = input.dataset.settingKey;
      const sensitive = input.dataset.sensitive === '1';
      const value = input.value.trim();
      if (!key) return;
      if (sensitive && !value) return;
      values[key] = value;
    }});
    const data = await api('/api/settings', {{method: 'POST', body: JSON.stringify({{values}})}});
    const changed = (data.changed || []).map(item => `${{item.key}}: ${{item.old || '<空>'}} -> ${{item.new || '<空>'}}`).join('\\n');
    await loadSettings();
    showStatus(changed ? `配置已保存：\\n${{changed}}\\n\\n如需立即生效，请重启对应服务。` : '没有配置变化。');
  }} catch (err) {{
    showStatus(err.message, 'err');
  }}
}}

function readRow(row, item={{}}) {{
  return {{
    ...item,
    enabled: row.querySelector('[data-field="enabled"]').checked,
    symbol: row.querySelector('[data-field="symbol"]').value.trim(),
    name: row.querySelector('[data-field="name"]').value.trim(),
    full_name: row.querySelector('[data-field="full_name"]').value.trim(),
    aliases: splitList(row.querySelector('[data-field="aliases"]').value),
    business_summary: row.querySelector('[data-field="business_summary"]').value.trim(),
    news_keywords: splitList(row.querySelector('[data-field="news_keywords"]').value),
    news_exclude_keywords: splitList(row.querySelector('[data-field="news_exclude_keywords"]').value)
  }};
}}

function syncRowsFromDom() {{
  document.querySelectorAll('#rows tr[data-index]').forEach(row => {{
    const index = Number(row.dataset.index);
    if (Number.isInteger(index) && index >= 0 && index < holdings.length) {{
      holdings[index] = readRow(row, holdings[index] || {{}});
    }}
  }});
}}

function currentRows() {{
  syncRowsFromDom();
  return holdings.map(item => ({{
    enabled: item.enabled !== false,
    symbol: String(item.symbol || '').trim(),
    name: String(item.name || '').trim(),
    full_name: String(item.full_name || '').trim(),
    aliases: splitList(Array.isArray(item.aliases) ? item.aliases.join('，') : item.aliases),
    business_summary: String(item.business_summary || '').trim(),
    news_keywords: splitList(Array.isArray(item.news_keywords) ? item.news_keywords.join('，') : item.news_keywords),
    news_exclude_keywords: splitList(Array.isArray(item.news_exclude_keywords) ? item.news_exclude_keywords.join('，') : item.news_exclude_keywords)
  }}));
}}

function renderTable(sync=true) {{
  if (sync) syncRowsFromDom();
  const q = document.getElementById('filter').value.trim().toLowerCase();
  const body = document.getElementById('rows');
  body.innerHTML = '';
  let visible = 0;
  holdings.forEach((item, index) => {{
    const hay = JSON.stringify(item).toLowerCase();
    if (q && !hay.includes(q)) return;
    visible += 1;
    const tr = document.createElement('tr');
    tr.dataset.index = index;
    tr.innerHTML = `
      <td class="enabled"><input data-field="enabled" type="checkbox" ${{item.enabled !== false ? 'checked' : ''}}></td>
      <td class="symbol"><input data-field="symbol" value="${{escapeHtml(item.symbol || '')}}"></td>
      <td class="name"><input data-field="name" value="${{escapeHtml(item.name || '')}}"></td>
      <td class="full"><textarea data-field="full_name">${{escapeHtml(item.full_name || '')}}</textarea></td>
      <td><textarea data-field="aliases">${{escapeHtml(joinList(item.aliases))}}</textarea></td>
      <td><textarea data-field="business_summary">${{escapeHtml(item.business_summary || '')}}</textarea></td>
      <td><textarea data-field="news_keywords">${{escapeHtml(joinList(item.news_keywords))}}</textarea></td>
      <td><textarea data-field="news_exclude_keywords">${{escapeHtml(joinList(item.news_exclude_keywords))}}</textarea></td>
      <td class="actions"><button class="danger" onclick="removeRow(${{index}})">删除</button></td>
    `;
    tr.addEventListener('input', () => {{
      holdings[index] = readRow(tr, holdings[index] || {{}});
    }});
    tr.addEventListener('change', () => {{
      holdings[index] = readRow(tr, holdings[index] || {{}});
    }});
    body.appendChild(tr);
  }});
  document.getElementById('summary').textContent = `共 ${{holdings.length}} 只，显示 ${{visible}} 只`;
}}

async function reloadData() {{
  try {{
    const data = await api('/api/holdings');
    holdings = data.holdings || [];
    loadedHoldings = true;
    renderTable(false);
    showStatus('已加载持仓。');
  }} catch (err) {{
    showStatus(err.message, 'err');
  }}
}}

function addRow() {{
  syncRowsFromDom();
  holdings.push({{enabled: true, symbol: '', name: '', aliases: [], news_keywords: [], news_exclude_keywords: []}});
  renderTable(false);
}}

function removeRow(index) {{
  if (!confirm('确认删除这只持仓？')) return;
  syncRowsFromDom();
  holdings.splice(index, 1);
  renderTable(false);
}}

function openBatch() {{ document.getElementById('batchModal').style.display = 'flex'; }}
function closeBatch() {{ document.getElementById('batchModal').style.display = 'none'; }}
function closeDiff() {{ document.getElementById('diffModal').style.display = 'none'; }}

function parseBatchLine(line) {{
  const parts = line.split(/[，,\\t]+/).map(s => s.trim()).filter(Boolean);
  if (!parts.length) return null;
  const codeLike = value => /^(\\d{{6}}(\\.(SH|SZ|BJ))?|HK\\d{{1,5}}|0?\\d{{4,5}}\\.HK)$/i.test(value);
  if (parts.length === 1) {{
    const only = parts[0];
    if (codeLike(only)) return {{symbol: only, name: only, enabled: true}};
    return {{symbol: '', name: only, enabled: true}};
  }}
  const [a, b] = parts;
  if (codeLike(a)) return {{symbol: a, name: b, enabled: true}};
  return {{symbol: b, name: a, enabled: true}};
}}

function applyBatch() {{
  syncRowsFromDom();
  const lines = document.getElementById('batchText').value.split(/\\n+/);
  const parsed = lines.map(parseBatchLine).filter(Boolean);
  holdings.push(...parsed);
  document.getElementById('batchText').value = '';
  closeBatch();
  renderTable(false);
}}

async function previewSave() {{
  try {{
    pendingPayload = currentRows();
    const data = await api('/api/preview', {{method: 'POST', body: JSON.stringify({{holdings: pendingPayload}})}});
    const warnings = (data.warnings || []).map(item => `! ${{item.message || item}}`).join('\\n');
    document.getElementById('diffText').textContent = [warnings ? `校验提醒：\\n${{warnings}}` : '', data.diff_text || '没有变化。'].filter(Boolean).join('\\n\\n');
    document.getElementById('diffModal').style.display = 'flex';
  }} catch (err) {{
    showStatus(err.message, 'err');
  }}
}}

async function confirmSave() {{
  try {{
    const data = await api('/api/save', {{method: 'POST', body: JSON.stringify({{holdings: pendingPayload || currentRows()}})}});
    closeDiff();
    showStatus(`保存成功。\\n备份：${{data.backup_path || '无'}}\\n已同步 SQLite：${{data.imported_count}} 只持仓。`);
    holdings = data.holdings || holdings;
    renderTable();
  }} catch (err) {{
    showStatus(err.message, 'err');
  }}
}}

document.getElementById('eventDate').value = todayString();
showView('overview');
</script>
</body>
</html>"""


def diff_text(diff: dict[str, Any]) -> str:
    lines: list[str] = []
    added = diff.get("added") or []
    removed = diff.get("removed") or []
    changed = diff.get("changed") or []
    if added:
        lines.append("新增：")
        for item in added:
            lines.append(f"+ {item.get('symbol', '')} {item.get('name', '')}")
    if removed:
        lines.append("删除：")
        for item in removed:
            lines.append(f"- {item.get('symbol', '')} {item.get('name', '')}")
    if changed:
        lines.append("修改：")
        for item in changed:
            before = item.get("before", {})
            after = item.get("after", {})
            lines.append(f"* {after.get('symbol') or before.get('symbol')} {before.get('name', '')} -> {after.get('name', '')}")
    return "\n".join(lines) or "没有变化。"


class HoldingsHandler(BaseHTTPRequestHandler):
    server_version = "SurveilHoldingsWeb/0.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"{self.address_string()} - {fmt % args}", flush=True)

    @property
    def token(self) -> str:
        return str(getattr(self.server, "token", ""))

    @property
    def restart_sina_flash(self) -> bool:
        return bool(getattr(self.server, "restart_sina_flash", False))

    def authorized(self) -> bool:
        if not self.token:
            return True
        supplied = self.headers.get("X-Holdings-Token", "")
        if supplied == self.token:
            return True
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        return (qs.get("token") or [""])[0] == self.token

    def send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, content: str) -> None:
        body = content.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or "0")
        raw = self.rfile.read(length).decode("utf-8")
        return json.loads(raw or "{}")

    def require_auth(self) -> bool:
        if self.authorized():
            return True
        self.send_json({"ok": False, "error": "未授权，请输入 HOLDINGS_WEB_TOKEN"}, HTTPStatus.UNAUTHORIZED)
        return False

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self.send_html(html_page(bool(self.token)))
            return
        if parsed.path == "/api/holdings":
            if not self.require_auth():
                return
            try:
                self.send_json({"ok": True, "holdings": normalized_holdings()})
            except Exception as exc:  # noqa: BLE001
                self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        if parsed.path == "/api/overview":
            if not self.require_auth():
                return
            try:
                qs = parse_qs(parsed.query)
                self.send_json(overview_payload((qs.get("date") or [""])[0]))
            except Exception as exc:  # noqa: BLE001
                self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        if parsed.path == "/api/events":
            if not self.require_auth():
                return
            try:
                qs = parse_qs(parsed.query)
                limit_raw = (qs.get("limit") or ["100"])[0]
                try:
                    limit = int(limit_raw)
                except ValueError:
                    limit = 100
                events = fetch_events_rows(
                    day=(qs.get("date") or [""])[0],
                    source=(qs.get("source") or [""])[0],
                    kind=(qs.get("kind") or [""])[0],
                    q=(qs.get("q") or [""])[0],
                    limit=limit,
                )
                self.send_json({"ok": True, "events": events})
            except Exception as exc:  # noqa: BLE001
                self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        if parsed.path == "/api/signals":
            if not self.require_auth():
                return
            try:
                qs = parse_qs(parsed.query)
                limit_raw = (qs.get("limit") or ["100"])[0]
                try:
                    limit = int(limit_raw)
                except ValueError:
                    limit = 100
                self.send_json(
                    {
                        "ok": True,
                        "summary": fetch_signal_summary(),
                        "signals": fetch_signal_rows(
                            q=(qs.get("q") or [""])[0],
                            source=(qs.get("source") or [""])[0],
                            symbol=(qs.get("symbol") or [""])[0],
                            verdict=(qs.get("verdict") or [""])[0],
                            importance=(qs.get("importance") or [""])[0],
                            limit=limit,
                        ),
                    }
                )
            except Exception as exc:  # noqa: BLE001
                self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        if parsed.path == "/api/signal-relations":
            if not self.require_auth():
                return
            try:
                qs = parse_qs(parsed.query)
                limit_raw = (qs.get("limit") or ["100"])[0]
                try:
                    limit = int(limit_raw)
                except ValueError:
                    limit = 100
                self.send_json(
                    {
                        "ok": True,
                        "relations": fetch_relation_rows(q=(qs.get("q") or [""])[0], limit=limit),
                    }
                )
            except Exception as exc:  # noqa: BLE001
                self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        if parsed.path == "/api/relations":
            if not self.require_auth():
                return
            try:
                qs = parse_qs(parsed.query)
                limit_raw = (qs.get("limit") or ["300"])[0]
                try:
                    limit = int(limit_raw)
                except ValueError:
                    limit = 300
                self.send_json(
                    {
                        "ok": True,
                        "relations": fetch_relation_rows(
                            q=(qs.get("q") or [""])[0],
                            enabled=(qs.get("enabled") or ["all"])[0],
                            limit=limit,
                        ),
                    }
                )
            except Exception as exc:  # noqa: BLE001
                self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        if parsed.path == "/api/relations/diff":
            if not self.require_auth():
                return
            try:
                self.send_json(
                    {
                        "ok": True,
                        "diff": diff_relations(db_path=DEFAULT_DB_PATH, config_path=STOCK_RELATIONS_CONFIG_PATH),
                    }
                )
            except Exception as exc:  # noqa: BLE001
                self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        if parsed.path == "/api/relation-suggestions":
            if not self.require_auth():
                return
            try:
                qs = parse_qs(parsed.query)
                self.send_json(
                    {
                        "ok": True,
                        "suggestions": list_relation_suggestions(
                            db_path=DEFAULT_DB_PATH,
                            status=(qs.get("status") or ["pending"])[0],
                            limit=100,
                        ),
                    }
                )
            except Exception as exc:  # noqa: BLE001
                self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        if parsed.path == "/api/health":
            if not self.require_auth():
                return
            try:
                self.send_json(health_payload())
            except Exception as exc:  # noqa: BLE001
                self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        if parsed.path == "/api/media-keywords":
            if not self.require_auth():
                return
            try:
                payload = media_keyword_payload()
                payload["ok"] = True
                self.send_json(payload)
            except Exception as exc:  # noqa: BLE001
                self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        if parsed.path == "/api/settings":
            if not self.require_auth():
                return
            try:
                payload = settings_payload()
                payload["ok"] = True
                self.send_json(payload)
            except Exception as exc:  # noqa: BLE001
                self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        if not self.require_auth():
            return
        parsed = urlparse(self.path)
        try:
            payload = self.read_json()
            if parsed.path == "/api/media-keywords":
                base_keywords = payload.get("base_keywords")
                include_keywords = payload.get("include_keywords")
                exclude_keywords = payload.get("exclude_keywords")
                if not isinstance(include_keywords, list) or not isinstance(exclude_keywords, list):
                    raise HoldingsError("请求缺少 include_keywords / exclude_keywords 数组")
                if base_keywords is not None and not isinstance(base_keywords, list):
                    raise HoldingsError("base_keywords 必须是数组")
                saved = save_media_keyword_config(base_keywords, include_keywords, exclude_keywords)
                saved.update(
                    {
                        "code_default_keywords": media_keyword_payload()["code_default_keywords"],
                        "default_keywords": saved["base_keywords"] or media_keyword_payload()["code_default_keywords"],
                        "base_keywords_overridden": bool(saved["base_keywords"]),
                    }
                )
                saved["ok"] = True
                self.send_json(saved)
                return
            if parsed.path == "/api/settings":
                values = payload.get("values")
                if not isinstance(values, dict):
                    raise HoldingsError("请求缺少 values 对象")
                saved = save_settings(values)
                saved["ok"] = True
                self.send_json(saved)
                return
            if parsed.path == "/api/signal-feedback":
                saved = save_signal_feedback(payload)
                saved["ok"] = True
                self.send_json(saved)
                return
            if parsed.path == "/api/relations/save":
                relation = payload.get("relation")
                if not isinstance(relation, dict):
                    raise HoldingsError("请求缺少 relation 对象")
                relation_id = payload.get("id")
                saved_relation = save_relation(
                    relation,
                    db_path=DEFAULT_DB_PATH,
                    relation_id=int(relation_id) if relation_id else None,
                )
                response = {"ok": True, "relation": saved_relation}
                response.update(relation_snapshot_payload())
                self.send_json(response)
                return
            if parsed.path == "/api/relations/delete":
                relation_id = int(payload.get("id") or 0)
                if relation_id <= 0:
                    raise HoldingsError("请求缺少有效 id")
                deleted = delete_relation(relation_id=relation_id, db_path=DEFAULT_DB_PATH)
                response = {"ok": True, "deleted": deleted}
                response.update(relation_snapshot_payload())
                self.send_json(response)
                return
            if parsed.path == "/api/relations/toggle":
                relation_id = int(payload.get("id") or 0)
                if relation_id <= 0:
                    raise HoldingsError("请求缺少有效 id")
                enabled = bool(payload.get("enabled"))
                relation = set_relation_enabled(relation_id=relation_id, enabled=enabled, db_path=DEFAULT_DB_PATH)
                response = {"ok": True, "relation": relation}
                response.update(relation_snapshot_payload())
                self.send_json(response)
                return
            if parsed.path == "/api/relations/export":
                response = {"ok": True}
                response.update(relation_snapshot_payload())
                self.send_json(response)
                return
            if parsed.path == "/api/relations/import":
                counts = import_relations(db_path=DEFAULT_DB_PATH, config_path=STOCK_RELATIONS_CONFIG_PATH)
                response = {"ok": True, "counts": counts}
                response.update(relation_snapshot_payload())
                self.send_json(response)
                return
            if parsed.path == "/api/relations/backfill":
                response = {"ok": True}
                response.update(run_relation_backfill(int(payload.get("days") or 7)))
                self.send_json(response)
                return
            if parsed.path == "/api/relation-suggestions/accept":
                suggestion_id = int(payload.get("id") or 0)
                if suggestion_id <= 0:
                    raise HoldingsError("请求缺少有效 id")
                relation = accept_relation_suggestion(suggestion_id=suggestion_id, db_path=DEFAULT_DB_PATH)
                response = {"ok": True, "relation": relation}
                response.update(relation_snapshot_payload())
                self.send_json(response)
                return
            if parsed.path == "/api/relation-suggestions/reject":
                suggestion_id = int(payload.get("id") or 0)
                if suggestion_id <= 0:
                    raise HoldingsError("请求缺少有效 id")
                rejected = reject_relation_suggestion(suggestion_id=suggestion_id, db_path=DEFAULT_DB_PATH)
                self.send_json({"ok": True, "rejected": rejected})
                return
            items = payload.get("holdings")
            if not isinstance(items, list):
                raise HoldingsError("请求缺少 holdings 数组")
            current = normalized_holdings()
            normalized = normalize_holdings_for_save(items, current)
            if parsed.path == "/api/preview":
                warnings = validate_holdings(normalized, verify_remote=True)
                diff = holdings_diff(current, normalized)
                self.send_json(
                    {
                        "ok": True,
                        "diff": diff,
                        "diff_text": diff_text(diff),
                        "holdings": normalized,
                        "warnings": warnings,
                    }
                )
                return
            if parsed.path == "/api/save":
                result = save_holdings(normalized, db_path=DEFAULT_DB_PATH)
                if self.restart_sina_flash:
                    subprocess.run(["systemctl", "restart", "surveil-sina-flash.service"], check=False)
                self.send_json(
                    {
                        "ok": True,
                        "backup_path": str(result.backup_path) if result.backup_path else "",
                        "imported_count": result.imported_count,
                        "changed_count": result.changed_count,
                        "holdings": normalized_holdings(),
                    }
                )
                return
        except Exception as exc:  # noqa: BLE001
            self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        self.send_error(HTTPStatus.NOT_FOUND)


def main() -> int:
    parser = argparse.ArgumentParser(description="Surveil 持仓管理 Web UI")
    parser.add_argument("--host", default=os.getenv("HOLDINGS_WEB_HOST", DEFAULT_HOST))
    parser.add_argument("--port", type=int, default=int(os.getenv("HOLDINGS_WEB_PORT", str(DEFAULT_PORT))))
    args = parser.parse_args()

    load_env(ROOT / ".env")
    host = args.host
    port = args.port
    server = ThreadingHTTPServer((host, port), HoldingsHandler)
    server.token = os.getenv("HOLDINGS_WEB_TOKEN", "").strip()
    server.restart_sina_flash = env_flag("HOLDINGS_WEB_RESTART_SINA_FLASH", False)
    print(f"Surveil holdings web listening on http://{host}:{port}", flush=True)
    if not server.token:
        print("WARNING: HOLDINGS_WEB_TOKEN 未配置。请仅通过 SSH 隧道访问。", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
