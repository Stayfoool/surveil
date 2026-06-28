#!/usr/bin/env python3
"""Monitor overseas semiconductor media RSS/RDF feeds."""

from __future__ import annotations

import argparse
import time

from env_utils import load_env
from llm_analysis import llm_config
from media_sources import OVERSEAS_MEDIA_FEEDS
from rss_monitor import ENV_PATH, run_once


def enabled_feeds() -> dict[str, str]:
    return dict(OVERSEAS_MEDIA_FEEDS)


def main() -> int:
    load_env(ENV_PATH)
    config = llm_config()
    if config:
        _, base_url, model = config
        print(f"Overseas media monitor LLM config: {base_url} / {model}", flush=True)
    else:
        print("Overseas media monitor LLM config: 未配置", flush=True)

    parser = argparse.ArgumentParser(description="Monitor overseas semiconductor media feeds.")
    parser.add_argument("--interval", type=int, default=0, help="Polling interval in seconds. 0 means run once.")
    parser.add_argument("--notify-baseline", action="store_true", help="首次建立基线时也发送通知。默认不发送旧条目。")
    args = parser.parse_args()

    feeds = enabled_feeds()
    if args.interval <= 0:
        run_once(feeds, notify_baseline=args.notify_baseline)
        return 0

    print(f"开始监控 {len(feeds)} 个海外半导体媒体 feed，轮询间隔 {args.interval} 秒。", flush=True)
    while True:
        run_once(feeds, notify_baseline=args.notify_baseline)
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
