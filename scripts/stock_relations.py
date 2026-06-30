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
        "relation_strength": str(item.get("relation_strength") or item.get("strength") or ""),
        "valid_from": str(item.get("valid_from") or ""),
        "valid_to": str(item.get("valid_to") or ""),
        "last_review_verdict": str(item.get("last_review_verdict") or ""),
        "hit_count": int(item.get("hit_count") or 0),
        "miss_count": int(item.get("miss_count") or 0),
        "source": str(item.get("source") or "config"),
        "enabled": 1 if item.get("enabled", True) is not False else 0,
        "raw_json": json_dumps(item),
    }


def row_value(row: sqlite3.Row | dict[str, Any], key: str, default: Any = "") -> Any:
    if isinstance(row, dict):
        return row.get(key, default)
    try:
        return row[key]
    except (IndexError, KeyError):
        return default


def relation_json_item(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    """Convert a DB row into the private JSON snapshot shape."""
    item = {
        "symbol": row_value(row, "symbol") or "",
        "symbol_name": row_value(row, "symbol_name") or "",
        "related_symbol": row_value(row, "related_symbol") or "",
        "related_name": row_value(row, "related_name") or "",
        "relation_type": row_value(row, "relation_type") or "",
        "impact_direction": row_value(row, "impact_direction") or "",
        "theme": row_value(row, "theme") or "",
        "reason": row_value(row, "reason") or "",
        "confidence": row_value(row, "confidence") or "",
        "relation_strength": row_value(row, "relation_strength") or "",
        "valid_from": row_value(row, "valid_from") or "",
        "valid_to": row_value(row, "valid_to") or "",
        "last_review_verdict": row_value(row, "last_review_verdict") or "",
        "hit_count": int(row_value(row, "hit_count", 0) or 0),
        "miss_count": int(row_value(row, "miss_count", 0) or 0),
        "source": row_value(row, "source") or "",
        "enabled": bool(row_value(row, "enabled", True)),
    }
    return {key: value for key, value in item.items() if value not in ("", None)}


def relation_response_item(row: sqlite3.Row) -> dict[str, Any]:
    item = relation_json_item(row)
    item["id"] = row["id"]
    item["enabled"] = bool(row["enabled"])
    item["updated_at"] = row["updated_at"] or ""
    return item


def relation_identity(item: dict[str, Any]) -> tuple[str, str, str]:
    normalized = normalize_relation_item(item)
    return normalized["symbol"], normalized["related_symbol"], normalized["relation_type"]


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
                    impact_direction, theme, reason, confidence, relation_strength,
                    valid_from, valid_to, last_review_verdict, hit_count, miss_count,
                    source, enabled,
                    raw_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, related_symbol, relation_type) DO UPDATE SET
                    symbol_name = excluded.symbol_name,
                    related_name = excluded.related_name,
                    impact_direction = excluded.impact_direction,
                    theme = excluded.theme,
                    reason = excluded.reason,
                    confidence = excluded.confidence,
                    relation_strength = excluded.relation_strength,
                    valid_from = excluded.valid_from,
                    valid_to = excluded.valid_to,
                    last_review_verdict = excluded.last_review_verdict,
                    hit_count = excluded.hit_count,
                    miss_count = excluded.miss_count,
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
                    item["relation_strength"],
                    item["valid_from"],
                    item["valid_to"],
                    item["last_review_verdict"],
                    item["hit_count"],
                    item["miss_count"],
                    item["source"],
                    item["enabled"],
                    item["raw_json"],
                    now,
                ),
            )
            counts["imported"] += 1
        conn.commit()
    return counts


def list_relations(
    *,
    db_path: Path = DEFAULT_DB_PATH,
    q: str = "",
    enabled: str = "all",
    limit: int = 300,
) -> list[dict[str, Any]]:
    init_db(db_path).close()
    q_lower = q.strip().lower()
    rows: list[dict[str, Any]] = []
    enabled_clause = ""
    params: list[Any] = []
    if enabled == "enabled":
        enabled_clause = "WHERE enabled = 1"
    elif enabled == "disabled":
        enabled_clause = "WHERE enabled = 0"
    with connect_sqlite(db_path) as conn:
        conn.row_factory = sqlite3.Row
        for row in conn.execute(
            f"""
            SELECT id, symbol, symbol_name, related_symbol, related_name, relation_type,
                   impact_direction, theme, reason, confidence, relation_strength,
                   valid_from, valid_to, last_review_verdict, hit_count, miss_count,
                   source, enabled, updated_at
            FROM stock_relations
            {enabled_clause}
            ORDER BY enabled DESC, updated_at DESC, id DESC
            LIMIT 10000
            """,
            params,
        ):
            item = relation_response_item(row)
            if q_lower and q_lower not in json.dumps(item, ensure_ascii=False).lower():
                continue
            rows.append(item)
            if len(rows) >= max(1, min(limit, 10000)):
                break
    return rows


