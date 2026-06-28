"""SQLite helpers shared by local monitor processes."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar


T = TypeVar("T")


def is_locked_error(exc: BaseException) -> bool:
    return isinstance(exc, sqlite3.OperationalError) and "locked" in str(exc).lower()


def connect_sqlite(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(exist_ok=True)
    last_error: sqlite3.OperationalError | None = None
    for attempt in range(5):
        conn = sqlite3.connect(path, timeout=60, isolation_level=None)
        try:
            conn.execute("PRAGMA busy_timeout = 60000")
            try:
                conn.execute("PRAGMA journal_mode = WAL")
            except sqlite3.OperationalError as exc:
                if not is_locked_error(exc):
                    raise
            conn.execute("PRAGMA synchronous = NORMAL")
            return conn
        except sqlite3.OperationalError as exc:
            conn.close()
            last_error = exc
            if not is_locked_error(exc) or attempt == 4:
                raise
            time.sleep(0.5 * (attempt + 1))
    raise RuntimeError(f"SQLite 连接失败：{last_error}")


def retry_on_locked(operation: Callable[[], T], attempts: int = 6) -> T:
    last_error: sqlite3.OperationalError | None = None
    for attempt in range(attempts):
        try:
            return operation()
        except sqlite3.OperationalError as exc:
            if not is_locked_error(exc):
                raise
            last_error = exc
            if attempt == attempts - 1:
                break
            time.sleep(0.5 * (attempt + 1))
    raise RuntimeError(f"SQLite 数据库繁忙，重试后仍失败：{last_error}")
