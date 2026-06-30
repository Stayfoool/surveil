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

CREATE TABLE IF NOT EXISTS seen_items (
    source TEXT NOT NULL,
    item_id TEXT NOT NULL,
    url TEXT NOT NULL,
    title TEXT NOT NULL,
    summary TEXT,
    published_at TEXT,
    first_seen_at TEXT NOT NULL,
    PRIMARY KEY (source, item_id)
);

CREATE TABLE IF NOT EXISTS seen_sources (
    source TEXT PRIMARY KEY,
    first_seen_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trendforce_page_seen_items (
    item_id TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    title TEXT NOT NULL,
    first_source TEXT NOT NULL,
    first_seen_at TEXT NOT NULL
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
    symbol_name TEXT,
    related_symbol TEXT NOT NULL,
    related_name TEXT,
    relation_type TEXT NOT NULL,
    impact_direction TEXT,
    theme TEXT,
    reason TEXT,
    confidence TEXT,
    source TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    raw_json TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE(symbol, related_symbol, relation_type)
);

CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_table TEXT NOT NULL,
    source_id TEXT NOT NULL,
    source TEXT NOT NULL,
    source_item_id TEXT,
    title TEXT NOT NULL,
    url TEXT,
    published_at TEXT,
    first_seen_at TEXT,
    pushed_at TEXT,
    importance TEXT,
    incremental_classification TEXT,
    direction TEXT,
    confidence TEXT,
    thesis TEXT,
    invalidation TEXT,
    model TEXT,
    prompt_version TEXT,
    raw_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(source_table, source_id)
);

CREATE INDEX IF NOT EXISTS idx_signals_created ON signals(created_at);
CREATE INDEX IF NOT EXISTS idx_signals_source ON signals(source, importance);

CREATE TABLE IF NOT EXISTS signal_targets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id INTEGER NOT NULL,
    target_key TEXT NOT NULL,
    symbol TEXT,
    name TEXT,
    market TEXT,
    target_role TEXT,
    expected_direction TEXT,
    expected_horizon TEXT,
    relation_type TEXT,
    relation_reason TEXT,
    confidence TEXT,
    raw_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(signal_id, target_key, target_role),
    FOREIGN KEY(signal_id) REFERENCES signals(id)
);

CREATE INDEX IF NOT EXISTS idx_signal_targets_symbol ON signal_targets(symbol);

CREATE TABLE IF NOT EXISTS signal_evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id INTEGER NOT NULL,
    evidence_type TEXT NOT NULL,
    text TEXT NOT NULL,
    url TEXT,
    source TEXT,
    observed_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(signal_id, evidence_type, text),
    FOREIGN KEY(signal_id) REFERENCES signals(id)
);

CREATE TABLE IF NOT EXISTS signal_outcomes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id INTEGER NOT NULL,
    target_id INTEGER,
    symbol TEXT,
    as_of_date TEXT NOT NULL,
    return_1d REAL,
    return_3d REAL,
    return_5d REAL,
    return_10d REAL,
    return_20d REAL,
    excess_return_1d REAL,
    excess_return_3d REAL,
    excess_return_5d REAL,
    excess_return_10d REAL,
    excess_return_20d REAL,
    max_drawdown REAL,
    max_runup REAL,
    volume_change REAL,
    limit_up_days INTEGER,
    matched_direction TEXT,
    outcome_status TEXT NOT NULL,
    outcome_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(signal_id, symbol, as_of_date),
    FOREIGN KEY(signal_id) REFERENCES signals(id),
    FOREIGN KEY(target_id) REFERENCES signal_targets(id)
);

CREATE INDEX IF NOT EXISTS idx_signal_outcomes_symbol ON signal_outcomes(symbol, as_of_date);

CREATE TABLE IF NOT EXISTS signal_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id INTEGER NOT NULL,
    target_id INTEGER,
    symbol TEXT,
    review_type TEXT NOT NULL,
    verdict TEXT,
    error_type TEXT,
    review_text TEXT NOT NULL,
    lessons_json TEXT,
    model TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(signal_id) REFERENCES signals(id),
    FOREIGN KEY(target_id) REFERENCES signal_targets(id)
);

CREATE INDEX IF NOT EXISTS idx_signal_reviews_signal ON signal_reviews(signal_id, review_type);
CREATE INDEX IF NOT EXISTS idx_signal_reviews_created ON signal_reviews(created_at);

CREATE TABLE IF NOT EXISTS source_scores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    window_days INTEGER NOT NULL,
    signal_count INTEGER NOT NULL DEFAULT 0,
    hit_rate REAL,
    avg_excess_return REAL,
    median_reaction_lag REAL,
    false_positive_rate REAL,
    stale_news_rate REAL,
    score_json TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE(source, window_days)
);
"""


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    if column not in table_columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def migrate_schema(conn: sqlite3.Connection) -> None:
    """Apply additive migrations for existing personal SQLite databases."""
    stock_relation_columns = {
        "symbol_name": "TEXT",
        "related_name": "TEXT",
        "impact_direction": "TEXT",
        "theme": "TEXT",
        "enabled": "INTEGER NOT NULL DEFAULT 1",
        "raw_json": "TEXT",
    }
    for column, definition in stock_relation_columns.items():
        add_column_if_missing(conn, "stock_relations", column, definition)
    add_column_if_missing(conn, "signal_reviews", "target_id", "INTEGER")
    add_column_if_missing(conn, "signal_reviews", "symbol", "TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_stock_relations_symbol ON stock_relations(symbol, enabled)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_stock_relations_related ON stock_relations(related_symbol, enabled)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_signal_reviews_signal ON signal_reviews(signal_id, review_type)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_signal_reviews_symbol ON signal_reviews(symbol, created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_signal_reviews_created ON signal_reviews(created_at)")


def init_db(path: Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect_sqlite(path)
    conn.executescript(SCHEMA)
    migrate_schema(conn)
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
