#!/usr/bin/env python3
"""Send a Serenity-style Feishu card using the latest X post."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from cards import build_serenity_card
from feishu import send_card
from x_check import configured_x_username, load_env, post_text
from x_monitor import fetch_recent_posts


def main() -> int:
    load_env(ROOT / ".env")
    username = configured_x_username()
    posts = fetch_recent_posts(username, 10)
    if not posts:
        raise SystemExit("没有取到 X 帖子。")
    post = next((item for item in posts if item.get("_media")), posts[0])
    post["url"] = f"https://x.com/{username}/status/{post['id']}"
    post["full_text"] = post_text(post).strip()
    sent = send_card(build_serenity_card(post))
    if not sent:
        raise SystemExit("FEISHU_WEBHOOK 为空。请先在 .env 里填写飞书机器人 webhook。")
    print(f"已发送飞书卡片测试：{post['url']}")
    print(f"媒体数量：{len(post.get('_media') or [])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
