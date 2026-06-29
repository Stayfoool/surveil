"""Storage helpers for investment signal outcome tracking."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from db_utils import connect_sqlite
from market_db import DEFAULT_DB_PATH, init_db


PROMPT_VERSION = "signal-loop-mvp-v1"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def json_loads(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


def normalize_importance(value: str) -> str:
    raw = str(value or "").strip().lower()
    mapping = {
        "高": "high",
        "重要": "high",
        "中": "medium",
        "中等": "medium",
        "低": "low",
        "不重要": "low",
    }
    return mapping.get(raw, raw)


def normalize_direction(value: str) -> str:
    raw = str(value or "").strip().lower()
    mapping = {
        "positive": "positive",
        "negative": "negative",
        "neutral": "neutral",
        "uncertain": "uncertain",
        "上涨": "positive",
        "利好": "positive",
        "增量利好": "positive",
        "下跌": "negative",
        "利空": "negative",
        "增量利空": "negative",
        "震荡或中性": "neutral",
        "中性": "neutral",
        "无法判断": "uncertain",
    }
    return mapping.get(raw, raw or "uncertain")


def normalize_symbol(value: str) -> str:
    return str(value or "").strip().upper()


def symbol_market(symbol: str) -> str:
    normalized = normalize_symbol(symbol)
    if normalized.endswith((".SZ", ".SH", ".BJ")):
        return "A股"
    if normalized.endswith(".HK"):
        return "港股"
    if normalized.endswith((".JP", ".KS", ".KQ", ".TW", ".TWO")):
        return "海外"
    if normalized:
        return "海外/未知"
    return "行业环节"


def is_a_share_symbol(symbol: str) -> bool:
    return normalize_symbol(symbol).endswith((".SZ", ".SH", ".BJ"))


def target_key(target: dict[str, Any]) -> str:
    symbol = normalize_symbol(str(target.get("symbol") or ""))
    if symbol:
        return symbol
    name = str(target.get("name") or target.get("target") or "").strip()
    market = str(target.get("market") or "").strip()
    return f"{market}:{name}".strip(":") or "unknown"


def ensure_signal_tables(conn: sqlite3.Connection) -> None:
    from market_db import SCHEMA

    conn.executescript(SCHEMA)
    conn.commit()


def upsert_signal(
    conn: sqlite3.Connection,
    signal: dict[str, Any],
    *,
    targets: list[dict[str, Any]] | None = None,
    evidence: list[dict[str, Any]] | None = None,
) -> int:
    """Insert or update one signal and its traceable targets/evidence."""
    ensure_signal_tables(conn)
    now = utc_now()
    source_table = str(signal["source_table"])
    source_id = str(signal["source_id"])
    conn.execute(
        """
        INSERT INTO signals (
            source_table, source_id, source, source_item_id, title, url,
            published_at, first_seen_at, pushed_at, importance,
            incremental_classification, direction, confidence, thesis,
            invalidation, model, prompt_version, raw_json, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_table, source_id) DO UPDATE SET
            source = excluded.source,
            source_item_id = excluded.source_item_id,
            title = excluded.title,
            url = excluded.url,
            published_at = excluded.published_at,
            first_seen_at = excluded.first_seen_at,
            pushed_at = excluded.pushed_at,
            importance = excluded.importance,
            incremental_classification = excluded.incremental_classification,
            direction = excluded.direction,
            confidence = excluded.confidence,
            thesis = excluded.thesis,
            invalidation = excluded.invalidation,
            model = excluded.model,
            prompt_version = excluded.prompt_version,
            raw_json = excluded.raw_json,
            updated_at = excluded.updated_at
        """,
        (
            source_table,
            source_id,
            str(signal.get("source") or ""),
            str(signal.get("source_item_id") or ""),
            str(signal.get("title") or ""),
            str(signal.get("url") or ""),
            str(signal.get("published_at") or ""),
            str(signal.get("first_seen_at") or ""),
            str(signal.get("pushed_at") or ""),
            normalize_importance(str(signal.get("importance") or "")),
            str(signal.get("incremental_classification") or ""),
            normalize_direction(str(signal.get("direction") or "")),
            str(signal.get("confidence") or ""),
            str(signal.get("thesis") or ""),
            str(signal.get("invalidation") or ""),
            str(signal.get("model") or ""),
            str(signal.get("prompt_version") or PROMPT_VERSION),
            json_dumps(signal.get("raw") or {}),
            now,
            now,
        ),
    )
    row = conn.execute(
        "SELECT id FROM signals WHERE source_table = ? AND source_id = ?",
        (source_table, source_id),
    ).fetchone()
    if not row:
        raise RuntimeError(f"signal upsert failed: {source_table}/{source_id}")
    signal_id = int(row[0])
    for target in targets or []:
        upsert_target(conn, signal_id, target)
    for item in evidence or []:
        insert_evidence(conn, signal_id, item)
    conn.commit()
    return signal_id


def upsert_target(conn: sqlite3.Connection, signal_id: int, target: dict[str, Any]) -> int:
    now = utc_now()
    symbol = normalize_symbol(str(target.get("symbol") or ""))
    key = target_key(target)
    role = str(target.get("target_role") or target.get("role") or "unknown")
    market = str(target.get("market") or symbol_market(symbol))
    conn.execute(
        """
        INSERT INTO signal_targets (
            signal_id, target_key, symbol, name, market, target_role,
            expected_direction, expected_horizon, relation_type, relation_reason,
            confidence, raw_json, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(signal_id, target_key, target_role) DO UPDATE SET
            symbol = excluded.symbol,
            name = excluded.name,
            market = excluded.market,
            expected_direction = excluded.expected_direction,
            expected_horizon = excluded.expected_horizon,
            relation_type = excluded.relation_type,
            relation_reason = excluded.relation_reason,
            confidence = excluded.confidence,
            raw_json = excluded.raw_json,
            updated_at = excluded.updated_at
        """,
        (
            signal_id,
            key,
            symbol,
            str(target.get("name") or target.get("target") or ""),
            market,
            role,
            normalize_direction(str(target.get("expected_direction") or target.get("impact_direction") or "")),
            str(target.get("expected_horizon") or target.get("duration") or ""),
            str(target.get("relation_type") or target.get("relation") or ""),
            str(target.get("relation_reason") or target.get("reason") or ""),
            str(target.get("confidence") or ""),
            json_dumps(target),
            now,
            now,
        ),
    )
    row = conn.execute(
        """
        SELECT id FROM signal_targets
        WHERE signal_id = ? AND target_key = ? AND target_role = ?
        """,
        (signal_id, key, role),
    ).fetchone()
    if not row:
        raise RuntimeError(f"signal target upsert failed: {signal_id}/{key}")
    return int(row[0])


def insert_evidence(conn: sqlite3.Connection, signal_id: int, evidence: dict[str, Any]) -> None:
    text = str(evidence.get("text") or "").strip()
    if not text:
        return
    conn.execute(
        """
        INSERT OR IGNORE INTO signal_evidence (
            signal_id, evidence_type, text, url, source, observed_at, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            signal_id,
            str(evidence.get("evidence_type") or "source"),
            text,
            str(evidence.get("url") or ""),
            str(evidence.get("source") or ""),
            str(evidence.get("observed_at") or ""),
            utc_now(),
        ),
    )


