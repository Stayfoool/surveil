#!/usr/bin/env python3
"""Regression checks for narrow semiconductor/AI industry hardline rules."""

from __future__ import annotations

from industry_hardline import (
    apply_hardline_review_override,
    explain_hardline,
    is_quantified_hardline_item,
)


def test_quantified_hardline_applies_to_allowed_sources() -> None:
    item = {
        "page_source": "semi_prnewswire_semiconductors",
        "title": "SEMI Raises 2026 Front-End Equipment Forecast to $152.2 Billion",
        "summary": "SEMI raised semiconductor equipment growth from 16.5% to 23.5%.",
    }
    assert is_quantified_hardline_item("trendforce_page", item) is True
    review = {
        "importance": "medium",
        "push_now": False,
        "affected_targets": ["半导体设备"],
        "reason": "模型认为需要日报观察。",
        "raw": {},
    }
    updated = apply_hardline_review_override("trendforce_page", item, review)
    assert updated["importance"] == "high"
    assert updated["push_now"] is True
    assert updated["industry_hardline_override"] is True
    assert "产业硬变量线覆盖" in updated["reason"]


def test_domestic_finance_sources_do_not_use_hardline_override() -> None:
    item = {
        "title": "中信建投：SEMI上修全年预期，达1522亿美元",
        "summary": "SEMI于6月11日发布报告，将2026年全球前端半导体设备市场规模增速上调至23.5%。",
    }
    review = {"importance": "medium", "push_now": False, "reason": "国内二手来源。"}
    assert is_quantified_hardline_item("yicai_brief", item) is False
    assert is_quantified_hardline_item("cls_telegraph_api", item) is False
    assert apply_hardline_review_override("yicai_brief", item, review) == review
    assert apply_hardline_review_override("cls_telegraph_api", item, review) == review


def test_prompt_note_identifies_source_family() -> None:
    note = explain_hardline(
        "digitimes_tw_semiconductors_components",
        ("AI server component capacity expands 20% with new equipment orders",),
    )
    assert "DIGITIMES" in note
    assert "硬变量" in note


def main() -> int:
    test_quantified_hardline_applies_to_allowed_sources()
    test_domestic_finance_sources_do_not_use_hardline_override()
    test_prompt_note_identifies_source_family()
    print("industry hardline checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
