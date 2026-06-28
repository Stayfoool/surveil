#!/usr/bin/env python3
"""Smoke test Sina Finance ZhiYan API/MCP integration."""

from __future__ import annotations

import json
import os
from pathlib import Path

from env_utils import load_env
from sina_zy_client import client_from_env, result_data


ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    load_env(ROOT / ".env")
    provider = os.getenv("SINA_NEWS_PROVIDER", "zy_api").strip() or "zy_api"
    client = client_from_env(provider)
    print(f"Sina ZY provider={provider}")
    if getattr(client, "base_url", ""):
        print(f"Sina ZY URL={client.base_url}")

    flash_payload = client.news_flash_list(page=1, num=3)
    flash_data = result_data(flash_payload)
    print("newsFlashList OK")
    print(json.dumps(flash_data, ensure_ascii=False)[:1200])

    stock_payload = client.stock_news_search(market="cn", symbol="sz300308", page=1, num=3)
    stock_data = result_data(stock_payload)
    print("stockNewsSearch OK")
    print(json.dumps(stock_data, ensure_ascii=False)[:1200])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
