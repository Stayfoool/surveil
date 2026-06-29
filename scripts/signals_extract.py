#!/usr/bin/env python3
"""Extract traceable investment signals from existing monitored items."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from db_utils import connect_sqlite
from env_utils import load_env
from market_db import DEFAULT_DB_PATH, init_db
from signal_store import (
    PROMPT_VERSION,
    json_loads,
    normalize_direction,
    normalize_importance,
    normalize_symbol,
    symbol_market,
    target_key,
    upsert_signal,
)


IMPORTANT_LEVELS = {"high", "medium"}
SKIPPED_SOURCES = {"jygs", "jygs_actions", "jygs_events"}
A_SHARE_CODE_RE = re.compile(r"(?<!\d)([034689][0-9]{5})(?:\.(SZ|SH|BJ))?(?!\d)", re.IGNORECASE)
CASHTAG_RE = re.compile(r"\$([A-Za-z][A-Za-z0-9._-]{0,12})")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def cutoff_iso(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=max(1, days))).isoformat()


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    return bool(row)


def scalar_from_path(data: dict[str, Any], *path: str) -> str:
    node: Any = data
    for key in path:
        if not isinstance(node, dict):
            return ""
        node = node.get(key)
    return str(node or "").strip()


def list_from_json(value: str | None) -> list[Any]:
    parsed = json_loads(value, [])
    return parsed if isinstance(parsed, list) else []


def should_include(importance: str, pushed: bool, include_medium: bool) -> bool:
    normalized = normalize_importance(importance)
    if pushed or normalized == "high":
        return True
    return include_medium and normalized == "medium"


def holding_names(conn: sqlite3.Connection) -> dict[str, str]:
    if not table_exists(conn, "portfolio_holdings"):
        return {}
    rows = conn.execute("SELECT symbol, name FROM portfolio_holdings WHERE enabled = 1").fetchall()
    return {normalize_symbol(str(symbol)): str(name or "") for symbol, name in rows}


def stock_names(conn: sqlite3.Connection) -> dict[str, str]:
    if not table_exists(conn, "stocks"):
        return {}
    rows = conn.execute("SELECT symbol, name FROM stocks").fetchall()
    return {normalize_symbol(str(symbol)): str(name or "") for symbol, name in rows}


def name_to_symbol(conn: sqlite3.Connection) -> dict[str, tuple[str, str]]:
    mapping: dict[str, tuple[str, str]] = {}
    for symbol, name in holding_names(conn).items():
        if name:
            mapping[name] = (symbol, name)
    if table_exists(conn, "portfolio_holdings"):
        for symbol, name, aliases_json in conn.execute(
            "SELECT symbol, name, aliases_json FROM portfolio_holdings WHERE enabled = 1"
        ).fetchall():
            normalized = normalize_symbol(str(symbol))
            if name:
                mapping[str(name)] = (normalized, str(name))
            aliases = list_from_json(str(aliases_json or "[]"))
            for alias in aliases:
                alias_text = str(alias).strip()
                if alias_text:
                    mapping[alias_text] = (normalized, str(name or alias_text))
    for symbol, name in stock_names(conn).items():
        if name and name not in mapping:
            mapping[name] = (symbol, name)
    return mapping


def normalize_a_share_code(code: str, suffix: str = "") -> str:
    raw = str(code or "").strip()
    if "." in raw:
        return normalize_symbol(raw)
    exchange = suffix.upper().strip(".")
    if not exchange:
        if raw.startswith(("0", "3")):
            exchange = "SZ"
        elif raw.startswith(("6", "9")):
            exchange = "SH"
        elif raw.startswith(("4", "8")):
            exchange = "BJ"
    return normalize_symbol(f"{raw}.{exchange}" if exchange else raw)


def target_from_text(
    conn: sqlite3.Connection,
    text: str,
    *,
    role: str = "affected_target",
    direction: str = "",
    reason: str = "",
    confidence: str = "",
) -> dict[str, Any] | None:
    value = str(text or "").strip()
    if not value:
        return None
    holdings = holding_names(conn)
    stocks = stock_names(conn)
    match = A_SHARE_CODE_RE.search(value)
    if match:
        symbol = normalize_a_share_code(match.group(1), match.group(2) or "")
        return target_from_symbol(
            symbol,
            name=holdings.get(symbol) or stocks.get(symbol, ""),
            role="holding" if symbol in holdings else role,
            direction=direction,
            relation_type="parsed_code",
            reason=reason or value,
            confidence=confidence,
        )
    for name, (symbol, official_name) in sorted(name_to_symbol(conn).items(), key=lambda item: len(item[0]), reverse=True):
        if name and name in value:
            return target_from_symbol(
                symbol,
                name=official_name,
                role="holding" if symbol in holdings else role,
                direction=direction,
                relation_type="matched_name",
                reason=reason or value,
                confidence=confidence,
            )
    cashtag = CASHTAG_RE.search(value)
    if cashtag:
        ticker = normalize_symbol(cashtag.group(1))
        return target_from_symbol(
            ticker,
            name=ticker,
            role="global_mapping",
            direction=direction,
            relation_type="cashtag",
            reason=reason or value,
            confidence=confidence,
        )
    return target_from_free_text(value, role=role, direction=direction, reason=reason, confidence=confidence)


def target_from_symbol(
    symbol: str,
    *,
    name: str = "",
    role: str = "direct",
    direction: str = "",
    relation_type: str = "",
    reason: str = "",
    confidence: str = "",
    horizon: str = "",
) -> dict[str, Any] | None:
    normalized = normalize_symbol(symbol)
    if not normalized:
        return None
    return {
        "symbol": normalized,
        "name": name,
        "market": symbol_market(normalized),
        "target_role": role,
        "expected_direction": normalize_direction(direction),
        "expected_horizon": horizon,
        "relation_type": relation_type,
        "relation_reason": reason,
        "confidence": confidence,
    }


def target_from_free_text(
    text: str,
    *,
    role: str = "industry_or_unknown",
    direction: str = "",
    reason: str = "",
    confidence: str = "",
) -> dict[str, Any] | None:
    value = str(text or "").strip()
    if not value:
        return None
    return {
        "name": value,
        "market": "行业环节",
        "target_role": role,
        "expected_direction": normalize_direction(direction),
        "relation_type": "LLM affected_target",
        "relation_reason": reason,
        "confidence": confidence,
    }


def dedupe_targets(targets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str]] = set()
    deduped: list[dict[str, Any]] = []
    for target in targets:
        key = (target_key(target), str(target.get("target_role") or ""))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(target)
    return deduped


def event_targets(
    conn: sqlite3.Connection,
    symbols_json: str | None,
    analysis: dict[str, Any],
) -> list[dict[str, Any]]:
    holdings = holding_names(conn)
    stocks = stock_names(conn)
    targets: list[dict[str, Any]] = []
    price_impact = analysis.get("price_impact") if isinstance(analysis.get("price_impact"), dict) else {}
    default_direction = str(price_impact.get("direction") or "")
    default_horizon = str(price_impact.get("duration") or "")
    for symbol in list_from_json(symbols_json):
        normalized = normalize_symbol(str(symbol))
        if not normalized:
            continue
        targets.append(
            target_from_symbol(
                normalized,
                name=holdings.get(normalized) or stocks.get(normalized, ""),
                role="holding" if normalized in holdings else "direct",
                direction=default_direction,
                relation_type="event_symbols",
                reason="事件 symbols_json 直接包含该标的。",
                horizon=default_horizon,
            )
        )

    related_holdings = analysis.get("related_holdings")
    if isinstance(related_holdings, list):
        for item in related_holdings:
            if not isinstance(item, dict):
                continue
            target = target_from_symbol(
                str(item.get("code") or item.get("symbol") or ""),
                name=str(item.get("name") or ""),
                role="holding",
                direction=str(item.get("impact_direction") or default_direction),
                relation_type=str(item.get("relation") or "LLM related_holding"),
                reason=str(item.get("reason") or ""),
                confidence=str(item.get("confidence") or ""),
                horizon=default_horizon,
            )
            if target:
                targets.append(target)

    for block_name, direction in (("positive", "positive"), ("negative", "negative")):
        for section in ("a_share", "global_equity"):
            section_data = analysis.get(section)
            if not isinstance(section_data, dict):
                continue
            rows = section_data.get(block_name)
            if not isinstance(rows, list):
                continue
            for item in rows:
                if not isinstance(item, dict):
                    continue
                code = str(item.get("code") or item.get("symbol") or "")
                if code:
                    target = target_from_symbol(
                        code,
                        name=str(item.get("name") or ""),
                        role="a_share_mapping" if section == "a_share" else "global_mapping",
                        direction=direction,
                        relation_type=section,
                        reason=str(item.get("reason") or ""),
                        confidence=str(item.get("confidence") or ""),
                        horizon=str(item.get("duration") or default_horizon),
                    )
                else:
                    target = target_from_free_text(
                        str(item.get("name") or item.get("full_name") or ""),
                        role="industry_or_unknown",
                        direction=direction,
                        reason=str(item.get("reason") or ""),
                        confidence=str(item.get("confidence") or ""),
                    )
                if target:
                    targets.append(target)
    return dedupe_targets([target for target in targets if target])


def event_signal_from_row(conn: sqlite3.Connection, row: sqlite3.Row) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]] | None:
    source = str(row["source"] or "")
    if source.lower() in SKIPPED_SOURCES:
        return None
    analysis = json_loads(str(row["analysis_json"] or "{}"), {})
    if not isinstance(analysis, dict):
        return None
    importance = normalize_importance(str(row["importance"] or analysis.get("importance") or ""))
    if not should_include(importance, bool(row["should_push"]) or bool(row["pushed_at"]), include_medium=True):
        return None
    incremental = analysis.get("incremental_view") if isinstance(analysis.get("incremental_view"), dict) else {}
    price = analysis.get("price_impact") if isinstance(analysis.get("price_impact"), dict) else {}
    signal = {
        "source_table": "events",
        "source_id": str(row["id"]),
        "source": source,
        "source_item_id": str(row["source_event_id"] or ""),
        "title": str(row["title"] or ""),
        "url": str(row["url"] or ""),
        "published_at": str(row["published_at"] or ""),
        "first_seen_at": str(row["first_seen_at"] or ""),
        "pushed_at": str(row["pushed_at"] or ""),
        "importance": importance,
        "incremental_classification": str(row["classification"] or incremental.get("classification") or ""),
        "direction": str(row["direction"] or price.get("direction") or ""),
        "confidence": scalar_from_path(analysis, "confidence"),
        "thesis": str(analysis.get("initial_impact") or analysis.get("core_content") or ""),
        "invalidation": "; ".join(str(item) for item in analysis.get("risks", [])[:3])
        if isinstance(analysis.get("risks"), list)
        else "",
        "model": str(row["model"] or analysis.get("_model") or ""),
        "prompt_version": PROMPT_VERSION,
        "raw": {
            "analysis": analysis,
            "event": {
                "event_type": row["event_type"],
                "summary": row["summary"],
                "full_text": row["full_text"],
                "themes": list_from_json(row["themes_json"]),
            },
        },
    }
    evidence = [
        {
            "evidence_type": "source",
            "text": str(row["summary"] or row["full_text"] or row["title"] or "")[:2000],
            "url": str(row["url"] or ""),
            "source": source,
            "observed_at": str(row["published_at"] or row["first_seen_at"] or ""),
        }
    ]
    tracking_points = analysis.get("tracking_points")
    if isinstance(tracking_points, list):
        for item in tracking_points[:5]:
            evidence.append(
                {
                    "evidence_type": "checkpoint",
                    "text": str(item),
                    "url": str(row["url"] or ""),
                    "source": source,
                    "observed_at": str(row["published_at"] or row["first_seen_at"] or ""),
                }
            )
    return signal, event_targets(conn, row["symbols_json"], analysis), evidence


def article_signal_from_row(conn: sqlite3.Connection, row: sqlite3.Row) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]] | None:
    source = str(row["source"] or "")
    if source.lower() in SKIPPED_SOURCES:
        return None
    importance = normalize_importance(str(row["importance"] or ""))
    pushed = bool(row["push_now"]) or bool(row["pushed_at"])
    if not should_include(importance, pushed, include_medium=False):
        return None
    gate = json_loads(str(row["gate_json"] or "{}"), {})
    if not isinstance(gate, dict):
        gate = {}
    targets: list[dict[str, Any]] = []
    affected = json_loads(str(row["affected_targets_json"] or "[]"), [])
    if isinstance(affected, list):
        for item in affected[:8]:
            targets.append(
                target_from_text(
                    conn,
                    str(item),
                    role="affected_target",
                    direction=str(row["incremental_classification"] or ""),
                    reason=str(row["market_impact"] or row["reason"] or ""),
                    confidence=str(row["confidence"] or ""),
                )
            )
    signal = {
        "source_table": "article_reviews",
        "source_id": f"{source}:{row['item_id']}",
        "source": source,
        "source_item_id": str(row["item_id"] or ""),
        "title": str(row["title"] or ""),
        "url": str(row["url"] or ""),
        "published_at": str(row["published_at"] or ""),
        "first_seen_at": str(row["created_at"] or ""),
        "pushed_at": str(row["pushed_at"] or ""),
        "importance": importance,
        "incremental_classification": str(row["incremental_classification"] or ""),
        "direction": str(row["incremental_classification"] or row["market_impact"] or ""),
        "confidence": str(row["confidence"] or ""),
        "thesis": str(row["market_impact"] or row["reason"] or row["daily_summary"] or ""),
        "invalidation": "",
        "model": str(gate.get("model") or ""),
        "prompt_version": PROMPT_VERSION,
        "raw": {"gate": gate, "source_module": row["source_module"]},
    }
    evidence = [
        {
            "evidence_type": "source",
            "text": "；".join(part for part in [str(row["daily_summary"] or ""), str(row["reason"] or "")] if part)[:2000],
            "url": str(row["url"] or ""),
            "source": source,
            "observed_at": str(row["published_at"] or row["created_at"] or ""),
        }
    ]
    return signal, dedupe_targets([target for target in targets if target]), evidence


def official_signal_from_row(conn: sqlite3.Connection, row: sqlite3.Row) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]] | None:
    source = str(row["source"] or "")
    importance = normalize_importance(str(row["importance"] or ""))
    pushed = bool(row["should_push_now"]) or bool(row["pushed_at"])
    if not should_include(importance, pushed, include_medium=False):
        return None
    analysis = json_loads(str(row["analysis_json"] or "{}"), {})
    if not isinstance(analysis, dict):
        analysis = {}
    incremental = analysis.get("incremental_view") if isinstance(analysis.get("incremental_view"), dict) else {}
    signal = {
        "source_table": "official_news_reviews",
        "source_id": f"{source}:{row['item_id']}",
        "source": source,
        "source_item_id": str(row["item_id"] or ""),
        "title": str(row["title"] or ""),
        "url": str(row["url"] or ""),
        "published_at": str(row["published_at"] or ""),
        "first_seen_at": str(row["created_at"] or ""),
        "pushed_at": str(row["pushed_at"] or ""),
        "importance": importance,
        "incremental_classification": str(incremental.get("classification") or ""),
        "direction": scalar_from_path(analysis, "price_impact", "direction")
        or str(incremental.get("classification") or ""),
        "confidence": str(analysis.get("confidence") or ""),
        "thesis": str(analysis.get("initial_impact") or analysis.get("core_content") or row["reason"] or ""),
        "invalidation": "; ".join(str(item) for item in analysis.get("risks", [])[:3])
        if isinstance(analysis.get("risks"), list)
        else "",
        "model": str(analysis.get("_model") or ""),
        "prompt_version": PROMPT_VERSION,
        "raw": {"analysis": analysis, "reason": row["reason"], "daily_summary": row["daily_summary"]},
    }
    targets = event_targets(conn, "[]", analysis)
    evidence = [
        {
            "evidence_type": "source",
            "text": "；".join(part for part in [str(row["daily_summary"] or ""), str(row["reason"] or "")] if part)[:2000],
            "url": str(row["url"] or ""),
            "source": source,
            "observed_at": str(row["published_at"] or row["created_at"] or ""),
        }
    ]
    return signal, targets, evidence


def x_targets(conn: sqlite3.Connection, text: str) -> list[dict[str, Any]]:
    targets: list[dict[str, Any]] = []
    for match in A_SHARE_CODE_RE.finditer(text):
        target = target_from_text(
            conn,
            match.group(0),
            role="x_mentioned_target",
            direction="uncertain",
            reason="X 帖子文本直接提到该代码。",
            confidence="低",
        )
        if target:
            targets.append(target)
    for name, (symbol, official_name) in name_to_symbol(conn).items():
        if name and name in text:
            targets.append(
                target_from_symbol(
                    symbol,
                    name=official_name,
                    role="holding",
                    direction="uncertain",
                    relation_type="matched_name",
                    reason="X 帖子文本匹配持仓名称或别名。",
                    confidence="低",
                )
            )
    for ticker in CASHTAG_RE.findall(text):
        target = target_from_symbol(
            ticker,
            name=ticker,
            role="global_mapping",
            direction="uncertain",
            relation_type="cashtag",
            reason="X 帖子文本包含 cashtag。",
            confidence="低",
        )
        if target:
            targets.append(target)
    return dedupe_targets([target for target in targets if target])


def x_signal_from_row(conn: sqlite3.Connection, row: sqlite3.Row) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]] | None:
    text = str(row["text"] or "").strip()
    targets = x_targets(conn, text)
    if not targets:
        return None
    source = str(row["source"] or "x")
    title = f"{source} X post {row['post_id']}"
    signal = {
        "source_table": "seen_posts",
        "source_id": f"{source}:{row['post_id']}",
        "source": source,
        "source_item_id": str(row["post_id"] or ""),
        "title": title,
        "url": str(row["url"] or ""),
        "published_at": str(row["published_at"] or ""),
        "first_seen_at": str(row["first_seen_at"] or ""),
        "pushed_at": str(row["delivered_at"] or row["first_seen_at"] or ""),
        "importance": "medium",
        "incremental_classification": "无法判断",
        "direction": "uncertain",
        "confidence": "低",
        "thesis": text[:500],
        "invalidation": "",
        "model": "",
        "prompt_version": PROMPT_VERSION,
        "raw": {"text": text, "delivery_status": row["delivery_status"]},
    }
    evidence = [
        {
            "evidence_type": "source",
            "text": text[:2000],
            "url": str(row["url"] or ""),
            "source": source,
            "observed_at": str(row["published_at"] or row["first_seen_at"] or ""),
        }
    ]
    return signal, targets, evidence


def latest_event_rows(conn: sqlite3.Connection, since: str) -> list[sqlite3.Row]:
    if not table_exists(conn, "events") or not table_exists(conn, "event_analyses"):
        return []
    return list(
        conn.execute(
            """
            WITH latest AS (
                SELECT event_id, MAX(id) AS analysis_id
                FROM event_analyses
                GROUP BY event_id
            ), pushed AS (
                SELECT event_id, MAX(sent_at) AS pushed_at
                FROM deliveries
                WHERE channel = 'feishu' AND status = 'sent'
                GROUP BY event_id
            )
            SELECT e.id, e.source, e.source_event_id, e.event_type, e.title, e.summary,
                   e.full_text, e.url, e.published_at, e.first_seen_at, e.symbols_json,
                   e.themes_json, a.model, a.importance, a.classification, a.direction,
                   a.impact_duration, a.should_push, a.analysis_json, a.created_at AS analysis_created_at,
                   COALESCE(p.pushed_at, '') AS pushed_at
            FROM events e
            JOIN latest l ON l.event_id = e.id
            JOIN event_analyses a ON a.id = l.analysis_id
            LEFT JOIN pushed p ON p.event_id = e.id
            WHERE COALESCE(e.published_at, e.first_seen_at, a.created_at) >= ?
               OR e.first_seen_at >= ?
            ORDER BY e.id
            """,
            (since, since),
        ).fetchall()
    )


def article_rows(conn: sqlite3.Connection, since: str) -> list[sqlite3.Row]:
    if not table_exists(conn, "article_reviews"):
        return []
    return list(
        conn.execute(
            """
            SELECT source, item_id, url, title, source_module, published_at,
                   importance, push_now, market_impact, incremental_classification,
                   affected_targets_json, reason, daily_summary, confidence,
                   gate_json, pushed_at, created_at
            FROM article_reviews
            WHERE COALESCE(published_at, created_at) >= ? OR created_at >= ?
            ORDER BY created_at
            """,
            (since, since),
        ).fetchall()
    )


def official_rows(conn: sqlite3.Connection, since: str) -> list[sqlite3.Row]:
    if not table_exists(conn, "official_news_reviews"):
        return []
    return list(
        conn.execute(
            """
            SELECT source, item_id, url, title, published_at, importance,
                   should_push_now, reason, daily_summary, analysis_json,
                   pushed_at, created_at
            FROM official_news_reviews
            WHERE COALESCE(published_at, created_at) >= ? OR created_at >= ?
            ORDER BY created_at
            """,
            (since, since),
        ).fetchall()
    )


def x_rows(conn: sqlite3.Connection, since: str) -> list[sqlite3.Row]:
    if not table_exists(conn, "seen_posts"):
        return []
    columns = {row[1] for row in conn.execute("PRAGMA table_info(seen_posts)").fetchall()}
    delivery_status = "delivery_status" if "delivery_status" in columns else "'' AS delivery_status"
    delivered_at = "delivered_at" if "delivered_at" in columns else "'' AS delivered_at"
    delivery_filter = "AND (delivery_status = 'sent' OR COALESCE(delivered_at, '') != '')" if "delivery_status" in columns else ""
    return list(
        conn.execute(
            f"""
            SELECT source, post_id, url, text, published_at, first_seen_at,
                   {delivery_status}, {delivered_at}
            FROM seen_posts
            WHERE (COALESCE(published_at, first_seen_at) >= ? OR first_seen_at >= ?)
              {delivery_filter}
            ORDER BY first_seen_at
            """,
            (since, since),
        ).fetchall()
    )


def extract_signals(*, db_path: Path, days: int, dry_run: bool = False) -> dict[str, int]:
    init_db(db_path).close()
    since = cutoff_iso(days)
    counts = {"events": 0, "article_reviews": 0, "official_news_reviews": 0, "signals": 0, "targets": 0}
    with connect_sqlite(db_path) as conn:
        conn.row_factory = sqlite3.Row
        candidates: list[tuple[str, tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]]] = []
        for row in latest_event_rows(conn, since):
            extracted = event_signal_from_row(conn, row)
            if extracted:
                counts["events"] += 1
                candidates.append(("events", extracted))
        for row in article_rows(conn, since):
            extracted = article_signal_from_row(conn, row)
            if extracted:
                counts["article_reviews"] += 1
                candidates.append(("article_reviews", extracted))
        for row in official_rows(conn, since):
            extracted = official_signal_from_row(conn, row)
            if extracted:
                counts["official_news_reviews"] += 1
                candidates.append(("official_news_reviews", extracted))
        for row in x_rows(conn, since):
            extracted = x_signal_from_row(conn, row)
            if extracted:
                counts.setdefault("seen_posts", 0)
                counts["seen_posts"] += 1
                candidates.append(("seen_posts", extracted))

        for source_table, (signal, targets, evidence) in candidates:
            if dry_run:
                print(
                    f"[dry-run] {source_table} {signal['source']} {signal['importance']} "
                    f"{signal['title'][:80]} targets={len(targets)}",
                    flush=True,
                )
                continue
            signal_id = upsert_signal(conn, signal, targets=targets, evidence=evidence)
            counts["signals"] += 1
            counts["targets"] += len([target for target in targets if target_key(target) != "unknown"])
            print(f"signal #{signal_id}: {signal['source']} {signal['title'][:80]}", flush=True)
    return counts


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Extract signal records from monitored events/reviews.")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="SQLite DB path.")
    parser.add_argument("--days", type=int, default=7, help="Lookback days. Default: 7.")
    parser.add_argument("--dry-run", action="store_true", help="Print candidate signals without writing.")
    return parser.parse_args()


def main() -> int:
    load_env()
    args = parse_args()
    counts = extract_signals(db_path=Path(args.db), days=args.days, dry_run=args.dry_run)
    print(json.dumps(counts, ensure_ascii=False, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
