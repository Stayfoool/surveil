#!/usr/bin/env python3
"""Small regression tests for domestic finance media helpers."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import china_finance_media_monitor as cfm
from china_finance_media_monitor import cls_sign, parse_cls_time


def test_cls_sign_includes_empty_values_and_sorts_keys() -> None:
    params = {
        "sv": "7.7.5",
        "rn": "20",
        "refresh_type": "1",
        "os": "web",
        "lastTime": "",
        "category": "",
        "app": "CailianpressWeb",
    }
    assert cls_sign(params) == "0151cb1ca42557f82288f8ac65797220"


def test_parse_cls_time_accepts_seconds_and_milliseconds() -> None:
    assert parse_cls_time("1719806400") == "2024-07-01T04:00:00+00:00"
    assert parse_cls_time("1719806400000") == "2024-07-01T04:00:00+00:00"


def test_parse_cls_time_keeps_timezone_aware_iso() -> None:
    assert parse_cls_time("2026-06-29T08:00:00+08:00") == "2026-06-29T00:00:00+00:00"


def test_cls_poll_interval_skips_recent_fetch() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        original_db = cfm.DB_PATH
        original_min = os.environ.get("CLS_MIN_POLL_SECONDS")
        try:
            cfm.DB_PATH = Path(tmpdir) / "test.sqlite3"
            os.environ["CLS_MIN_POLL_SECONDS"] = "300"
            cfm.save_source_state("cls_telegraph_api", {"last_fetch_at": "2026-06-29T00:00:00+00:00"})
            original_now = cfm.datetime

            class FakeDateTime(cfm.datetime):
                @classmethod
                def now(cls, tz=None):  # noqa: ANN001
                    return cls.fromisoformat("2026-06-29T00:01:00+00:00")

            cfm.datetime = FakeDateTime
            assert cfm.should_skip_cls_poll("cls_telegraph_api") is True
        finally:
            cfm.DB_PATH = original_db
            cfm.datetime = original_now
            if original_min is None:
                os.environ.pop("CLS_MIN_POLL_SECONDS", None)
            else:
                os.environ["CLS_MIN_POLL_SECONDS"] = original_min


def test_run_once_fetches_sources_independently() -> None:
    calls: list[str] = []
    original_source_items = cfm.source_items
    original_record_success = cfm.record_source_success
    original_record_failure = cfm.record_source_failure
    original_save = cfm.save_new_items_with_retry
    try:
        def fake_source_items(source: str):
            calls.append(source)
            if source == "bad":
                raise RuntimeError("boom")
            return [{"id": source, "title": source, "url": "", "published_at": ""}]

        cfm.source_items = fake_source_items
        successes: list[str] = []
        failures: list[str] = []
        cfm.record_source_success = lambda conn, monitor, source: successes.append(source)
        cfm.record_source_failure = lambda conn, monitor, source, exc: failures.append(source)
        cfm.save_new_items_with_retry = lambda source, items, notify_baseline=False: list(items)
        count = cfm.run_once(["good", "bad"], notify_baseline=False)
        assert count == 1
        assert sorted(calls) == ["bad", "good"]
        assert successes == ["good"]
        assert failures == ["bad"]
    finally:
        cfm.source_items = original_source_items
        cfm.record_source_success = original_record_success
        cfm.record_source_failure = original_record_failure
        cfm.save_new_items_with_retry = original_save


def main() -> int:
    test_cls_sign_includes_empty_values_and_sorts_keys()
    test_parse_cls_time_accepts_seconds_and_milliseconds()
    test_parse_cls_time_keeps_timezone_aware_iso()
    test_cls_poll_interval_skips_recent_fetch()
    test_run_once_fetches_sources_independently()
    print("china_finance_media_monitor helper tests OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
