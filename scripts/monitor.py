#!/usr/bin/env python3
"""Unified monitor for X and RSS sources."""

from __future__ import annotations

import argparse
import time

from rss_monitor import DEFAULT_FEEDS, run_once as run_rss_once
from x_check import configured_x_username, load_env
from x_monitor import run_once as run_x_once


def main() -> int:
    load_env(__import__("pathlib").Path(__file__).resolve().parents[1] / ".env")
    parser = argparse.ArgumentParser(description="Run all configured monitors.")
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--x-max-results", type=int, default=10)
    parser.add_argument("--skip-x", action="store_true")
    parser.add_argument("--skip-rss", action="store_true")
    args = parser.parse_args()

    username = configured_x_username()
    if args.interval <= 0:
        if not args.skip_x:
            run_x_once(username, args.x_max_results)
        if not args.skip_rss:
            run_rss_once(DEFAULT_FEEDS)
        return 0

    print(f"开始统一监控，轮询间隔 {args.interval} 秒。")
    while True:
        if not args.skip_x:
            try:
                run_x_once(username, args.x_max_results)
            except Exception as exc:
                print(f"X 监控失败：{exc}")
        if not args.skip_rss:
            try:
                run_rss_once(DEFAULT_FEEDS)
            except Exception as exc:
                print(f"RSS 监控失败：{exc}")
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
