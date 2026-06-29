#!/usr/bin/env python3
"""Regression test for recovering JSON from noisy LLM output."""

from __future__ import annotations

import os

os.environ["SURVEIL_DISABLE_LLM"] = "1"

from llm_analysis import parse_json_object


def main() -> int:
    noisy = """
    前缀说明
    ```json
    {
      "core_content": "test",
      "themes": ["A", "B"],
      "incremental_view": {
        "classification": "增量利好",
        "surprise_level": "中",
        "priced_in": "部分定价",
        "reason": "x"
      }
    }
    ```
    后缀说明
    """
    parsed = parse_json_object(noisy)
    if parsed.get("core_content") != "test":
        raise AssertionError("failed to recover fenced json")
    print("llm json recovery checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
