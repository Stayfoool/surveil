#!/usr/bin/env python3
"""Regression checks for investment gate prompt guardrails."""

from __future__ import annotations

import article_gate
import official_news_gate


def assert_contains(text: str, expected: str) -> None:
    if expected not in text:
        raise AssertionError(f"prompt missing expected text: {expected}")


def main() -> int:
    article_prompt = article_gate.GATE_SYSTEM_PROMPT + "\n" + article_gate.GATE_USER_PROMPT
    official_prompt = official_news_gate.GATE_SYSTEM_PROMPT + "\n" + official_news_gate.GATE_USER_PROMPT

    for prompt in (article_prompt, official_prompt):
        assert_contains(prompt, "星际之门/Stargate-like")
        assert_contains(prompt, "超大资本开支")
        assert_contains(prompt, "预告/据报/拟宣布/将公布")
        assert_contains(prompt, "不能因为尚未正式公布就自动降为 medium")
        assert_contains(prompt, "待确认/预告性质")
        assert_contains(prompt, "设备、材料、存储、光通信、PCB、先进封装、电力、液冷")

    assert_contains(article_prompt, "push_now=true")
    assert_contains(official_prompt, "should_push_now=true")
    print("gate prompt guardrail checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
