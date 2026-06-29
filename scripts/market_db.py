"""SQLite schema for the unified market monitor."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from db_utils import connect_sqlite


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = ROOT / "data" / "surveil.sqlite3"


SCHEMA = """
CREATE TABLE IF NOT EXISTS source_state (
    source TEXT PRIMARY KEY,
    state_json TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source_health (
    monitor TEXT NOT NULL,
    source TEXT NOT NULL,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_success_at TEXT,
    last_failure_at TEXT,
    last_error TEXT,
    last_alerted_at TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (monitor, source)
);

CREATE TABLE IF NOT EXISTS stocks (
    symbol TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    full_name TEXT,
    exchange TEXT,
    industry TEXT,
    concepts_json TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS portfolio_holdings (
    symbol TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    full_name TEXT,
    aliases_json TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    raw_json TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    source_event_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    title TEXT NOT NULL,
    summary TEXT,
    full_text TEXT,
    url TEXT,
    published_at TEXT,
    first_seen_at TEXT NOT NULL,
    symbols_json TEXT,
    themes_json TEXT,
    raw_json TEXT,
    content_hash TEXT NOT NULL,
    baseline_only INTEGER NOT NULL DEFAULT 0,
    UNIQUE(source, source_event_id)
);

CREATE INDEX IF NOT EXISTS idx_events_seen ON events(first_seen_at);
CREATE INDEX IF NOT EXISTS idx_events_source_type ON events(source, event_type);

CREATE TABLE IF NOT EXISTS event_analyses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER NOT NULL,
    task TEXT NOT NULL,
    model TEXT,
    importance TEXT,
    classification TEXT,
    direction TEXT,
    impact_duration TEXT,
    should_push INTEGER NOT NULL DEFAULT 0,
    analysis_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(event_id) REFERENCES events(id)
);

CREATE TABLE IF NOT EXISTS deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER,
    channel TEXT NOT NULL,
    status TEXT NOT NULL,
    sent_at TEXT,
    error TEXT,
    payload_json TEXT,
    FOREIGN KEY(event_id) REFERENCES events(id)
);

CREATE TABLE IF NOT EXISTS jygs_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_date TEXT NOT NULL,
    run_slot TEXT NOT NULL,
    symbol TEXT,
    name TEXT NOT NULL,
    latest_price TEXT,
    change_pct TEXT,
    board_status TEXT,
    limit_up_time TEXT,
    themes TEXT,
    reason TEXT,
    full_text TEXT,
    url TEXT,
    raw_json TEXT,
    content_hash TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    UNIQUE(trade_date, run_slot, symbol, content_hash)
);

CREATE TABLE IF NOT EXISTS stock_predictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    source_id INTEGER NOT NULL,
    symbol TEXT,
    name TEXT NOT NULL,
    trade_date TEXT,
    prediction_direction TEXT,
    duration_bucket TEXT,
    confidence TEXT,
    thesis TEXT,
    invalidation TEXT,
    model TEXT,
    prompt_version TEXT,
    analysis_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stock_outcomes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    prediction_id INTEGER NOT NULL,
    as_of_date TEXT NOT NULL,
    return_1d REAL,
    return_3d REAL,
    return_5d REAL,
    return_10d REAL,
    max_drawdown REAL,
    limit_up_days INTEGER,
    outcome_json TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE(prediction_id, as_of_date),
    FOREIGN KEY(prediction_id) REFERENCES stock_predictions(id)
);

CREATE TABLE IF NOT EXISTS prediction_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    prediction_id INTEGER NOT NULL,
    review_type TEXT NOT NULL,
    review_text TEXT NOT NULL,
    model TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(prediction_id) REFERENCES stock_predictions(id)
);

CREATE TABLE IF NOT EXISTS stock_relations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    related_symbol TEXT NOT NULL,
    relation_type TEXT NOT NULL,
    reason TEXT,
    confidence TEXT,
    source TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE(symbol, related_symbol, relation_type)
);
"""


def init_db(path: Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect_sqlite(path)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def main() -> int:
    with init_db() as conn:
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        ).fetchall()
    print("initialized tables:")
    for (name,) in tables:
        print(f"- {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
