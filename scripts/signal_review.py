#!/usr/bin/env python3
"""Generate automatic reviews for investment signal outcomes."""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean
from typing import Any

from db_utils import connect_sqlite
from market_db import DEFAULT_DB_PATH, init_db
from pipeline_health import record_pipeline_failure, record_pipeline_success
from signal_store import json_dumps, normalize_direction


REVIEW_VERSION = "signal-review-rules-v1"
HORIZONS = (1, 3, 5, 10, 20)
REVIEWABLE_STATUSES = {"complete", "partial_1d", "partial_3d", "partial_5d", "partial_10d", "partial_20d"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(parsed) or math.isinf(parsed):
        return None
    return parsed


def horizon_from_status(status: str) -> int:
    if status == "complete":
        return 20
    if status.startswith("partial_") and status.endswith("d"):
        try:
            return int(status.removeprefix("partial_").removesuffix("d"))
        except ValueError:
            return 0
    return 0


def best_available_return(row: sqlite3.Row) -> tuple[int, float | None]:
    for horizon in reversed(HORIZONS):
        value = safe_float(row[f"return_{horizon}d"])
        if value is not None:
            return horizon, value
    return 0, None


def classify_review(row: sqlite3.Row) -> dict[str, Any]:
    status = str(row["outcome_status"] or "")
    direction = normalize_direction(str(row["expected_direction"] or row["signal_direction"] or ""))
    horizon_ready, selected_return = best_available_return(row)
    return_1d = safe_float(row["return_1d"])
    return_3d = safe_float(row["return_3d"])
    max_runup = safe_float(row["max_runup"])
    max_drawdown = safe_float(row["max_drawdown"])
    volume_change = safe_float(row["volume_change"])
    reasons: list[str] = []
    lessons: list[str] = []
    error_type = ""

    if status not in REVIEWABLE_STATUSES or not horizon_ready or selected_return is None:
        return {
            "verdict": "unverifiable",
            "error_type": "quote_unavailable",
            "review_text": f"行情结果状态为 {status or 'unknown'}，暂无法复盘。",
            "lessons": ["保持显式失败状态，不用规则替代缺失行情。"],
            "horizon_days": horizon_ready,
            "selected_return": selected_return,
        }
    if horizon_ready < 3:
        return {
            "verdict": "too_early",
            "error_type": "window_not_ready",
            "review_text": f"当前只形成 {horizon_ready} 个交易日观察窗口，先标记为 too_early。",
            "lessons": ["等待 3/5/10 日窗口后再判断持续性。"],
            "horizon_days": horizon_ready,
            "selected_return": selected_return,
        }
    if direction not in {"positive", "negative"}:
        return {
            "verdict": "unverifiable",
            "error_type": "direction_uncertain",
            "review_text": "原始信号方向为中性或无法判断，不能用股价方向验证命中。",
            "lessons": ["后续抽取信号时应尽量明确受益/受损方向，否则只做事件归档。"],
            "horizon_days": horizon_ready,
            "selected_return": selected_return,
        }

    threshold_hit = 3.0
    threshold_partial = 1.0
    if direction == "negative":
        selected_return = -selected_return
        if return_1d is not None:
            return_1d = -return_1d
        if return_3d is not None:
            return_3d = -return_3d
        max_runup, max_drawdown = (
            -max_drawdown if max_drawdown is not None else None,
            -max_runup if max_runup is not None else None,
        )

    if selected_return >= threshold_hit:
        verdict = "hit"
        reasons.append(f"{horizon_ready} 日方向收益约 {selected_return:.2f}%，超过命中阈值。")
    elif selected_return >= threshold_partial or (max_runup is not None and max_runup >= threshold_hit):
        verdict = "partial"
        reasons.append(
            f"{horizon_ready} 日方向收益约 {selected_return:.2f}%，但期间最大有利波动约 {max_runup:.2f}%。"
            if max_runup is not None
            else f"{horizon_ready} 日方向收益约 {selected_return:.2f}%，只达到部分兑现。"
        )
        error_type = "weak_follow_through"
        lessons.append("信号有交易性反应，但持续性或幅度不足，需要区分短线情绪和基本面重估。")
    else:
        verdict = "miss"
        reasons.append(f"{horizon_ready} 日方向收益约 {selected_return:.2f}%，未兑现原方向判断。")
        if max_runup is not None and max_runup >= threshold_partial:
            error_type = "timing_or_duration_error"
            lessons.append("盘中或短窗口曾有反应，但持有窗口判断偏长。")
        elif volume_change is not None and volume_change < 20:
            error_type = "low_market_attention"
            lessons.append(
                "成交额未明显放大只是表象；需人工复查是否旧闻/已 price in，或被后续供给扩张、竞品、政策等反向信息覆盖。"
            )
        else:
            error_type = "direction_or_relevance_error"
            lessons.append("需要复查事件增量性、标的关联关系和是否已有预期。")

    if not error_type and verdict == "hit":
        error_type = "none"
        lessons.append("该来源/主题/标的关系在本窗口内得到市场验证，可作为后续人工参考。")
    if return_1d is not None:
        reasons.append(f"1 日方向收益约 {return_1d:.2f}%。")
    if return_3d is not None:
        reasons.append(f"3 日方向收益约 {return_3d:.2f}%。")
    if volume_change is not None:
        reasons.append(f"成交额变化约 {volume_change:.2f}%。")

    return {
        "verdict": verdict,
        "error_type": error_type,
        "review_text": " ".join(reasons),
        "lessons": lessons,
        "horizon_days": horizon_ready,
        "selected_return": selected_return,
    }


def latest_outcome_rows(conn: sqlite3.Connection, since: str, limit: int | None) -> list[sqlite3.Row]:
    sql = """
        WITH latest_outcome AS (
            SELECT signal_id, symbol, MAX(as_of_date) AS as_of_date
            FROM signal_outcomes
            GROUP BY signal_id, symbol
        )
        SELECT s.id AS signal_id, s.source, s.title, s.url, s.published_at,
               s.direction AS signal_direction, s.importance,
               t.id AS target_id, t.symbol, t.name, t.expected_direction, t.relation_type,
               o.as_of_date, o.return_1d, o.return_3d, o.return_5d, o.return_10d, o.return_20d,
               o.max_drawdown, o.max_runup, o.volume_change, o.matched_direction,
               o.outcome_status, o.outcome_json
        FROM signal_outcomes o
        JOIN latest_outcome lo
          ON lo.signal_id = o.signal_id AND lo.symbol = o.symbol AND lo.as_of_date = o.as_of_date
        JOIN signals s ON s.id = o.signal_id
        LEFT JOIN signal_targets t ON t.id = o.target_id
        WHERE s.created_at >= ?
        ORDER BY o.as_of_date DESC, s.id DESC
    """
    params: list[Any] = [since]
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    return list(conn.execute(sql, params).fetchall())


def existing_review_keys(conn: sqlite3.Connection) -> set[tuple[int, str, str]]:
    keys: set[tuple[int, str, str]] = set()
    for row in conn.execute(
        "SELECT signal_id, symbol, lessons_json FROM signal_reviews WHERE review_type = ?",
        (REVIEW_VERSION,),
    ).fetchall():
        symbol = str(row[1] or "")
        as_of_date = ""
        if not symbol:
            try:
                lessons = json.loads(str(row[2] or "{}"))
            except json.JSONDecodeError:
                lessons = {}
            symbol = str(lessons.get("symbol") or "") if isinstance(lessons, dict) else ""
            as_of_date = str(lessons.get("as_of_date") or "") if isinstance(lessons, dict) else ""
        else:
            try:
                lessons = json.loads(str(row[2] or "{}"))
            except json.JSONDecodeError:
                lessons = {}
            as_of_date = str(lessons.get("as_of_date") or "") if isinstance(lessons, dict) else ""
        keys.add((int(row[0]), symbol, as_of_date))
    return keys


def insert_review(conn: sqlite3.Connection, row: sqlite3.Row, review: dict[str, Any]) -> None:
    symbol = str(row["symbol"] or "")
    lessons_json = {
        "symbol": symbol,
        "name": row["name"] or "",
        "as_of_date": row["as_of_date"] or "",
        "horizon_days": review.get("horizon_days"),
        "selected_return": review.get("selected_return"),
        "lessons": review.get("lessons") or [],
        "outcome_status": row["outcome_status"] or "",
        "review_version": REVIEW_VERSION,
    }
    conn.execute(
        """
        INSERT INTO signal_reviews (
            signal_id, target_id, symbol, review_type, verdict, error_type, review_text,
            lessons_json, model, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            int(row["signal_id"]),
            int(row["target_id"]) if row["target_id"] is not None else None,
            symbol,
            REVIEW_VERSION,
            review.get("verdict") or "",
            review.get("error_type") or "",
            f"{symbol}：{review.get('review_text') or ''}",
            json_dumps(lessons_json),
            "rules",
            utc_now(),
        ),
    )


def refresh_source_scores(conn: sqlite3.Connection, window_days: int = 30) -> None:
    since = (datetime.now(timezone.utc) - timedelta(days=window_days)).isoformat()
    rows = conn.execute(
        """
        SELECT s.source, r.verdict, r.error_type, r.lessons_json
        FROM signal_reviews r
        JOIN signals s ON s.id = r.signal_id
        WHERE r.review_type = ? AND r.created_at >= ?
        """,
        (REVIEW_VERSION, since),
    ).fetchall()
    grouped: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        grouped[str(row[0] or "unknown")].append(row)
    now = utc_now()
    for source, source_rows in grouped.items():
        verdicts = Counter(str(row[1] or "") for row in source_rows)
        signal_count = len(source_rows)
        hit_rate = verdicts["hit"] / signal_count if signal_count else None
        false_positive_rate = verdicts["miss"] / signal_count if signal_count else None
        returns: list[float] = []
        lags: list[int] = []
        stale_count = 0
        for row in source_rows:
            try:
                lessons = json.loads(str(row[3] or "{}"))
            except json.JSONDecodeError:
                lessons = {}
            value = safe_float(lessons.get("selected_return")) if isinstance(lessons, dict) else None
            lag = lessons.get("horizon_days") if isinstance(lessons, dict) else None
            if value is not None:
                returns.append(value)
            if isinstance(lag, int) and lag > 0:
                lags.append(lag)
            if str(row[2] or "") in {"quote_unavailable", "direction_uncertain"}:
                stale_count += 1
        score_json = {
            "verdicts": dict(verdicts),
            "review_version": REVIEW_VERSION,
        }
        conn.execute(
            """
            INSERT INTO source_scores (
                source, window_days, signal_count, hit_rate, avg_excess_return,
                median_reaction_lag, false_positive_rate, stale_news_rate,
                score_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source, window_days) DO UPDATE SET
                signal_count = excluded.signal_count,
                hit_rate = excluded.hit_rate,
                avg_excess_return = excluded.avg_excess_return,
                median_reaction_lag = excluded.median_reaction_lag,
                false_positive_rate = excluded.false_positive_rate,
                stale_news_rate = excluded.stale_news_rate,
                score_json = excluded.score_json,
                updated_at = excluded.updated_at
            """,
            (
                source,
                window_days,
                signal_count,
                hit_rate,
                mean(returns) if returns else None,
                sorted(lags)[len(lags) // 2] if lags else None,
                false_positive_rate,
                stale_count / signal_count if signal_count else None,
                json_dumps(score_json),
                now,
            ),
        )


def review_signals(*, db_path: Path, days: int, limit: int | None = None, dry_run: bool = False) -> dict[str, int]:
    init_db(db_path).close()
    since = (datetime.now(timezone.utc) - timedelta(days=max(1, days))).isoformat()
    counts = {"outcomes": 0, "reviewed": 0, "skipped_existing": 0}
    with connect_sqlite(db_path) as conn:
        conn.row_factory = sqlite3.Row
        existing = existing_review_keys(conn)
        for row in latest_outcome_rows(conn, since, limit):
            counts["outcomes"] += 1
            key = (int(row["signal_id"]), str(row["symbol"] or ""), str(row["as_of_date"] or ""))
            if key in existing:
                counts["skipped_existing"] += 1
                continue
            review = classify_review(row)
            if dry_run:
                print(
                    f"[dry-run] signal={row['signal_id']} {row['symbol']} "
                    f"{review['verdict']} {review['error_type']}",
                    flush=True,
                )
                counts["reviewed"] += 1
                continue
            insert_review(conn, row, review)
            counts["reviewed"] += 1
            print(
                f"review signal={row['signal_id']} {row['symbol']} "
                f"{review['verdict']} {review['error_type']}",
                flush=True,
            )
        if not dry_run:
            refresh_source_scores(conn, 30)
            refresh_source_scores(conn, 90)
            conn.commit()
    return counts


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate automatic reviews for signal outcomes.")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="SQLite DB path.")
    parser.add_argument("--days", type=int, default=60, help="Signal lookback days. Default: 60.")
    parser.add_argument("--limit", type=int, default=None, help="Limit outcome rows.")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    db_path = Path(args.db)
    try:
        counts = review_signals(db_path=db_path, days=args.days, limit=args.limit, dry_run=args.dry_run)
        print(json.dumps(counts, ensure_ascii=False, sort_keys=True), flush=True)
        if not args.dry_run:
            record_pipeline_success("signal_review", db_path=db_path)
        return 0
    except Exception as exc:  # noqa: BLE001
        if not args.dry_run:
            record_pipeline_failure("signal_review", exc, db_path=db_path)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