def save_relation(
    item: dict[str, Any],
    *,
    db_path: Path = DEFAULT_DB_PATH,
    relation_id: int | None = None,
) -> dict[str, Any]:
    init_db(db_path).close()
    normalized = normalize_relation_item(item)
    now = utc_now()
    with connect_sqlite(db_path) as conn:
        conn.row_factory = sqlite3.Row
        if relation_id:
            cur = conn.execute(
                """
                UPDATE stock_relations
                SET symbol = ?, symbol_name = ?, related_symbol = ?, related_name = ?,
                    relation_type = ?, impact_direction = ?, theme = ?, reason = ?,
                    confidence = ?, relation_strength = ?, valid_from = ?, valid_to = ?,
                    last_review_verdict = ?, hit_count = ?, miss_count = ?, source = ?,
                    enabled = ?, raw_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    normalized["symbol"],
                    normalized["symbol_name"],
                    normalized["related_symbol"],
                    normalized["related_name"],
                    normalized["relation_type"],
                    normalized["impact_direction"],
                    normalized["theme"],
                    normalized["reason"],
                    normalized["confidence"],
                    normalized["relation_strength"],
                    normalized["valid_from"],
                    normalized["valid_to"],
                    normalized["last_review_verdict"],
                    normalized["hit_count"],
                    normalized["miss_count"],
                    normalized["source"],
                    normalized["enabled"],
                    normalized["raw_json"],
                    now,
                    relation_id,
                ),
            )
            if cur.rowcount == 0:
                raise ValueError(f"relation id {relation_id} not found")
            row_id = relation_id
        else:
            conn.execute(
                """
                INSERT INTO stock_relations (
                    symbol, symbol_name, related_symbol, related_name, relation_type,
                    impact_direction, theme, reason, confidence, relation_strength,
                    valid_from, valid_to, last_review_verdict, hit_count, miss_count,
                    source, enabled, raw_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, related_symbol, relation_type) DO UPDATE SET
                    symbol_name = excluded.symbol_name,
                    related_name = excluded.related_name,
                    impact_direction = excluded.impact_direction,
                    theme = excluded.theme,
                    reason = excluded.reason,
                    confidence = excluded.confidence,
                    relation_strength = excluded.relation_strength,
                    valid_from = excluded.valid_from,
                    valid_to = excluded.valid_to,
                    last_review_verdict = excluded.last_review_verdict,
                    hit_count = excluded.hit_count,
                    miss_count = excluded.miss_count,
                    source = excluded.source,
                    enabled = excluded.enabled,
                    raw_json = excluded.raw_json,
                    updated_at = excluded.updated_at
                """,
                (
                    normalized["symbol"],
                    normalized["symbol_name"],
                    normalized["related_symbol"],
                    normalized["related_name"],
                    normalized["relation_type"],
                    normalized["impact_direction"],
                    normalized["theme"],
                    normalized["reason"],
                    normalized["confidence"],
                    normalized["relation_strength"],
                    normalized["valid_from"],
                    normalized["valid_to"],
                    normalized["last_review_verdict"],
                    normalized["hit_count"],
                    normalized["miss_count"],
                    normalized["source"],
                    normalized["enabled"],
                    normalized["raw_json"],
                    now,
                ),
            )
            row = conn.execute(
                """
                SELECT id FROM stock_relations
                WHERE symbol = ? AND related_symbol = ? AND relation_type = ?
                """,
                (normalized["symbol"], normalized["related_symbol"], normalized["relation_type"]),
            ).fetchone()
            row_id = int(row["id"])
        conn.commit()
        row = conn.execute(
            """
            SELECT id, symbol, symbol_name, related_symbol, related_name, relation_type,
                   impact_direction, theme, reason, confidence, relation_strength,
                   valid_from, valid_to, last_review_verdict, hit_count, miss_count,
                   source, enabled, updated_at
            FROM stock_relations WHERE id = ?
            """,
            (row_id,),
        ).fetchone()
        return relation_response_item(row)


def delete_relation(*, relation_id: int, db_path: Path = DEFAULT_DB_PATH) -> bool:
    init_db(db_path).close()
    with connect_sqlite(db_path) as conn:
        cur = conn.execute("DELETE FROM stock_relations WHERE id = ?", (relation_id,))
        conn.commit()
        return cur.rowcount > 0


def set_relation_enabled(*, relation_id: int, enabled: bool, db_path: Path = DEFAULT_DB_PATH) -> dict[str, Any]:
    init_db(db_path).close()
    now = utc_now()
    with connect_sqlite(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            "UPDATE stock_relations SET enabled = ?, updated_at = ? WHERE id = ?",
            (1 if enabled else 0, now, relation_id),
        )
        if cur.rowcount == 0:
            raise ValueError(f"relation id {relation_id} not found")
        conn.commit()
        row = conn.execute(
            """
            SELECT id, symbol, symbol_name, related_symbol, related_name, relation_type,
                   impact_direction, theme, reason, confidence, relation_strength,
                   valid_from, valid_to, last_review_verdict, hit_count, miss_count,
                   source, enabled, updated_at
            FROM stock_relations WHERE id = ?
            """,
            (relation_id,),
        ).fetchone()
        return relation_response_item(row)


def export_relations(*, db_path: Path = DEFAULT_DB_PATH, config_path: Path = DEFAULT_CONFIG_PATH) -> dict[str, Any]:
    init_db(db_path).close()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    relations = list_relations(db_path=db_path, q="", enabled="all", limit=10000)
    payload = {
        "updated_at": utc_now(),
        "relations": [relation_json_item(row) for row in relations],
    }
    tmp_path = config_path.with_suffix(config_path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp_path.replace(config_path)
    return {"path": str(config_path), "count": len(payload["relations"])}


def diff_relations(*, db_path: Path = DEFAULT_DB_PATH, config_path: Path = DEFAULT_CONFIG_PATH) -> dict[str, Any]:
    init_db(db_path).close()
    db_relations = {relation_identity(item): relation_json_item(item) for item in list_relations(db_path=db_path, limit=10000)}
    payload = load_json(config_path)
    json_rows = payload.get("relations") or []
    json_relations: dict[tuple[str, str, str], dict[str, Any]] = {}
    invalid = 0
    for raw in json_rows:
        if not isinstance(raw, dict):
            invalid += 1
            continue
        try:
            normalized = normalize_relation_item(raw)
        except ValueError:
            invalid += 1
            continue
        json_relations[relation_identity(normalized)] = relation_json_item(normalized)
    added_in_db = [db_relations[key] for key in db_relations.keys() - json_relations.keys()]
    only_in_json = [json_relations[key] for key in json_relations.keys() - db_relations.keys()]
    changed = []
    for key in db_relations.keys() & json_relations.keys():
        if json_dumps(db_relations[key]) != json_dumps(json_relations[key]):
            changed.append({"db": db_relations[key], "json": json_relations[key]})
    return {
        "config_path": str(config_path),
        "db_count": len(db_relations),
        "json_count": len(json_relations),
        "invalid_json_rows": invalid,
        "only_in_db": added_in_db[:100],
        "only_in_json": only_in_json[:100],
        "changed": changed[:100],
    }


def create_relation_suggestion(
    item: dict[str, Any],
    *,
    db_path: Path = DEFAULT_DB_PATH,
    source_table: str = "",
    source_id: str = "",
) -> dict[str, Any]:
    init_db(db_path).close()
    normalized = normalize_relation_item(item)
    now = utc_now()
    with connect_sqlite(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            """
            INSERT INTO relation_suggestions (
                source_table, source_id, symbol, symbol_name, related_symbol, related_name,
                relation_type, impact_direction, theme, reason, confidence, source,
                status, raw_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
            """,
            (
                source_table,
                source_id,
                normalized["symbol"],
                normalized["symbol_name"],
                normalized["related_symbol"],
                normalized["related_name"],
                normalized["relation_type"],
                normalized["impact_direction"],
                normalized["theme"],
                normalized["reason"],
                normalized["confidence"],
                normalized["source"],
                normalized["raw_json"],
                now,
                now,
            ),
        )
        conn.commit()
        return relation_suggestion_item(conn.execute("SELECT * FROM relation_suggestions WHERE id = ?", (cur.lastrowid,)).fetchone())


def relation_suggestion_item(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "source_table": row["source_table"] or "",
        "source_id": row["source_id"] or "",
        "symbol": row["symbol"] or "",
        "symbol_name": row["symbol_name"] or "",
        "related_symbol": row["related_symbol"] or "",
        "related_name": row["related_name"] or "",
        "relation_type": row["relation_type"] or "",
        "impact_direction": row["impact_direction"] or "",
        "theme": row["theme"] or "",
        "reason": row["reason"] or "",
        "confidence": row["confidence"] or "",
        "source": row["source"] or "",
        "status": row["status"] or "",
        "created_at": row["created_at"] or "",
        "updated_at": row["updated_at"] or "",
        "reviewed_at": row["reviewed_at"] or "",
    }


def list_relation_suggestions(
    *,
    db_path: Path = DEFAULT_DB_PATH,
    status: str = "pending",
    limit: int = 100,
) -> list[dict[str, Any]]:
    init_db(db_path).close()
    status = status.strip().lower()
    clause = ""
    params: list[Any] = []
    if status and status != "all":
        clause = "WHERE status = ?"
        params.append(status)
    with connect_sqlite(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return [
            relation_suggestion_item(row)
            for row in conn.execute(
                f"""
                SELECT * FROM relation_suggestions
                {clause}
                ORDER BY updated_at DESC, id DESC
                LIMIT ?
                """,
                (*params, max(1, min(limit, 500))),
            )
        ]


def accept_relation_suggestion(*, suggestion_id: int, db_path: Path = DEFAULT_DB_PATH) -> dict[str, Any]:
    init_db(db_path).close()
    now = utc_now()
    with connect_sqlite(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM relation_suggestions WHERE id = ?", (suggestion_id,)).fetchone()
        if not row:
            raise ValueError(f"suggestion id {suggestion_id} not found")
        relation = save_relation(dict(relation_suggestion_item(row), enabled=True), db_path=db_path)
        conn.execute(
            "UPDATE relation_suggestions SET status = 'accepted', reviewed_at = ?, updated_at = ? WHERE id = ?",
            (now, now, suggestion_id),
        )
        conn.commit()
    return relation


def reject_relation_suggestion(*, suggestion_id: int, db_path: Path = DEFAULT_DB_PATH) -> bool:
    init_db(db_path).close()
    now = utc_now()
    with connect_sqlite(db_path) as conn:
        cur = conn.execute(
            "UPDATE relation_suggestions SET status = 'rejected', reviewed_at = ?, updated_at = ? WHERE id = ?",
            (now, now, suggestion_id),
        )
        conn.commit()
        return cur.rowcount > 0


def related_targets_for_symbols(
    conn: sqlite3.Connection,
    symbols: list[str],
    *,
    max_per_symbol: int = 5,
) -> list[dict[str, Any]]:
    return related_targets_for_context(conn, trigger_values=symbols, max_per_trigger=max_per_symbol)


def related_targets_for_context(
    conn: sqlite3.Connection,
    *,
    trigger_values: list[str],
    context_text: str = "",
    max_per_trigger: int = 5,
) -> list[dict[str, Any]]:
    exact_triggers = {normalize_symbol(value) for value in trigger_values if normalize_symbol(str(value))}
    context = str(context_text or "").lower()
    if not exact_triggers and not context:
        return []
    rows: list[sqlite3.Row] = []
    conn.row_factory = sqlite3.Row
    candidates = conn.execute(
        """
        SELECT symbol, symbol_name, related_symbol, related_name, relation_type,
               impact_direction, theme, reason, confidence, source
        FROM stock_relations
        WHERE enabled = 1
          AND (COALESCE(valid_to, '') = '' OR valid_to >= DATE('now'))
        ORDER BY
          CASE confidence WHEN '高' THEN 0 WHEN 'high' THEN 0 WHEN '中' THEN 1 WHEN 'medium' THEN 1 ELSE 2 END,
          updated_at DESC
        LIMIT 2000
        """
    ).fetchall()
    per_trigger_count: dict[str, int] = {}
    for row in candidates:
        symbol = normalize_symbol(str(row["symbol"] or ""))
        symbol_name = str(row["symbol_name"] or "").strip()
        theme = str(row["theme"] or "").strip()
        matched = symbol in exact_triggers or normalize_symbol(symbol_name) in exact_triggers or normalize_symbol(theme) in exact_triggers
        if not matched and context:
            probes = [str(row["symbol"] or "").strip(), symbol_name, theme]
            matched = any(len(probe) >= 3 and probe.lower() in context for probe in probes if probe)
        if not matched:
            continue
        if per_trigger_count.get(symbol, 0) >= max(1, max_per_trigger):
            continue
        per_trigger_count[symbol] = per_trigger_count.get(symbol, 0) + 1
        rows.append(row)
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
