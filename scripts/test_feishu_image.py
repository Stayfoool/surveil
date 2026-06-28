#!/usr/bin/env python3
"""Test Feishu image upload and card embedding with the latest Serenity post."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from feishu_image import configured, image_key_from_url
from x_check import configured_x_username, load_env
from x_monitor import fetch_recent_posts


def main() -> int:
    load_env(ROOT / ".env")
    if not configured():
        raise SystemExit("缺少 FEISHU_APP_ID 或 FEISHU_APP_SECRET。")
    posts = fetch_recent_posts(configured_x_username(), 5)
    for post in posts:
        media = post.get("_media") or []
        if not media:
            continue
        url = media[0]["url"]
        image_key = image_key_from_url(url)
        if not image_key:
            raise SystemExit("图片上传失败，未获得 image_key。")
        print(f"image_key={image_key}")
        return 0
    raise SystemExit("最近帖子里没有媒体图片，无法测试上传。")


if __name__ == "__main__":
    raise SystemExit(main())
