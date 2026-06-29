#!/usr/bin/env python3
"""Regression checks for X stream health bridging."""

from __future__ import annotations

import tempfile
from pathlib import Path

import x_stream


def test_stream_failure_records_unified_health() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        original_db = x_stream.DB_PATH
        original_alerts = x_stream.alerts_enabled
        try:
            x_stream.DB_PATH = Path(tmpdir) / "test.sqlite3"
            x_stream.alerts_enabled = lambda: False
            x_stream.record_stream_failure("HTTP 401: unauthorized", status_code=401, phase="stream")
            with x_stream.connect_db() as conn:
                row = conn.execute(
                    """
                    SELECT monitor, source, consecutive_failures, last_error
                    FROM source_health
                    WHERE monitor = ? AND source = ?
                    """,
                    ("x_stream", "auth"),
                ).fetchone()
                assert row is not None
                assert row[2] == 1
                assert "401" in row[3]
                detail = conn.execute(
                    "SELECT status, failure_count FROM x_stream_health WHERE issue_key = ?",
                    ("auth",),
                ).fetchone()
                assert detail is not None
                assert detail[0] == "failing"
                assert detail[1] == 1
        finally:
            x_stream.DB_PATH = original_db
            x_stream.alerts_enabled = original_alerts


def test_stream_recovery_clears_unified_health() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        original_db = x_stream.DB_PATH
        original_alerts = x_stream.alerts_enabled
        try:
            x_stream.DB_PATH = Path(tmpdir) / "test.sqlite3"
            x_stream.alerts_enabled = lambda: False
            x_stream.record_stream_failure("HTTP 503: unavailable", status_code=503, phase="stream")
            x_stream.record_stream_recovery(phase="stream_connected")
            with x_stream.connect_db() as conn:
                row = conn.execute(
                    """
                    SELECT consecutive_failures
                    FROM source_health
                    WHERE monitor = ? AND source = ?
                    """,
                    ("x_stream", "x_api_unavailable"),
                ).fetchone()
                assert row is not None
                assert row[0] == 0
        finally:
            x_stream.DB_PATH = original_db
            x_stream.alerts_enabled = original_alerts


def main() -> int:
    test_stream_failure_records_unified_health()
    test_stream_recovery_clears_unified_health()
    print("x stream health checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
