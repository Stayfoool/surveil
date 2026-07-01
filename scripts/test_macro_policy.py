#!/usr/bin/env python3
"""Regression checks for macro liquidity / Fed-policy monitoring rules."""

from __future__ import annotations

import china_finance_media_monitor as cfm
from macro_policy import apply_macro_review_override, is_macro_event, macro_policy_match
from sina_flash import event_from_row


def test_primary_macro_events_match_without_market_move() -> None:
    item = {"title": "今晚21:00美联储主席沃什将发表讲话，市场关注降息路径"}
    match = macro_policy_match(item)
    assert match["matched"] is True
    assert match["tier"] == "primary"


def test_former_chair_powell_still_matches_when_relevant() -> None:
    item = {"title": "前美联储主席鲍威尔评论通胀前景，2年期美债收益率大跌"}
    match = macro_policy_match(item)
    assert match["matched"] is True
    assert match["tier"] == "primary"


def test_nonfarm_cpi_pce_are_primary() -> None:
    for title in [
        "美国6月非农就业人数将于明晚公布，市场预期新增12万人",
        "美国CPI数据今晚公布，市场关注核心通胀",
        "美国核心PCE物价指数将公布，或影响美联储降息预期",
    ]:
        assert macro_policy_match({"title": title})["tier"] == "primary"


def test_secondary_data_requires_surprise_or_market_reaction() -> None:
    assert macro_policy_match({"title": "美国ADP就业人数今晚公布"})["matched"] is False
    match = macro_policy_match({"title": "美国ADP就业人数大幅不及预期，2年期美债收益率大跌8个基点"})
    assert match["matched"] is True
    assert match["tier"] == "secondary_major"


def test_retail_sales_is_ignored() -> None:
    assert macro_policy_match({"title": "美国零售销售数据今晚公布"})["matched"] is False


def test_macro_review_override_pushes_primary_events() -> None:
    review = {"importance": "medium", "push_now": False, "affected_targets": [], "reason": "普通宏观预告。"}
    item = {"title": "美国非农就业报告明晚公布，市场预期失业率维持不变"}
    updated = apply_macro_review_override(review, item)
    assert updated["importance"] == "high"
    assert updated["push_now"] is True
    assert updated["macro_policy_line"]["tier"] == "primary"
    assert "A股风险偏好" in updated["affected_targets"]


def test_china_media_focus_accepts_macro_items() -> None:
    item = {
        "title": "沃什讲话后，2年期美债收益率大跌",
        "summary": "市场重新定价美联储降息路径。",
        "full_text": "",
    }
    assert cfm.should_focus_item(item) is True


def test_sina_flash_macro_event_without_holdings_match() -> None:
    row = {
        "id": "macro-1",
        "rich_text": "美联储主席沃什讲话后，2年期美债收益率大跌，美元指数走弱。",
        "create_time": "2026-07-01 21:05:00",
        "ext": "{}",
    }
    event = event_from_row(row, holdings=[])
    assert event is not None
    assert event["symbols"] == []
    assert "宏观流动性/美联储政策" in event["themes"]
    assert event["raw"]["macro_policy_line"]["matched"] is True


def main() -> int:
    test_primary_macro_events_match_without_market_move()
    test_former_chair_powell_still_matches_when_relevant()
    test_nonfarm_cpi_pce_are_primary()
    test_secondary_data_requires_surprise_or_market_reaction()
    test_retail_sales_is_ignored()
    test_macro_review_override_pushes_primary_events()
    test_china_media_focus_accepts_macro_items()
    test_sina_flash_macro_event_without_holdings_match()
    print("macro policy checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
