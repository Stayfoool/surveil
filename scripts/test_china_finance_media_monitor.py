#!/usr/bin/env python3
"""Small regression tests for domestic finance media helpers."""

from __future__ import annotations

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


def main() -> int:
    test_cls_sign_includes_empty_values_and_sorts_keys()
    test_parse_cls_time_accepts_seconds_and_milliseconds()
    print("china_finance_media_monitor helper tests OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
