#!/usr/bin/env python3
"""Backfill market outcomes for extracted investment signals."""

from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from db_utils import connect_sqlite
from env_utils import load_env
from ifind_client import IfindClient, IfindError, IfindNoDataError
from market_db import DEFAULT_DB_PATH, init_db
from signal_store import is_a_share_symbol, normalize_direction, normalize_symbol, upsert_outcome


DEFAULT_INDICATORS = "close,preClose,amount,volume"
HORIZONS = (1, 3, 5, 10, 20)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        parsed = float(str(value).replace(",", "").replace("%", "").strip())
    except ValueError:
        return None
    if math.isnan(parsed) or math.isinf(parsed):
        return None
    return parsed


def first_present(row: dict[str, Any], *keys: str) -> Any:
    lowered = {str(key).lower(): value for key, value in row.items()}
    for key in keys:
        if key in row and row[key] not in (None, ""):
            return row[key]
        lower = key.lower()
        if lower in lowered and lowered[lower] not in (None, ""):
            return lowered[lower]
    return None


def parse_dateish(value: Any) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    if "T" in text:
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
        except ValueError:
            pass
    for fmt, width in (
        ("%Y-%m-%d", 10),
        ("%Y%m%d", 8),
        ("%Y/%m/%d", 10),
        ("%Y-%m-%d %H:%M:%S", 19),
    ):
        try:
            return datetime.strptime(text[:width], fmt).date()
        except ValueError:
            continue
    return None


def published_date(value: str) -> date:
    parsed = parse_dateish(value)
    if parsed:
        return parsed
    return datetime.now(timezone.utc).date()


