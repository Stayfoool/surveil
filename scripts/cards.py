"""Feishu card builders for Surveil notifications."""

from __future__ import annotations

from typing import Any
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from feishu_image import image_key_from_url
from link_enrichment import analysis_text_with_links, display_url
from media_sources import is_overseas_media_source, overseas_media_access_note, overseas_media_module
from post_analysis import analyze_post, company_label, extract_tickers


def truncate(value: str, limit: int) -> str:
    cleaned = " ".join(value.split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 3] + "..."


def md_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("*", "\\*").replace("_", "\\_")


def div_markdown(content: str) -> dict[str, Any]:
    return {
        "tag": "div",
        "text": {
            "tag": "lark_md",
            "content": content,
        },
    }


def note_text(content: str) -> dict[str, Any]:
    return {
        "tag": "note",
        "elements": [
            {
                "tag": "plain_text",
                "content": content,
            }
        ],
    }


def format_time(value: str) -> str:
    if not value:
        return "unknown"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        bj = parsed.astimezone(ZoneInfo("Asia/Shanghai"))
        utc = parsed.astimezone(timezone.utc)
        return f"{bj:%Y-%m-%d %H:%M:%S} 北京时间（UTC {utc:%Y-%m-%d %H:%M:%S}）"
    except ValueError:
        return value


def now_beijing() -> str:
    return datetime.now(timezone.utc).astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S 北京时间")


def text_chunks(text: str, limit: int = 1300) -> list[str]:
    paragraphs = [part.strip() for part in text.replace("\r", "").split("\n") if part.strip()]
    if not paragraphs:
        paragraphs = [text.strip()]
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        candidate = paragraph if not current else f"{current}\n\n{paragraph}"
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
        while len(paragraph) > limit:
            chunks.append(paragraph[:limit])
            paragraph = paragraph[limit:]
        current = paragraph
    if current:
        chunks.append(current)
    return chunks


def build_serenity_card(post: dict[str, Any]) -> dict[str, Any]:
    text = post.get("full_text") or post.get("text") or ""
    preview = truncate(text, 1000)
    url = post.get("url") or ""
    tickers = extract_tickers(text)
    metrics = post.get("public_metrics") or {}
    media = post.get("_media") or []
    links = post.get("_links") or []
    analysis_lines = analyze_post(analysis_text_with_links(text, links))

    elements: list[dict[str, Any]] = [
        div_markdown(f"**发送时间**：{md_escape(now_beijing())}"),
        div_markdown(f"**发布时间**：{md_escape(format_time(str(post.get('created_at', ''))))}"),
        div_markdown("**内容类型**：X API 公开帖。当前 X API 没有返回“付费订阅内容”标记，因此不能判定为付费订阅帖。"),
    ]
    if metrics:
        elements.append(
            div_markdown(
                "**互动**："
                f"回复 {metrics.get('reply_count')} / "
                f"转发 {metrics.get('retweet_count')} / "
                f"喜欢 {metrics.get('like_count')} / "
                f"引用 {metrics.get('quote_count')}"
            )
        )
    if tickers:
        elements.append(div_markdown("**涉及标的**：" + md_escape("；".join(company_label(ticker) for ticker in tickers))))

    elements.append({"tag": "hr"})
    full_chunks = text_chunks(text)
    for index, chunk in enumerate(full_chunks, start=1):
        title = "**原文全文**" if index == 1 else f"**原文全文（续 {index}）**"
        elements.append(div_markdown(f"{title}\n{md_escape(chunk)}"))

    if links:
        elements.append({"tag": "hr"})
        elements.append(div_markdown("**外链内容**"))
        for index, link in enumerate(links, start=1):
            effective_url = str(link.get("effective_url") or link.get("url") or "")
            title = str(link.get("title") or "").strip()
            description = str(link.get("description") or "").strip()
            body = str(link.get("text") or "").strip()
            status = str(link.get("status") or "unknown")
            error = str(link.get("error") or "").strip()
            parts = [f"**链接 {index}**：{md_escape(display_url(effective_url))}"]
            if title:
                parts.append(f"标题：{md_escape(title)}")
            if description:
                parts.append(f"摘要：{md_escape(description)}")
            if body:
                parts.append(f"正文摘录：{md_escape(truncate(body, 900))}")
            if status != "ok" or error:
                parts.append(f"抓取状态：{md_escape(status)}；{md_escape(error or '未抽取到正文')}")
            elements.append(div_markdown("\n".join(parts)))
    elements.extend(
        [
            {"tag": "hr"},
            div_markdown("**快速解读**\n" + md_escape("\n".join(analysis_lines[1:]))),
        ]
    )

    embedded_count = 0
    for item in media[:3]:
        media_url = item.get("url")
        if not media_url:
            continue
        image_key = image_key_from_url(media_url)
        if not image_key:
            continue
        elements.append(
            {
                "tag": "img",
                "img_key": image_key,
                "alt": {
                    "tag": "plain_text",
                    "content": "Serenity 配图",
                },
                "mode": "fit_horizontal",
            }
        )
        embedded_count += 1

    actions: list[dict[str, Any]] = []
    if url:
        actions.append(
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "打开推文"},
                "type": "primary",
                "multi_url": {
                    "url": url,
                    "pc_url": url,
                    "ios_url": url,
                    "android_url": url,
                },
            }
        )
    for index, item in enumerate(media[:3], start=1):
        media_url = item.get("url")
        if not media_url:
            continue
        actions.append(
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": f"打开图片 {index}"},
                "type": "default",
                "multi_url": {
                    "url": media_url,
                    "pc_url": media_url,
                    "ios_url": media_url,
                    "android_url": media_url,
                },
            }
        )
    for index, link in enumerate(links[:3], start=1):
        link_url = str(link.get("effective_url") or link.get("url") or "")
        if not link_url:
            continue
        actions.append(
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": f"打开外链 {index}"},
                "type": "default",
                "multi_url": {
                    "url": link_url,
                    "pc_url": link_url,
                    "ios_url": link_url,
                    "android_url": link_url,
                },
            }
        )
    if actions:
        elements.append({"tag": "action", "actions": actions})

    if media and embedded_count == 0:
        elements.append(note_text("未配置飞书应用凭证或图片上传失败，已回退为图片按钮链接。"))

    return {
        "config": {
            "wide_screen_mode": True,
        },
        "header": {
            "template": "blue",
            "title": {
                "tag": "plain_text",
                "content": "Serenity 新帖",
            },
        },
        "elements": elements,
    }


