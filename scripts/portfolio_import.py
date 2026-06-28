#!/usr/bin/env python3
"""Import configured holdings into the unified SQLite database."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from db_utils import connect_sqlite
from env_utils import load_env
from market_db import DEFAULT_DB_PATH, init_db


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = ROOT / "config" / "portfolio.json"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_portfolio(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(f"持仓配置不存在：{path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    holdings = data.get("holdings")
    if not isinstance(holdings, list):
        raise ValueError("持仓配置缺少 holdings 数组")
    normalized: list[dict[str, Any]] = []
    for item in holdings:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol") or "").strip().upper()
        name = str(item.get("name") or "").strip()
        if not symbol and not name:
            continue
        aliases = item.get("aliases") if isinstance(item.get("aliases"), list) else []
        normalized.append(
            {
                "symbol": symbol,
                "name": name or symbol,
                "full_name": str(item.get("full_name") or "").strip(),
                "aliases": [str(alias).strip() for alias in aliases if str(alias).strip()],
                "enabled": 1 if item.get("enabled", True) else 0,
                "raw": item,
            }
        )
    return normalized


def import_holdings(config_path: Path = DEFAULT_CONFIG_PATH, db_path: Path = DEFAULT_DB_PATH) -> int:
    init_db(db_path).close()
    holdings = load_portfolio(config_path)
    now = utc_now()
    active_symbols = [item["symbol"] for item in holdings if item["symbol"]]
    with connect_sqlite(db_path) as conn:
        for item in holdings:
            if not item["symbol"]:
                continue
            conn.execute(
                """
                INSERT INTO portfolio_holdings
                    (symbol, name, full_name, aliases_json, enabled, raw_json, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol) DO UPDATE SET
                    name = excluded.name,
                    full_name = excluded.full_name,
                    aliases_json = excluded.aliases_json,
                    enabled = excluded.enabled,
                    raw_json = excluded.raw_json,
                    updated_at = excluded.updated_at
                """,
                (
                    item["symbol"],
                    item["name"],
                    item["full_name"],
                    json.dumps(item["aliases"], ensure_ascii=False),
                    item["enabled"],
                    json.dumps(item["raw"], ensure_ascii=False, sort_keys=True),
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO stocks (symbol, name, full_name, exchange, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(symbol) DO UPDATE SET
                    name = excluded.name,
                    full_name = COALESCE(NULLIF(excluded.full_name, ''), stocks.full_name),
                    exchange = COALESCE(NULLIF(excluded.exchange, ''), stocks.exchange),
                    updated_at = excluded.updated_at
                """,
                (
                    item["symbol"],
                    item["name"],
                    item["full_name"],
                    exchange_from_symbol(item["symbol"]),
                    now,
                ),
            )
        if active_symbols:
            placeholders = ",".join("?" for _ in active_symbols)
            conn.execute(
                f"""
                UPDATE portfolio_holdings
                SET enabled = 0, updated_at = ?
                WHERE symbol NOT IN ({placeholders})
                """,
                (now, *active_symbols),
            )
        else:
            conn.execute("UPDATE portfolio_holdings SET enabled = 0, updated_at = ?", (now,))
        conn.commit()
    return len(holdings)


def exchange_from_symbol(symbol: str) -> str:
    if symbol.startswith("HK") or symbol.endswith(".HK"):
        return "港交所"
    if symbol.endswith(".SH"):
        return "上交所"
    if symbol.endswith(".SZ"):
        return "深交所"
    if symbol.endswith(".BJ"):
        return "北交所"
    return ""


def main() -> int:
    parser = argparse.ArgumentParser(description="导入持仓配置到 SQLite")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    args = parser.parse_args()

    load_env(ROOT / ".env")
    count = import_holdings(Path(args.config), Path(args.db))
    print(f"已导入持仓 {count} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