def upsert_outcome(
    conn: sqlite3.Connection,
    *,
    signal_id: int,
    symbol: str,
    as_of_date: str,
    outcome_status: str,
    target_id: int | None = None,
    metrics: dict[str, Any] | None = None,
    outcome_json: dict[str, Any] | None = None,
) -> int:
    ensure_signal_tables(conn)
    metrics = metrics or {}
    conn.execute(
        """
        INSERT INTO signal_outcomes (
            signal_id, target_id, symbol, as_of_date, return_1d, return_3d,
            return_5d, return_10d, return_20d, excess_return_1d,
            excess_return_3d, excess_return_5d, excess_return_10d,
            excess_return_20d, max_drawdown, max_runup, volume_change,
            limit_up_days, matched_direction, outcome_status, outcome_json, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(signal_id, symbol, as_of_date) DO UPDATE SET
            target_id = excluded.target_id,
            return_1d = excluded.return_1d,
            return_3d = excluded.return_3d,
            return_5d = excluded.return_5d,
            return_10d = excluded.return_10d,
            return_20d = excluded.return_20d,
            excess_return_1d = excluded.excess_return_1d,
            excess_return_3d = excluded.excess_return_3d,
            excess_return_5d = excluded.excess_return_5d,
            excess_return_10d = excluded.excess_return_10d,
            excess_return_20d = excluded.excess_return_20d,
            max_drawdown = excluded.max_drawdown,
            max_runup = excluded.max_runup,
            volume_change = excluded.volume_change,
            limit_up_days = excluded.limit_up_days,
            matched_direction = excluded.matched_direction,
            outcome_status = excluded.outcome_status,
            outcome_json = excluded.outcome_json,
            updated_at = excluded.updated_at
        """,
        (
            signal_id,
            target_id,
            normalize_symbol(symbol),
            as_of_date,
            metrics.get("return_1d"),
            metrics.get("return_3d"),
            metrics.get("return_5d"),
            metrics.get("return_10d"),
            metrics.get("return_20d"),
            metrics.get("excess_return_1d"),
            metrics.get("excess_return_3d"),
            metrics.get("excess_return_5d"),
            metrics.get("excess_return_10d"),
            metrics.get("excess_return_20d"),
            metrics.get("max_drawdown"),
            metrics.get("max_runup"),
            metrics.get("volume_change"),
            metrics.get("limit_up_days"),
            str(metrics.get("matched_direction") or ""),
            outcome_status,
            json_dumps(outcome_json or {}),
            utc_now(),
        ),
    )
    row = conn.execute(
        "SELECT id FROM signal_outcomes WHERE signal_id = ? AND symbol = ? AND as_of_date = ?",
        (signal_id, normalize_symbol(symbol), as_of_date),
    ).fetchone()
    if not row:
        raise RuntimeError(f"signal outcome upsert failed: {signal_id}/{symbol}/{as_of_date}")
    conn.commit()
    return int(row[0])


def connect_db(path: Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    init_db(path).close()
    return connect_sqlite(path)
