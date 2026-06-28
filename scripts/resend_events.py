"""Resend existing unified events to Feishu using current settings."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from db_utils import connect_sqlite
from env_utils import load_env
from event_pipeline import analyze_event, maybe_deliver_event
from market_db import DEFAULT_DB_PATH, init_db


def latest_analysis(event_id: int, task: str, db_path: Path) -> dict | None:
    with connect_sqlite(db_path) as conn:
        row = conn.execute(
            """
            SELECT analysis_json
            FROM event_analyses
            WHERE event_id = ? AND task = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (event_id, task),
        ).fetchone()
    if not row:
        return None
    return json.loads(row[0])


def event_exists(event_id: int, db_path: Path) -> bool:
    with connect_sqlite(db_path) as conn:
        row = conn.execute("SELECT 1 FROM events WHERE id = ? LIMIT 1", (event_id,)).fetchone()
    return bool(row)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("event_ids", nargs="+", type=int, help="Unified events.id values to resend.")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="SQLite database path.")
    parser.add_argument("--task", default="sina_stock_news_portfolio", help="Analysis task name.")
    parser.add_argument(
        "--reanalyze",
        action="store_true",
        help="Generate a fresh LLM analysis with current model before sending.",
    )
    return parser.parse_args()


def main() -> int:
    load_env()
    args = parse_args()
    db_path = Path(args.db)
    init_db(db_path).close()
    exit_code = 0
    for event_id in args.event_ids:
        if not event_exists(event_id, db_path):
            print(f"event #{event_id}: missing", file=sys.stderr)
            exit_code = 1
            continue
        try:
            if args.reanalyze:
                analysis = analyze_event(event_id, task=f"{args.task}_manual_resend", db_path=db_path)
            else:
                analysis = latest_analysis(event_id, args.task, db_path)
                if analysis is None:
                    analysis = analyze_event(event_id, task=args.task, db_path=db_path)
        except Exception as exc:  # noqa: BLE001 - manual ops should continue with other IDs.
            print(f"event #{event_id}: analysis_failed: {exc}", file=sys.stderr)
            exit_code = 1
            continue
        status = maybe_deliver_event(event_id, analysis, db_path=db_path)
        print(f"event #{event_id}: {status}")
        if status != "sent":
            exit_code = 1
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
