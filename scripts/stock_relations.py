#!/usr/bin/env python3
"""Import and query lightweight stock/industry relation mappings."""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from db_utils import connect_sqlite
from market_db import DEFAULT_DB_PATH, init_db
from signal_store import json_dumps, normalize_direction, normalize_symbol, symbol_market


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = ROOT / "config" / "stock_relations.json"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"relations": []}
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        return data
    if isinstance(data, list):
        return {"relations": data}
    raise ValueError(f"{path} must be a JSON object or array")


def normalize_relation_item(item: dict[str, Any]) -> dict[str, Any]:
    symbol = normalize_symbol(str(item.get("symbol") or item.get("from_symbol") or item.get("trigger_symbol") or ""))
    related_symbol = normalize_symbol(
        str(item.get("related_symbol") or item.get("to_symbol") or item.get("affected_symbol") or "")
    )
    if not symbol or not related_symbol:
        raise ValueError("relation item requires symbol and related_symbol")
    return {
        "symbol": symbol,
        "symbol_name": str(item.get("symbol_name") or item.get("from_name") or item.get("trigger_name") or ""),
        "related_symbol": related_symbol,
        "related_name": str(item.get("related_name") or item.get("to_name") or item.get("affected_name") or ""),
        "relation_type": str(item.get("relation_type") or item.get("relation") or "related"),
        "impact_direction": normalize_direction(str(item.get("impact_direction") or item.get("direction") or "uncertain")),
        "theme": str(item.get("theme") or ""),
        "reason": str(item.get("reason") or ""),
        "confidence": str(item.get("confidence") or ""),
        "source": str(item.get("source") or "config"),
        "enabled": 1 if item.get("enabled", True) is not False else 0,
        "raw_json": json_dumps(item),
    }


def import_relations(*, db_path: Path, config_path: Path) -> dict[str, int]:
    init_db(db_path).close()
    payload = load_json(config_path)
    rows = payload.get("relations") or []
    if not isinstance(rows, list):
        raise ValueError("relations must be an array")
    counts = {"read": 0, "imported": 0, "skipped": 0}
    now = utc_now()
    with connect_sqlite(db_path) as conn:
        for raw in rows:
            counts["read"] += 1
            if not isinstance(raw, dict):
                counts["skipped"] += 1
                continue
            try:
                item = normalize_relation_item(raw)
            except ValueError:
                counts["skipped"] += 1
                continue
            conn.execute(
                """
                INSERT INTO stock_relations (
                    symbol, symbol_name, related_symbol, related_name, relation_type,
                    impact_direction, theme, reason, confidence, source, enabled,
                    raw_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, related_symbol, relation_type) DO UPDATE SET
                    symbol_name = excluded.symbol_name,
                    related_name = excluded.related_name,
                    impact_direction = excluded.impact_direction,
                    theme = excluded.theme,
                    reason = excluded.reason,
                    confidence = excluded.confidence,
                    source = excluded.source,
                    enabled = excluded.enabled,
                    raw_json = excluded.raw_json,
                    updated_at = excluded.updated_at
                """,
                (
                    item["symbol"],
                    item["symbol_name"],
                    item["related_symbol"],
                    item["related_name"],
                    item["relation_type"],
                    item["impact_direction"],
                    item["theme"],
                    item["reason"],
                    item["confidence"],
                    item["source"],
                    item["enabled"],
                    item["raw_json"],
                    now,
                ),
            )
            counts["imported"] += 1
        conn.commit()
    return counts


def related_targets_for_symbols(
    conn: sqlite3.Connection,
    symbols: list[str],
    *,
    max_per_symbol: int = 5,
) -> list[dict[str, Any]]:
    normalized_symbols = [normalize_symbol(symbol) for symbol in symbols if normalize_symbol(symbol)]
    if not normalized_symbols:
        return []
    rows: list[sqlite3.Row] = []
    conn.row_factory = sqlite3.Row
    for symbol in normalized_symbols:
        rows.extend(
            conn.execute(
                """
                SELECT symbol, symbol_name, related_symbol, related_name, relation_type,
                       impact_direction, theme, reason, confidence, source
                FROM stock_relations
                WHERE enabled = 1 AND symbol = ?
                ORDER BY
                  CASE confidence WHEN '高' THEN 0 WHEN 'high' THEN 0 WHEN '中' THEN 1 WHEN 'medium' THEN 1 ELSE 2 END,
                  updated_at DESC
                LIMIT ?
                """,
                (symbol, max(1, max_per_symbol)),
            ).fetchall()
        )
    targets: list[dict[str, Any]] = []
    for row in rows:
        related_symbol = normalize_symbol(str(row["related_symbol"] or ""))
        if not related_symbol:
            continue
        targets.append(
            {
                "symbol": related_symbol,
                "name": row["related_name"] or related_symbol,
                "market": symbol_market(related_symbol),
                "target_role": "related_stock",
                "expected_direction": normalize_direction(str(row["impact_direction"] or "")),
                "relation_type": row["relation_type"] or "related",
                "relation_reason": row["reason"] or f"由 {row['symbol']} 的关联关系映射。",
                "confidence": row["confidence"] or "",
                "theme": row["theme"] or "",
                "source_relation_symbol": row["symbol"],
                "source": row["source"] or "",
            }
        )
    return targets


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import stock relation mappings into SQLite.")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="SQLite DB path.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="Relation JSON config path.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    counts = import_relations(db_path=Path(args.db), config_path=Path(args.config))
    print(json.dumps(counts, ensure_ascii=False, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
