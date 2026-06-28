#!/usr/bin/env python3
"""Regression checks for X external-link enrichment."""

from __future__ import annotations

from link_enrichment import analysis_text_with_links, fetch_link, link_summary_for_prompt, url_candidates_from_post
from x_stream import merge_linked_status_links, x_status_ids_from_links


def main() -> int:
    post = {
        "text": "@Beyucu88 光迅科技 https://t.co/YLWS3Dq98d and 东山精密 https://t.co/NBKJVrwXqu",
        "entities": {
            "urls": [
                {
                    "url": "https://t.co/YLWS3Dq98d",
                    "expanded_url": "https://example.com/accelink",
                    "display_url": "example.com/accelink",
                },
                {
                    "url": "https://t.co/NBKJVrwXqu",
                    "unwound_url": "https://example.com/dsbj",
                    "display_url": "example.com/dsbj",
                },
            ]
        },
    }
    candidates = url_candidates_from_post(post)
    urls = [item["url"] for item in candidates]
    if urls != ["https://example.com/accelink", "https://example.com/dsbj"]:
        raise AssertionError(f"unexpected URL candidates: {urls!r}")

    links = [
        {
            "url": "https://t.co/YLWS3Dq98d",
            "effective_url": "https://example.com/accelink",
            "title": "光迅科技发布高速光模块新进展",
            "description": "文章摘要",
            "text": "外链正文指出光迅科技在高速光模块、硅光和 CPO 方向有客户验证进展。",
            "status": "ok",
        },
        {
            "url": "https://t.co/NBKJVrwXqu",
            "effective_url": "https://example.com/dsbj",
            "title": "东山精密 AI PCB 订单跟踪",
            "description": "",
            "text": "外链正文指出东山精密受益于 AI 服务器 PCB 需求，但短期估值已有较多反映。",
            "status": "ok",
        },
    ]
    summary = link_summary_for_prompt(links)
    if "光迅科技发布高速光模块新进展" not in summary or "东山精密 AI PCB 订单跟踪" not in summary:
        raise AssertionError("link summary lost fetched titles")
    analysis_input = analysis_text_with_links(post["text"], links)
    if "推文原文：" not in analysis_input or "推文外链内容：" not in analysis_input:
        raise AssertionError("analysis input should include both tweet and links")
    if "CPO" not in analysis_input or "AI 服务器 PCB" not in analysis_input:
        raise AssertionError("analysis input lost linked article text")

    media_link = fetch_link("https://x.com/example_user/status/2069306746343149787/photo/1")
    if media_link.get("status") != "media_link" or "JavaScript is disabled" in str(media_link.get("text")):
        raise AssertionError("X photo links should not be fetched as normal web pages")
    ids = x_status_ids_from_links(
        {
            "id": "1",
            "text": "https://t.co/example",
            "entities": {
                "urls": [
                    {
                        "expanded_url": "https://x.com/example_user/status/2069306746343149787/photo/1",
                    }
                ]
            },
        }
    )
    if ids != ["2069306746343149787"]:
        raise AssertionError(f"failed to extract linked X status id: {ids}")

    linked_post = {
        "id": "1",
        "text": "@BeardedxScholar https://t.co/vIrhRrH298",
        "entities": {
            "urls": [
                {
                    "url": "https://t.co/vIrhRrH298",
                    "expanded_url": "https://x.com/example_user/status/2069306746343149787/photo/1",
                }
            ]
        },
        "_links": [
            {
                "url": "https://t.co/vIrhRrH298",
                "effective_url": "https://x.com/example_user/status/2069306746343149787/photo/1",
                "title": "",
                "description": "",
                "text": "",
                "status": "media_link",
                "error": "X 图片页需要 JavaScript",
            }
        ],
        "_linked_statuses": [
            {
                "status_id": "2069306746343149787",
                "url": "https://x.com/example_user/status/2069306746343149787/photo/1",
                "text": "linked X post text from API",
                "author_username": "example_user",
                "media_count": 1,
                "is_photo": True,
            }
        ],
    }
    merge_linked_status_links(linked_post)
    merged = linked_post["_links"][0]
    if merged.get("status") != "ok" or "linked X post text from API" not in str(merged.get("text")):
        raise AssertionError(f"linked X status should replace media placeholder: {merged!r}")
    linked_analysis_input = analysis_text_with_links(linked_post["text"], linked_post["_links"])
    if "linked X post text from API" not in linked_analysis_input:
        raise AssertionError("linked X status text should be included in LLM input")

    print("link enrichment regression checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