def normalize_history_rows(response: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for table in response.get("tables") or []:
        if not isinstance(table, dict):
            continue
        symbol = str(table.get("thscode") or table.get("code") or "").strip().upper()
        table_data = table.get("table") if isinstance(table.get("table"), dict) else {}
        row_count = max((len(value) for value in table_data.values() if isinstance(value, list)), default=0)
        for index in range(row_count):
            row: dict[str, Any] = {"symbol": symbol}
            for key, values in table_data.items():
                if isinstance(values, list) and index < len(values):
                    row[key] = values[index]
            rows.append(row)
    if rows:
        return rows

    for key in ("data", "list"):
        data = response.get(key)
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
    return []


def normalize_quote_row(row: dict[str, Any]) -> dict[str, Any] | None:
    trade_date = parse_dateish(first_present(row, "time", "date", "tradeDate", "tradedate", "日期", "交易日期"))
    close = safe_float(first_present(row, "close", "收盘价", "收盘", "latest", "最新价"))
    amount = safe_float(first_present(row, "amount", "成交额", "turnover"))
    volume = safe_float(first_present(row, "volume", "成交量"))
    if not trade_date or close is None:
        return None
    return {"date": trade_date, "close": close, "amount": amount, "volume": volume, "raw": row}


def quote_rows_from_response(response: dict[str, Any]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for row in normalize_history_rows(response):
        quote = normalize_quote_row(row)
        if quote:
            normalized.append(quote)
    return sorted(normalized, key=lambda item: item["date"])


def compute_return(base: float, close: float | None) -> float | None:
    if base == 0 or close is None:
        return None
    return round((close / base - 1.0) * 100.0, 4)


def direction_match(expected_direction: str, return_1d: float | None, return_3d: float | None) -> str:
    direction = normalize_direction(expected_direction)
    value = return_3d if return_3d is not None else return_1d
    if value is None or direction in {"uncertain", "neutral", ""}:
        return "unverifiable"
    if direction == "positive":
        return "matched" if value > 0 else "missed"
    if direction == "negative":
        return "matched" if value < 0 else "missed"
    return "unverifiable"


def compute_metrics(quotes: list[dict[str, Any]], expected_direction: str) -> tuple[dict[str, Any], dict[str, Any], str]:
    if not quotes:
        return {}, {"reason": "no_quote_rows"}, "quote_unavailable"
    base = float(quotes[0]["close"])
    metrics: dict[str, Any] = {}
    for horizon in HORIZONS:
        if len(quotes) > horizon:
            metrics[f"return_{horizon}d"] = compute_return(base, float(quotes[horizon]["close"]))
    observed = quotes[1:] if len(quotes) > 1 else quotes
    closes = [float(item["close"]) for item in observed if item.get("close") is not None]
    if closes and base:
        metrics["max_runup"] = round((max(closes) / base - 1.0) * 100.0, 4)
        metrics["max_drawdown"] = round((min(closes) / base - 1.0) * 100.0, 4)
    amounts = [safe_float(item.get("amount")) for item in quotes if item.get("amount") is not None]
    amounts = [item for item in amounts if item is not None]
    if len(amounts) >= 2 and amounts[0]:
        metrics["volume_change"] = round((amounts[-1] / amounts[0] - 1.0) * 100.0, 4)
    metrics["matched_direction"] = direction_match(
        expected_direction,
        metrics.get("return_1d"),
        metrics.get("return_3d"),
    )
    outcome_json = {
        "base_date": quotes[0]["date"].isoformat(),
        "base_close": base,
        "last_date": quotes[-1]["date"].isoformat(),
        "quote_count": len(quotes),
        "horizons": list(HORIZONS),
        "quotes": [
            {
                "date": item["date"].isoformat(),
                "close": item.get("close"),
                "amount": item.get("amount"),
                "volume": item.get("volume"),
            }
            for item in quotes[:25]
        ],
    }
    max_horizon_ready = max((h for h in HORIZONS if metrics.get(f"return_{h}d") is not None), default=0)
    status = "complete" if max_horizon_ready >= 20 else f"partial_{max_horizon_ready}d"
    return metrics, outcome_json, status


def fetch_quotes(client: IfindClient, symbol: str, start: date, end: date) -> list[dict[str, Any]]:
    indicators = os.getenv("SIGNAL_OUTCOME_IFIND_INDICATORS", DEFAULT_INDICATORS).strip() or DEFAULT_INDICATORS
    response = client.history_quotes(
        normalize_symbol(symbol),
        indicators,
        start.strftime("%Y-%m-%d"),
        end.strftime("%Y-%m-%d"),
    )
    return quote_rows_from_response(response)


def target_rows(conn: sqlite3.Connection, days: int, limit: int | None) -> list[sqlite3.Row]:
    since = (datetime.now(timezone.utc) - timedelta(days=max(1, days))).isoformat()
    sql = """
        SELECT s.id AS signal_id, s.title, s.published_at, s.direction AS signal_direction,
               t.id AS target_id, t.symbol, t.expected_direction
        FROM signals s
        JOIN signal_targets t ON t.signal_id = s.id
        WHERE s.created_at >= ?
          AND COALESCE(t.symbol, '') != ''
        ORDER BY s.created_at DESC, s.id DESC
    """
    params: list[Any] = [since]
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    return list(conn.execute(sql, params).fetchall())


def update_outcomes(*, db_path: Path, days: int, limit: int | None = None, dry_run: bool = False) -> dict[str, int]:
    load_env()
    init_db(db_path).close()
    counts = {"targets": 0, "updated": 0, "skipped": 0, "failed": 0}
    client: IfindClient | None = None
    if not dry_run:
        try:
            client = IfindClient.from_env()
        except IfindError as exc:
            print(f"iFinD 未配置，无法回填行情：{exc}", flush=True)
            client = None
    with connect_sqlite(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = target_rows(conn, days, limit)
        counts["targets"] = len(rows)
        for row in rows:
            symbol = normalize_symbol(str(row["symbol"] or ""))
            if not is_a_share_symbol(symbol):
                counts["skipped"] += 1
                if not dry_run:
                    upsert_outcome(
                        conn,
                        signal_id=int(row["signal_id"]),
                        target_id=int(row["target_id"]),
                        symbol=symbol,
                        as_of_date=datetime.now(timezone.utc).date().isoformat(),
                        outcome_status="unsupported_market",
                        outcome_json={"reason": "MVP only backfills A-share symbols"},
                    )
                continue
            start = published_date(str(row["published_at"] or "")) - timedelta(days=3)
            end = datetime.now(timezone.utc).date()
            if dry_run:
                print(f"[dry-run] {symbol} signal={row['signal_id']} {start}..{end}", flush=True)
                continue
            if client is None:
                counts["skipped"] += 1
                upsert_outcome(
                    conn,
                    signal_id=int(row["signal_id"]),
                    target_id=int(row["target_id"]),
                    symbol=symbol,
                    as_of_date=end.isoformat(),
                    outcome_status="ifind_not_configured",
                    outcome_json={"reason": "missing IFIND_REFRESH_TOKEN or IFIND_ACCESS_TOKEN"},
                )
                continue
            try:
                quotes = fetch_quotes(client, symbol, start, end)
            except IfindNoDataError as exc:
                counts["failed"] += 1
                upsert_outcome(
                    conn,
                    signal_id=int(row["signal_id"]),
                    target_id=int(row["target_id"]),
                    symbol=symbol,
                    as_of_date=end.isoformat(),
                    outcome_status="quote_no_data",
                    outcome_json={"error": str(exc)[:1000]},
                )
                continue
            except Exception as exc:  # noqa: BLE001 - isolate provider failures per signal
                counts["failed"] += 1
                upsert_outcome(
                    conn,
                    signal_id=int(row["signal_id"]),
                    target_id=int(row["target_id"]),
                    symbol=symbol,
                    as_of_date=end.isoformat(),
                    outcome_status="quote_error",
                    outcome_json={"error": str(exc)[:1000]},
                )
                continue
            event_date = published_date(str(row["published_at"] or ""))
            quotes = [item for item in quotes if item["date"] >= event_date]
            expected = str(row["expected_direction"] or row["signal_direction"] or "")
            metrics, outcome_json, status = compute_metrics(quotes, expected)
            upsert_outcome(
                conn,
                signal_id=int(row["signal_id"]),
                target_id=int(row["target_id"]),
                symbol=symbol,
                as_of_date=end.isoformat(),
                outcome_status=status,
                metrics=metrics,
                outcome_json=outcome_json,
            )
            counts["updated"] += 1
            print(f"outcome {symbol} signal={row['signal_id']} status={status}", flush=True)
    return counts


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backfill signal market outcomes with iFinD history quotes.")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="SQLite DB path.")
    parser.add_argument("--days", type=int, default=30, help="Lookback signal creation days. Default: 30.")
    parser.add_argument("--limit", type=int, default=None, help="Limit target rows.")
    parser.add_argument("--dry-run", action="store_true", help="Print work without calling iFinD or writing outcomes.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    counts = update_outcomes(db_path=Path(args.db), days=args.days, limit=args.limit, dry_run=args.dry_run)
    print(json.dumps(counts, ensure_ascii=False, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
