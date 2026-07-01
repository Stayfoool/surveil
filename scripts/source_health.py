"""Source health tracking and alerting."""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta, timezone

from feishu import send_text


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_source_health_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
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
        )
        """
    )


def alert_threshold() -> int:
    raw = os.getenv("SOURCE_HEALTH_ALERT_FAILURES", "").strip()
    try:
        return max(1, int(raw)) if raw else 3
    except ValueError:
        return 3


def alert_cooldown() -> timedelta:
    raw = os.getenv("SOURCE_HEALTH_ALERT_COOLDOWN_MINUTES", "").strip()
    try:
        minutes = max(1, int(raw)) if raw else 60
    except ValueError:
        minutes = 60
    return timedelta(minutes=minutes)


def recovery_alert_enabled() -> bool:
    return os.getenv("SOURCE_HEALTH_ALERT_RECOVERY", "1").strip() != "0"


def should_alert_recovery(failure_count: int, last_alerted_at: str | None) -> bool:
    if not recovery_alert_enabled() or failure_count < alert_threshold():
        return False
    last_alert = parse_dt(last_alerted_at)
    if not last_alert:
        return False
    return datetime.now(timezone.utc) - last_alert >= alert_cooldown()


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def should_alert_failure(failure_count: int, last_alerted_at: str | None) -> bool:
    if failure_count < alert_threshold():
        return False
    last_alert = parse_dt(last_alerted_at)
    if not last_alert:
        return True
    return datetime.now(timezone.utc) - last_alert >= alert_cooldown()


def truncate_error(error: str) -> str:
    return " ".join(str(error).split())[:500]


def record_source_failure(conn: sqlite3.Connection, monitor: str, source: str, error: Exception | str) -> None:
    ensure_source_health_table(conn)
    now = utc_now()
    row = conn.execute(
        """
        SELECT consecutive_failures, last_alerted_at
        FROM source_health
        WHERE monitor = ? AND source = ?
        """,
        (monitor, source),
    ).fetchone()
    previous_failures = int(row[0]) if row else 0
    last_alerted_at = str(row[1]) if row and row[1] else None
    failure_count = previous_failures + 1
    error_text = truncate_error(str(error))
    conn.execute(
        """
        INSERT INTO source_health (
            monitor, source, consecutive_failures, last_failure_at, last_error, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(monitor, source) DO UPDATE SET
            consecutive_failures = excluded.consecutive_failures,
            last_failure_at = excluded.last_failure_at,
            last_error = excluded.last_error,
            updated_at = excluded.updated_at
        """,
        (monitor, source, failure_count, now, error_text, now),
    )
    if should_alert_failure(failure_count, last_alerted_at):
        try:
            sent = send_text(
                "MarketPulseWire 监控源异常",
                [
                    f"模块：{monitor}",
                    f"来源：{source}",
                    f"连续失败：{failure_count}",
                    f"错误：{error_text}",
                ],
            )
        except Exception as exc:  # noqa: BLE001 - health alert must not break monitors
            print(f"{monitor}/{source} 健康告警发送失败：{exc}", flush=True)
            sent = False
        if sent:
            conn.execute(
                "UPDATE source_health SET last_alerted_at = ?, updated_at = ? WHERE monitor = ? AND source = ?",
                (now, now, monitor, source),
            )


def record_source_success(conn: sqlite3.Connection, monitor: str, source: str) -> None:
    ensure_source_health_table(conn)
    now = utc_now()
    row = conn.execute(
        """
        SELECT consecutive_failures, last_error, last_alerted_at
        FROM source_health
        WHERE monitor = ? AND source = ?
        """,
        (monitor, source),
    ).fetchone()
    previous_failures = int(row[0]) if row else 0
    previous_error = str(row[1]) if row and row[1] else ""
    last_alerted_at = str(row[2]) if row and row[2] else None
    conn.execute(
        """
        INSERT INTO source_health (
            monitor, source, consecutive_failures, last_success_at, updated_at
        ) VALUES (?, ?, 0, ?, ?)
        ON CONFLICT(monitor, source) DO UPDATE SET
            consecutive_failures = 0,
            last_success_at = excluded.last_success_at,
            updated_at = excluded.updated_at
        """,
        (monitor, source, now, now),
    )
    if should_alert_recovery(previous_failures, last_alerted_at):
        try:
            send_text(
                "MarketPulseWire 监控源恢复",
                [
                    f"模块：{monitor}",
                    f"来源：{source}",
                    f"此前连续失败：{previous_failures}",
                    f"最近错误：{previous_error}",
                ],
            )
        except Exception as exc:  # noqa: BLE001 - recovery alert must not break monitors
            print(f"{monitor}/{source} 恢复告警发送失败：{exc}", flush=True)
