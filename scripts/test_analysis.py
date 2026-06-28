#!/usr/bin/env python3
"""Regression checks for lightweight post analysis."""

from __future__ import annotations

import os

os.environ["SURVEIL_DISABLE_LLM"] = "1"

from post_analysis import analyze_post, analyze_post_rule, detect_themes, is_author_portfolio_review


def assert_equal(actual, expected, label: str) -> None:
    if actual != expected:
        raise AssertionError(f"{label}: expected {expected!r}, got {actual!r}")


def main() -> int:
    bernstein = """Bernstein is legit the dumbest analyst firm I’ve seen calling for a 50% crash in Kioxia.

They gave $INTC a $36 PT back in Jan and now it’s $118.

Good lesson to ignore institutional reports that get published for retail consumption.

They’re not here to help retail investors."""
    assert_equal(detect_themes(bernstein), ["卖方研报/评级/目标价"], "Bernstein themes")
    failure = "\n".join(analyze_post(bernstein))
    if "【LLM 解析失败】" not in failure:
        raise AssertionError("production analysis should expose LLM failure instead of falling back")
    analysis = "\n".join(analyze_post_rule(bernstein))
    if "AI 算力" in analysis or "工业富联" in analysis:
        raise AssertionError("retail/analyst post incorrectly mapped to AI/A-share compute theme")
    if "增量判断：" not in analysis:
        raise AssertionError("analysis should always include incremental view")

    serenity_review = """I think something to highlight also is not all my ideas are green, especially on short term timeframes!

My core three themes are Neoclouds (Energy), Memory, and Photonics.

And I'm glad I chose the literal top performers for each segment from $NBIS to $EWY leaps to $SIVE.

However, I still have pretty large losses following the false analyst report on CPO delays that $NVDA refuted.

But regardless, I'd prefer to judge how ideas play out on medium term timeframes over a few months rather than a few weeks."""
    if not is_author_portfolio_review(serenity_review):
        raise AssertionError("Serenity portfolio review should be detected")
    review_analysis = "\n".join(analyze_post_rule(serenity_review))
    if "增量判断：已有预期/观点复盘" not in review_analysis:
        raise AssertionError("portfolio review should not be marked as fresh bullish/bearish incremental signal")
    if "增量判断：增量利空" in review_analysis:
        raise AssertionError("portfolio review incorrectly marked as incremental bearish")
    if "这条推文是作者复盘自己过去提出的投资想法" not in review_analysis:
        raise AssertionError("portfolio review core summary should match the post")

    cpo = "Mizuho Research revised up optical engine projections from $NVDA demand ramp. InP DFB lasers remains the focus, hello $SIVE."
    themes = detect_themes(cpo)
    if "光互连/CPO/激光器" not in themes:
        raise AssertionError("CPO/laser post should map to optical theme")

    generic = "\n".join(analyze_post_rule("A short market observation without enough detail."))
    if "增量判断：无法判断" not in generic:
        raise AssertionError("manual rule analysis should include explicit incremental uncertainty")
    print("analysis regression checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
