#!/usr/bin/env python3
"""Small iFinD connectivity smoke test without printing secrets."""

from __future__ import annotations

from pathlib import Path

from env_utils import load_env
from ifind_client import IfindClient, mask_token


ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    load_env(ROOT / ".env")
    client = IfindClient.from_env()
    print(f"iFinD base_url={client.base_url}")
    print(f"refresh_token={mask_token(client.refresh_token)}")
    if client.access_token:
        print(f"configured_access_token={mask_token(client.access_token)}")
    token = client.ensure_access_token()
    print(f"access_token={mask_token(token)}")
    data = client.realtime_quotes("300308.SZ", "latest,changeRatio,amount")
    print("real_time_quotation OK")
    print(str(data)[:1000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
