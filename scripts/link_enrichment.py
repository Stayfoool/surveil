"""Fetch and summarize external links attached to X posts."""

from __future__ import annotations

import html
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


DEFAULT_USER_AGENT = "surveil-link-enrichment/0.1"
DEFAULT_TIMEOUT_SECONDS = 12
DEFAULT_MAX_LINKS = 4
DEFAULT_MAX_BYTES = 2 * 1024 * 1024
DEFAULT_MAX_TEXT_CHARS = 6000
DEFAULT_PROMPT_TEXT_CHARS_PER_LINK = 2200
DEFAULT_PROMPT_TOTAL_CHARS = 9000


def env_int(name: str, default: int, *, minimum: int | None = None) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    if minimum is not None:
        return max(minimum, value)
    return value


def strip_tags(value: str) -> str:
    value = re.sub(r"(?is)<script.*?</script>|<style.*?</style>|<noscript.*?</noscript>", "", value)
    value = re.sub(r"(?i)<br\s*/?>", "\n", value)
    value = re.sub(r"(?i)</(?:p|div|li|h[1-6]|tr)>", "\n", value)
    value = re.sub(r"(?s)<[^>]+>", "", value)
    value = html.unescape(value)
    return re.sub(r"[ \t\f\v]+", " ", value).strip()


def normalize_whitespace(value: str) -> str:
    value = value.replace("\r", "\n")
    value = re.sub(r"\n{3,}", "\n\n", value)
    lines = [re.sub(r"\s+", " ", line).strip() for line in value.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def decode_html_bytes(raw: bytes, content_type: str) -> str:
    charset = ""
    match = re.search(r"charset=([A-Za-z0-9_.-]+)", content_type or "", re.I)
    if match:
        charset = match.group(1)
    for encoding in [charset, "utf-8", "gb18030", "latin-1"]:
        if not encoding:
            continue
        try:
            return raw.decode(encoding, errors="replace")
        except LookupError:
            continue
    return raw.decode("utf-8", errors="replace")


def canonical_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(value.strip())
    if not parsed.scheme:
        return value.strip()
    if parsed.scheme not in {"http", "https"}:
        return value.strip()
    netloc = parsed.netloc.lower()
    path = parsed.path or "/"
    return urllib.parse.urlunsplit((parsed.scheme.lower(), netloc, path, parsed.query, ""))


def display_url(value: str, limit: int = 110) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 3] + "..."


def x_internal_link_kind(url: str) -> str:
    parsed = urllib.parse.urlsplit(url.strip())
    host = parsed.netloc.lower()
    if host not in {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}:
        return ""
    path = parsed.path.rstrip("/")
    if re.search(r"/status/\d+/photo/\d+$", path):
        return "x_photo"
    if re.search(r"/status/\d+$", path):
        return "x_status"
    return ""


