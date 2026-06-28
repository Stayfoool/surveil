#!/usr/bin/env python3
"""Regression checks for LLM analysis formatting without network calls."""

from __future__ import annotations

import os

os.environ["SURVEIL_DISABLE_LLM"] = "1"

from llm_analysis import analyze_with_llm, format_llm_analysis, parse_json_object


def main() -> int:
    if analyze_with_llm("AI ASIC demand lifts MLCC demand") is not None:
        raise AssertionError("LLM should be disabled during this test")

    parsed = parse_json_object(
        """
        ```json
        {
          "core_content": "AI ASIC 推动高端 MLCC 需求集中。",
          "themes": ["MLCC/被动元件", "AI 加速器"],
          "incremental_view": {
            "classification": "增量利好",
            "surprise_level": "中",
            "priced_in": "部分定价",
            "reason": "新增信息来自供应链扩产滞后和高端规格集中。"
          },
          "initial_impact": "偏利好高端 MLCC 供应商。",
          "a_share": {
            "positive": [
              {
                "name": "风华高科",
                "code": "000636.SZ",
                "full_name": "广东风华高新科技股份有限公司",
                "listing": "深交所主板",
                "reason": "国内 MLCC 龙头之一，受益于国产替代和高端规格需求。",
                "impact_magnitude": "中",
                "duration": "数周到数月",
                "persistence": "阶段性持续",
                "confidence": "中"
              }
            ],
            "negative": []
          },
          "global_equity": {"positive": [], "negative": []},
          "tracking_points": ["高端 MLCC 交期", "云厂商 ASIC 出货"],
          "risks": ["海外扩产快于预期"],
          "watchlist_view": "可纳入观察名单，但需验证价格和订单。"
        }
        ```
        """
    )
    lines = "\n".join(format_llm_analysis(parsed, "deepseek-chat"))
    if "增量判断：增量利好" not in lines:
        raise AssertionError("incremental view missing")
    if "风华高科 000636.SZ" not in lines:
        raise AssertionError("A-share company formatting failed")
    if "模型：deepseek-chat" not in lines:
        raise AssertionError("model line missing")

    missing_incremental = "\n".join(format_llm_analysis({"core_content": "只有摘要。"}, "deepseek-chat"))
    if "增量判断：无法判断" not in missing_incremental:
        raise AssertionError("missing incremental view should be filled with fallback")
    print("llm analysis formatting checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
