#!/usr/bin/env python3
"""Regression checks for portfolio monitor helpers."""

from __future__ import annotations

from portfolio_monitor import Holding, normalize_cn_symbol, should_keep_event


def main() -> int:
    if normalize_cn_symbol("000063.SZ") != ("000063", "szse"):
        raise AssertionError("SZ symbol normalization failed")
    if normalize_cn_symbol("600519.SH") != ("600519", "sse"):
        raise AssertionError("SH symbol normalization failed")
    holding = Holding("000063.SZ", "中兴通讯", "CN", True, {"keywords": ["投资者关系"]})
    if not should_keep_event(holding, {"title": "中兴通讯投资者关系活动记录表", "summary": ""}):
        raise AssertionError("investor relations event should be important")
    if should_keep_event(holding, {"title": "关于职工董事选举结果的公告", "summary": ""}):
        raise AssertionError("routine board event should not match without keyword")
    print("portfolio monitor helper checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