def source_module(source: str, url: str) -> str:
    if is_overseas_media_source(source):
        return overseas_media_module(source)
    official_sources = {
        "openai_news": "OpenAI / 官方新闻",
        "nvidia_blog": "NVIDIA / 官方博客",
        "nvidia_developer_blog": "NVIDIA / Developer Blog",
        "samsung_semiconductor_news": "Samsung Semiconductor / 官方新闻",
        "samsung_global_semiconductor": "Samsung Newsroom / Semiconductors",
        "skhynix_newsroom": "SK hynix / Newsroom",
        "micron_news_releases": "Micron / News Releases",
    }
    if source in official_sources:
        return official_sources[source]
    if source == "trendforce_page":
        if "/research/download/" in url:
            return "TrendForce / Research Report 或 Selected Topics"
        if "/news/" in url:
            return "TrendForce / News"
        if "/presscenter/analysis" in url:
            return "TrendForce / Press Centre / In-Depth Analyses"
        return "TrendForce / 官方页面监控"
    if source.startswith("trendforce_"):
        if "/presscenter/" in url:
            return "TrendForce / Press Centre / News"
        if "/research/" in url:
            return "TrendForce / Research Report"
        if "selected_topics" in url:
            return "TrendForce / Selected Topics"
        return "TrendForce / RSS"
    if source == "semianalysis":
        return "SemiAnalysis / RSS"
    return source


def access_note(source: str, url: str, body_source: str) -> str:
    if is_overseas_media_source(source):
        return overseas_media_access_note(source, body_source)
    if source in {
        "openai_news",
        "nvidia_blog",
        "nvidia_developer_blog",
        "samsung_semiconductor_news",
        "samsung_global_semiconductor",
        "skhynix_newsroom",
        "micron_news_releases",
    }:
        return "免费公开内容：来自公司官方新闻/博客/RSS。"
    if source == "trendforce_page":
        if "/research/download/" in url:
            return "可能为 Research Report / Selected Topics 会员或付费内容；当前只使用官方页面公开标题/摘要，不绕过付费墙。"
        if "/news/" in url:
            return "免费公开内容：来自 TrendForce News 官方页面。"
        return f"免费/付费状态未知：正文来源为 {body_source}。"
    if source.startswith("trendforce_"):
        if "/presscenter/news/" in url:
            return "免费公开内容：来自 TrendForce Press Centre 新闻页/RSS。"
        if "/research/" in url:
            return "可能为 Research Report 内容；完整报告是否付费取决于 TrendForce 页面权限。"
        return "免费/付费状态未知：来自 RSS，需以原页面访问权限为准。"
    if source == "semianalysis":
        return "免费/付费状态未知：来自 RSS；若原页面进入付费墙，以页面权限为准。"
    return f"免费/付费状态未知：正文来源为 {body_source}。"


def build_article_card(source: str, item: dict[str, Any]) -> dict[str, Any]:
    title = item.get("title", "")
    url = item.get("url", "")
    text = item.get("full_text") or item.get("summary") or ""
    body_source = item.get("body_source", "RSS")
    analysis_lines = item.get("analysis_lines") or analyze_post(
        f"{title}\n\n{text}",
        thinking_override=item.get("analysis_thinking"),
        max_tokens_override=item.get("analysis_max_tokens"),
    )
    prefix_lines = item.get("analysis_lines_prefix") or []
    if prefix_lines and len(analysis_lines) > 1:
        analysis_lines = [analysis_lines[0], *prefix_lines, *analysis_lines[1:]]
    elements: list[dict[str, Any]] = [
        div_markdown(f"**发送时间**：{md_escape(now_beijing())}"),
        div_markdown(f"**来源模块**：{md_escape(item.get('source_module') or source_module(source, url))}"),
        div_markdown(f"**免费/付费**：{md_escape(item.get('access_note') or access_note(source, url, body_source))}"),
        div_markdown(f"**发布时间**：{md_escape(format_time(str(item.get('published_at', ''))))}"),
        div_markdown(f"**正文来源**：{md_escape(body_source)}"),
        {"tag": "hr"},
        div_markdown(f"**标题**\n{md_escape(title)}"),
    ]
    for index, chunk in enumerate(text_chunks(text), start=1):
        chunk_title = "**原文全文**" if index == 1 else f"**原文全文（续 {index}）**"
        elements.append(div_markdown(f"{chunk_title}\n{md_escape(chunk)}"))
    elements.extend(
        [
            {"tag": "hr"},
            div_markdown("**中文解读**\n" + md_escape("\n".join(analysis_lines[1:]))),
        ]
    )
    if url:
        elements.append(
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "打开原文"},
                        "type": "primary",
                        "multi_url": {
                            "url": url,
                            "pc_url": url,
                            "ios_url": url,
                            "android_url": url,
                        },
                    }
                ],
            }
        )
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "green",
            "title": {
                "tag": "plain_text",
                "content": item.get("source_display")
                or item.get("source_module")
                or f"{source} 新文章",
            },
        },
        "elements": elements,
    }
