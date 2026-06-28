#!/usr/bin/env python3
"""Send a test message through the configured Feishu custom bot."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from feishu import send_text
from x_check import load_env


ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    load_env(ROOT / ".env")
    sent = send_text(
        "Surveil 飞书联调测试",
        [
            "如果你看到这条消息，说明 FEISHU_WEBHOOK 已经接通。",
            f"发送时间：{datetime.now(timezone.utc).isoformat()}",
        ],
    )
    if not sent:
        raise SystemExit("FEISHU_WEBHOOK 为空。请先在 .env 里填写飞书机器人 webhook。")
    print("已发送飞书测试消息。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
