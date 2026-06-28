#!/usr/bin/env python3
"""Small LLM connectivity smoke test without printing secrets."""

from __future__ import annotations

from pathlib import Path

from env_utils import load_env
from llm_analysis import call_chat_completion, llm_config


ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    load_env(ROOT / ".env")
    config = llm_config()
    if not config:
        print("LLM 未配置：缺少 LLM_API_KEY / LLM_BASE_URL / LLM_MODEL")
        return 1
    _, base_url, model = config
    print(f"LLM base_url={base_url}")
    print(f"LLM model={model}")
    parsed, used_model = call_chat_completion(
        "TrendForce says AI ASIC demand may tighten high-end MLCC supply in 2H26. "
        "Please judge whether this is incremental for related semiconductor supply-chain stocks."
    )
    print(f"LLM OK model={used_model}")
    print(f"core_content={str(parsed.get('core_content') or '')[:200]}")
    incremental = parsed.get("incremental_view") or {}
    if isinstance(incremental, dict):
        print(f"incremental_classification={incremental.get('classification') or ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
