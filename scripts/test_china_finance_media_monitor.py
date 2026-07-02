#!/usr/bin/env python3
"""Small regression tests for domestic finance media helpers."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import china_finance_media_monitor as cfm
from china_finance_media_monitor import cls_sign, next_data_from_html, parse_cls_time


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


def test_yicai_morning_brief_is_mandatory_push() -> None:
    item = {
        "title": "<b>券商晨会观点速递  |</b> ①中信建投：半导体设备全球景气周期持续确认",
        "summary": "",
        "full_text": "",
    }
    assert cfm.is_mandatory_yicai_morning_brief("yicai_brief", item) is True
    review = {"importance": "medium", "push_now": False, "reason": "普通观点汇总。"}
    updated = cfm.force_mandatory_morning_review(review, item)
    assert updated["importance"] == "high"
    assert updated["push_now"] is True
    assert updated["mandatory_push"] == "yicai_morning_brief"
    assert "强制推送规则" in updated["reason"]


def test_star_market_daily_next_data_parser() -> None:
    payload = {
        "props": {
            "pageProps": {
                "data": {
                    "articles": [
                        {
                            "article_id": 2414199,
                            "article_title": "【炬光科技：现阶段并不认为康宁Glass Bridge方案会对公司的CPO业务产生实质性的负面影响】",
                            "article_brief": "《科创板日报》1日讯，炬光科技发布投资者关系活动记录表公告。",
                            "article_author": "科创板日报记者",
                            "article_time": 1782900000,
                            "share_url": "https://api3.cls.cn/share/article/2414199?os=web&sv=7.7.5&app=CailianpressWeb",
                            "stock_list": [{"name": "炬光科技", "StockID": "sh688167"}],
                            "subjects": [{"subject_name": "科创板最新动态"}],
                            "article_tags": [{"name": "原创"}],
                        }
                    ]
                }
            }
        }
    }
    html = f'<html><script id="__NEXT_DATA__" type="application/json">{json.dumps(payload, ensure_ascii=False)}</script></html>'
    parsed = next_data_from_html(html)
    assert parsed["props"]["pageProps"]["data"]["articles"][0]["article_id"] == 2414199

    original_http_get = cfm.http_get
    try:
        class Response:
            content = html.encode("utf-8")

        cfm.http_get = lambda *args, **kwargs: Response()
        items = cfm.parse_star_market_daily_subject_items()
        assert len(items) == 1
        assert items[0]["source_module"] == "科创板日报 / 科创板最新动态"
        assert "炬光科技" in items[0]["summary"]
        assert items[0]["published_at"] == "2026-07-01T10:00:00+00:00"
    finally:
        cfm.http_get = original_http_get


def test_star_market_daily_cross_source_dedup() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        original_db = cfm.DB_PATH
        try:
            cfm.DB_PATH = Path(tmpdir) / "test.sqlite3"
            first = {
                "id": "cls-1",
                "url": "https://api3.cls.cn/share/article/1?os=web",
                "title": "《科创板日报》讯 AI芯片公司订单大增",
                "summary": "《科创板日报》讯 AI芯片公司订单大增",
                "published_at": "2026-07-01T00:00:00+00:00",
                "source_module": "科创板日报 / 财联社电报",
            }
            second = {
                "id": "subject-1",
                "url": "https://api3.cls.cn/share/article/1?os=web",
                "title": "《科创板日报》讯 AI芯片公司订单大增",
                "summary": "科创板日报专题页",
                "published_at": "2026-07-01T00:01:00+00:00",
                "source_module": "科创板日报 / 科创板最新动态",
            }
            assert len(cfm.save_new_items_with_retry("cls_telegraph_api", [first], notify_baseline=True)) == 1
            assert len(cfm.save_new_items_with_retry("star_market_daily_subject", [second], notify_baseline=True)) == 0
        finally:
            cfm.DB_PATH = original_db


def test_default_sources_include_star_market_daily() -> None:
    assert "star_market_daily_subject" in cfm.parse_sources_arg([])


def main() -> int:
    test_cls_sign_includes_empty_values_and_sorts_keys()
    test_parse_cls_time_accepts_seconds_and_milliseconds()
    test_parse_cls_time_keeps_timezone_aware_iso()
    test_cls_poll_interval_skips_recent_fetch()
    test_run_once_fetches_sources_independently()
    test_yicai_morning_brief_is_mandatory_push()
    test_star_market_daily_next_data_parser()
    test_star_market_daily_cross_source_dedup()
    test_default_sources_include_star_market_daily()
    print("china_finance_media_monitor helper tests OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