def url_candidates_from_post(post: dict[str, Any]) -> list[dict[str, str]]:
    urls: list[dict[str, str]] = []
    entity_short_urls: set[str] = set()
    entities = post.get("entities")
    raw_urls = entities.get("urls") if isinstance(entities, dict) else []
    if isinstance(raw_urls, list):
        for item in raw_urls:
            if not isinstance(item, dict):
                continue
            expanded = str(item.get("unwound_url") or item.get("expanded_url") or item.get("url") or "").strip()
            short = str(item.get("url") or "").strip()
            display = str(item.get("display_url") or "").strip()
            if expanded:
                urls.append({"url": expanded, "short_url": short, "display_url": display})
            if short:
                entity_short_urls.add(canonical_url(short))

    text = str(post.get("full_text") or post.get("text") or "")
    for match in re.finditer(r"https?://[^\s)\]}>\"]+", text):
        raw = match.group(0).rstrip(".,;:!?")
        if canonical_url(raw) in entity_short_urls:
            continue
        if raw:
            urls.append({"url": raw, "short_url": raw, "display_url": ""})

    deduped: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in urls:
        key = canonical_url(item["url"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def http_request(url: str, *, method: str = "GET", timeout: int | None = None) -> tuple[str, dict[str, str], bytes]:
    request = urllib.request.Request(
        url,
        method=method,
        headers={
            "User-Agent": os.getenv("LINK_ENRICHMENT_USER_AGENT", DEFAULT_USER_AGENT),
            "Accept": "text/html,application/xhtml+xml,application/pdf;q=0.8,text/plain;q=0.7,*/*;q=0.3",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout or DEFAULT_TIMEOUT_SECONDS) as response:
        final_url = response.geturl()
        headers = {key.lower(): value for key, value in response.headers.items()}
        max_bytes = env_int("LINK_ENRICHMENT_MAX_BYTES", DEFAULT_MAX_BYTES, minimum=1024)
        content_length = headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > max_bytes:
                    raise RuntimeError(f"页面过大：{content_length} bytes")
            except ValueError:
                pass
        raw = response.read(max_bytes + 1)
        if len(raw) > max_bytes:
            raise RuntimeError(f"页面超过大小限制：{max_bytes} bytes")
        return final_url, headers, raw


def expand_url(url: str) -> str:
    try:
        final_url, _, _ = http_request(url, method="HEAD", timeout=8)
        return final_url or url
    except Exception:
        try:
            final_url, _, _ = http_request(url, method="GET", timeout=8)
            return final_url or url
        except Exception:
            return url


def meta_content(html_text: str, patterns: list[str]) -> str:
    for pattern in patterns:
        match = re.search(pattern, html_text, re.I | re.S)
        if match:
            value = html.unescape(match.group(1)).strip()
            if value:
                return normalize_whitespace(value)
    return ""


def extract_title(html_text: str) -> str:
    return meta_content(
        html_text,
        [
            r'<meta\s+property=["\']og:title["\']\s+content=["\'](.*?)["\']',
            r'<meta\s+name=["\']twitter:title["\']\s+content=["\'](.*?)["\']',
            r"<title[^>]*>(.*?)</title>",
        ],
    )


def extract_description(html_text: str) -> str:
    return meta_content(
        html_text,
        [
            r'<meta\s+name=["\']description["\']\s+content=["\'](.*?)["\']',
            r'<meta\s+property=["\']og:description["\']\s+content=["\'](.*?)["\']',
            r'<meta\s+name=["\']twitter:description["\']\s+content=["\'](.*?)["\']',
        ],
    )


def extract_body(html_text: str) -> str:
    candidates: list[str] = []
    for pattern in [
        r"(?is)<article\b[^>]*>(.*?)</article>",
        r"(?is)<main\b[^>]*>(.*?)</main>",
        r'(?is)<div\b[^>]*(?:class|id)=["\'][^"\']*(?:article|content|post|news|entry)[^"\']*["\'][^>]*>(.*?)</div>',
    ]:
        candidates.extend(match.group(1) for match in re.finditer(pattern, html_text))

    paragraphs = re.findall(r"(?is)<p\b[^>]*>(.*?)</p>", html_text)
    if paragraphs:
        candidates.append("\n\n".join(paragraphs))

    best = ""
    for candidate in candidates:
        cleaned = normalize_whitespace(strip_tags(candidate))
        if len(cleaned) > len(best):
            best = cleaned
    if not best:
        best = normalize_whitespace(strip_tags(html_text))

    lines = []
    noise = (
        "cookie",
        "privacy policy",
        "all rights reserved",
        "subscribe",
        "sign up",
        "login",
        "advertisement",
    )
    for line in best.splitlines():
        lower = line.lower()
        if len(line) < 20:
            continue
        if any(word in lower for word in noise):
            continue
        lines.append(line)
    text = "\n\n".join(lines) or best
    return text[: env_int("LINK_ENRICHMENT_MAX_TEXT_CHARS", DEFAULT_MAX_TEXT_CHARS, minimum=500)]


def fetch_link(url: str) -> dict[str, Any]:
    original_url = url.strip()
    effective_url = expand_url(original_url) if "://t.co/" in original_url else original_url
    x_kind = x_internal_link_kind(effective_url)
    result: dict[str, Any] = {
        "url": original_url,
        "effective_url": effective_url,
        "title": "",
        "description": "",
        "text": "",
        "content_type": "",
        "status": "ok",
        "error": "",
    }
    if x_kind == "x_photo":
        result["status"] = "media_link"
        result["error"] = "X 图片页需要 JavaScript，不能按普通网页抽取；应使用 X API media metadata 或卡片中的图片附件。"
        result["content_type"] = "x-photo"
        return result
    if x_kind == "x_status":
        result["status"] = "x_status_link"
        result["error"] = "X 帖子页不按普通网页抽取，避免误抓 JavaScript 提示页；如需正文/图片应走 X API。"
        result["content_type"] = "x-status"
        return result
    try:
        final_url, headers, raw = http_request(effective_url, method="GET")
        result["effective_url"] = final_url or effective_url
        x_kind = x_internal_link_kind(result["effective_url"])
        if x_kind == "x_photo":
            result["status"] = "media_link"
            result["error"] = "X 图片页需要 JavaScript，不能按普通网页抽取；应使用 X API media metadata 或卡片中的图片附件。"
            result["content_type"] = "x-photo"
            return result
        if x_kind == "x_status":
            result["status"] = "x_status_link"
            result["error"] = "X 帖子页不按普通网页抽取，避免误抓 JavaScript 提示页；如需正文/图片应走 X API。"
            result["content_type"] = "x-status"
            return result
        content_type = headers.get("content-type", "")
        result["content_type"] = content_type
        if "application/pdf" in content_type.lower() or result["effective_url"].lower().split("?", 1)[0].endswith(".pdf"):
            result["status"] = "unsupported"
            result["error"] = "PDF 外链暂未自动解析"
            return result
        html_text = decode_html_bytes(raw, content_type)
        if "<html" not in html_text.lower() and "text/plain" in content_type.lower():
            result["text"] = normalize_whitespace(html_text)[: env_int("LINK_ENRICHMENT_MAX_TEXT_CHARS", DEFAULT_MAX_TEXT_CHARS, minimum=500)]
            return result
        result["title"] = extract_title(html_text)
        result["description"] = extract_description(html_text)
        result["text"] = extract_body(html_text)
        if not result["text"] and not result["description"]:
            result["status"] = "empty"
            result["error"] = "未抽取到有效正文"
        return result
    except urllib.error.HTTPError as exc:
        result["status"] = "error"
        result["error"] = f"HTTP {exc.code}"
        return result
    except urllib.error.URLError as exc:
        result["status"] = "error"
        result["error"] = f"网络错误：{exc.reason}"
        return result
    except Exception as exc:
        result["status"] = "error"
        result["error"] = str(exc)
        return result


def enrich_post_links(post: dict[str, Any]) -> list[dict[str, Any]]:
    if os.getenv("X_LINK_ENRICHMENT_ENABLED", "1").strip() == "0":
        post["_links"] = []
        return []
    max_links = env_int("X_LINK_ENRICHMENT_MAX_LINKS", DEFAULT_MAX_LINKS, minimum=0)
    links: list[dict[str, Any]] = []
    for candidate in url_candidates_from_post(post)[:max_links]:
        fetched = fetch_link(candidate["url"])
        fetched["short_url"] = candidate.get("short_url") or ""
        fetched["display_url"] = candidate.get("display_url") or display_url(fetched.get("effective_url") or candidate["url"])
        links.append(fetched)
    post["_links"] = links
    return links


def link_summary_for_prompt(links: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    per_link_limit = env_int(
        "LINK_ENRICHMENT_PROMPT_TEXT_CHARS_PER_LINK",
        DEFAULT_PROMPT_TEXT_CHARS_PER_LINK,
        minimum=300,
    )
    for index, link in enumerate(links, start=1):
        status = str(link.get("status") or "")
        effective_url = str(link.get("effective_url") or link.get("url") or "")
        title = str(link.get("title") or "").strip()
        description = str(link.get("description") or "").strip()
        text = str(link.get("text") or "").strip()
        error = str(link.get("error") or "").strip()
        section = [f"链接 {index}: {effective_url}"]
        if title:
            section.append(f"标题：{title}")
        if description:
            section.append(f"描述：{description}")
        if text:
            if len(text) > per_link_limit:
                text = text[: per_link_limit - 3] + "..."
            section.append(f"正文摘录：{text}")
        if status != "ok" or error:
            section.append(f"抓取状态：{status or 'unknown'}；{error or '无正文'}")
        parts.append("\n".join(section))
    summary = "\n\n".join(parts)
    total_limit = env_int("LINK_ENRICHMENT_PROMPT_TOTAL_CHARS", DEFAULT_PROMPT_TOTAL_CHARS, minimum=1000)
    if len(summary) > total_limit:
        summary = summary[: total_limit - 3] + "..."
    return summary


def analysis_text_with_links(text: str, links: list[dict[str, Any]]) -> str:
    summary = link_summary_for_prompt(links)
    if not summary:
        return text
    return f"推文原文：\n{text.strip()}\n\n推文外链内容：\n{summary}"
