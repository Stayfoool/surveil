#!/usr/bin/env python3
"""Low-frequency Sina Finance per-stock news monitor for portfolio holdings."""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from db_utils import connect_sqlite
from env_utils import load_env
from event_pipeline import analyze_event, content_hash, load_enabled_holdings, maybe_deliver_event, upsert_event
from llm_analysis import call_chat_completion_with_prompts
from market_db import DEFAULT_DB_PATH, init_db
from portfolio_import import import_holdings
from sina_zy_client import client_from_env, result_data


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = ROOT / "config" / "portfolio.json"
SOURCE = "sina_stock_news"
STATE_KEY = "sina_stock_news"
BASE_URL = "https://vip.stock.finance.sina.com.cn/corp/go.php/vCB_AllNewsStock/symbol/{symbol}.phtml"
COMPANY_COLON_PATTERN = re.compile(r"^[\u4e00-\u9fffA-Za-z0-9Ａ-Ｚａ-ｚ（）()·]+[：:]")
ANNOUNCEMENT_KEYWORDS = (
    "公告",
    "报告书",
    "法律意见书",
    "审计报告",
    "年度报告",
    "半年度报告",
    "季度报告",
    "一季报",
    "三季报",
    "年报",
    "半年报",
    "业绩预告",
    "业绩快报",
    "定期报告",
    "风险提示",
    "公司提示",
    "估值畸高",
    "严重脱离基本面",
    "投资者关系活动记录表",
    "投资者关系活动",
    "股票交易异常波动",
    "停牌核查",
    "停牌",
    "复牌",
    "权益分派",
    "利润分配",
    "拟斥资",
    "回购股份",
    "回购A股",
    "回购A 股",
    "回购方案",
    "回购报告书",
    "价格上限调整",
    "股东大会",
    "董事会",
    "监事会",
    "独立董事",
    "募集资金",
    "监管协议",
    "关联交易",
    "担保",
    "质押",
    "减持",
    "增持",
    "解除限售",
    "限售股",
    "限售股份",
    "股权激励",
    "限制性股票",
    "股票期权",
    "激励计划",
    "授予",
    "归属",
    "注销",
    "公司章程",
)
DEFAULT_RELEVANCE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "000725.SZ": (
        "京东方",
        "面板",
        "显示",
        "OLED",
        "LCD",
        "柔性显示",
        "玻璃基",
        "封装载板",
        "先进封装",
        "Mini LED",
        "Micro LED",
    ),
    "001270.SZ": (
        "铖昌",
        "相控阵",
        "T/R",
        "TR芯片",
        "射频芯片",
        "微波",
        "毫米波",
        "卫星",
        "雷达",
        "低轨",
    ),
    "300179.SZ": (
        "四方达",
        "培育钻石",
        "金刚石",
        "超硬材料",
        "复合片",
        "散热",
        "功能性金刚石",
        "CVD金刚石",
    ),
    "300285.SZ": (
        "国瓷",
        "MLCC",
        "钛酸钡",
        "电子陶瓷",
        "陶瓷粉体",
        "氧化锆",
        "氧化铝",
        "陶瓷基板",
        "固态电池",
    ),
    "300308.SZ": (
        "中际旭创",
        "光模块",
        "CPO",
        "NPO",
        "LPO",
        "800G",
        "1.6T",
        "光互连",
        "光通信",
        "硅光",
        "数据中心",
    ),
    "300433.SZ": (
        "蓝思",
        "盖板玻璃",
        "防护玻璃",
        "玻璃盖板",
        "消费电子玻璃",
        "车载玻璃",
        "结构件",
        "蓝宝石",
        "超级玻璃",
        "玻璃基板",
        "TGV",
        "玻璃通孔",
    ),
    "301511.SZ": (
        "德福",
        "铜箔",
        "复合集流体",
        "锂电铜箔",
        "PCB铜箔",
        "RTF铜箔",
        "电解铜箔",
    ),
    "601869.SH": (
        "长飞",
        "光纤",
        "光缆",
        "光通信",
        "特种光纤",
        "数据中心",
        "空芯光纤",
    ),
    "603773.SH": (
        "沃格",
        "玻璃基板",
        "玻璃通孔",
        "TGV",
        "超级玻璃",
        "电镀填孔",
        "先进封装",
        "封装载板",
        "Mini LED",
        "显示玻璃",
    ),
    "688143.SH": (
        "长盈通",
        "特种光纤",
        "光纤环",
        "光纤陀螺",
        "惯导",
        "军工",
        "低空",
    ),
    "688456.SH": (
        "有研粉材",
        "金属粉体",
        "锡粉",
        "铜粉",
        "镍粉",
        "3D打印",
        "增材制造",
        "电子浆料",
        "靶材",
    ),
    "688498.SH": (
        "源杰",
        "激光芯片",
        "光芯片",
        "DFB",
        "EML",
        "CW",
        "VCSEL",
        "InP",
        "磷化铟",
        "光模块",
        "光通信",
        "CPO",
    ),
    "688820.SH": (
        "盛合晶微",
        "封测",
        "先进封装",
        "晶圆级封装",
        "WLP",
        "Chiplet",
        "FOPLP",
        "Fan-out",
        "玻璃基板",
    ),
}
DEFAULT_RELEVANCE_EXCLUDE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "300433.SZ": ("光纤", "光缆", "光通信", "光模块"),
}
RELEVANCE_SYSTEM_PROMPT = """你是 A 股持仓新闻监控系统的相关性判定器。
任务：判断一条新浪个股资讯标题是否真的与某只持仓股相关。
要求：
- 只输出 JSON，不要 Markdown，不要输出 JSON 外解释。
- 不要因为新闻出现在某只股票页面就默认相关；新浪个股页会混入同行业、同概念甚至弱相关噪声。
- 如果标题直接提到该公司简称、全称、股票代码，通常是 directly_relevant。
- 如果标题没有直接提公司，只能在业务、产品、上下游、竞争格局、客户需求、行业价格、技术路线或板块行情对该公司有清晰影响时判为 relevant。
- 如果只是泛市场、其他公司、宏观行情、ETF、基金持仓、弱概念蹭热点，判为 not_relevant。
- 置信度低时宁可判 not_relevant，避免错误推送。
"""
RELEVANCE_USER_PROMPT = """请判断以下新闻标题与持仓股是否相关，并输出 JSON。

输出格式：
{{
  "relevant": true,
  "confidence": "high/medium/low",
  "relation": "directly_relevant/business_related/industry_related/upstream_downstream/competitor_related/theme_related/not_relevant",
  "reason": "一句中文说明为什么相关或不相关"
}}

持仓股：
- 简称：{name}
- 代码：{symbol}
- 全称：{full_name}
- 别名：{aliases}
- 业务关键词：{keywords}
- 业务简介：{business_summary}

新闻：
- 标题：{title}
- 发布时间：{published_at}
- URL：{url}
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def env_int(name: str, default: int, minimum: int = 1) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return max(minimum, int(raw))
    except ValueError:
        return default


def env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    return raw.lower() in {"1", "true", "yes", "y", "on", "是"}


def news_provider() -> str:
    return os.getenv("SINA_NEWS_PROVIDER", "legacy").strip().lower() or "legacy"


def sina_symbol(symbol: str) -> str:
    raw = symbol.strip().upper()
    if raw.endswith(".SZ"):
        return f"sz{raw.split('.')[0]}"
    if raw.endswith(".SH"):
        return f"sh{raw.split('.')[0]}"
    if raw.endswith(".BJ"):
        return f"bj{raw.split('.')[0]}"
    return raw.lower()


def sina_zy_symbol(symbol: str) -> tuple[str, str]:
    raw = symbol.strip().upper()
    code = raw.split(".", 1)[0]
    if raw.endswith(".SZ"):
        return "cn", f"sz{code}"
    if raw.endswith(".SH"):
        return "cn", f"sh{code}"
    if raw.endswith(".BJ"):
        return "cn", f"bj{code}"
    if raw.startswith("HK"):
        return "hk", raw[2:].zfill(5)
    if raw.endswith(".HK"):
        return "hk", code.zfill(5)
    return "cn", raw.lower()


def absolute_url(url: str) -> str:
    if url.startswith("//"):
        return f"https:{url}"
    if url.startswith("/"):
        return f"https://vip.stock.finance.sina.com.cn{url}"
    return url


def strip_markup(value: str) -> str:
    text = re.sub(r"<[^>]+>", "", value or "")
    return html.unescape(text).replace("\u3000", " ").strip()


AI_GENERATED_PATTERNS = (
    r"内容\s*由\s*A[IＩ]\s*生成",
    r"本文\s*由\s*A[IＩ]\s*生成",
    r"本[文篇]内容\s*由\s*A[IＩ]\s*生成",
    r"A[IＩ]\s*生成内容",
    r"智能生成内容",
)


def is_ai_generated_content(value: str) -> bool:
    text = strip_markup(value)
    normalized = re.sub(r"\s+", "", text).upper()
    return any(re.search(pattern, normalized, re.I) for pattern in AI_GENERATED_PATTERNS)


def fetch_article_text(url: str, timeout: int = 15) -> str:
    if not url.startswith(("http://", "https://")):
        return ""
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "User-Agent": "surveil-sina-stock-news/article-fetch/0.1",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read()
    head = body[:1000].decode("ascii", errors="ignore").lower()
    encoding = "utf-8" if "utf-8" in head else "gb18030"
    html_text = body.decode(encoding, errors="replace")
    if is_ai_generated_content(html_text):
        raise ValueError("sina_ai_generated_content")
    paragraphs: list[str] = []
    for match in re.finditer(r"<p[^>]*>(.*?)</p>", html_text, re.I | re.S):
        paragraph = strip_markup(match.group(1))
        if not paragraph:
            continue
        if paragraph.startswith(("网友提问", "老师回答", "新浪简介", "Copyright")):
            continue
        if "新浪财经意见反馈" in paragraph or "24小时滚动播报" in paragraph:
            continue
        paragraphs.append(paragraph)
    if paragraphs:
        article_text = "\n".join(paragraphs).strip()
        if is_ai_generated_content(article_text):
            raise ValueError("sina_ai_generated_content")
        return article_text
    body_match = re.search(r'<div[^>]+id=["\']artibody["\'][^>]*>(.*?)</div>', html_text, re.I | re.S)
    if body_match:
        article_text = strip_markup(body_match.group(1))
        if is_ai_generated_content(article_text):
            raise ValueError("sina_ai_generated_content")
        return article_text
    return ""


def parse_published_datetime(value: str) -> datetime | None:
    text = (value or "").strip()
    match = re.search(r"\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2})?", text)
    if not match:
        return None
    candidate = match.group(0).replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(candidate, fmt)
        except ValueError:
            continue
    return None


def freshness_hint(published_at: str, text: str) -> dict[str, Any]:
    published = parse_published_datetime(published_at)
    if not published or not text:
        return {"status": "unknown"}
    candidates: list[datetime] = []
    for year, month, day in re.findall(r"(20\d{2})年(\d{1,2})月(\d{1,2})日", text):
        try:
            candidates.append(datetime(int(year), int(month), int(day)))
        except ValueError:
            continue
    for month, day in re.findall(r"(?<!\d)(\d{1,2})月(\d{1,2})日", text):
        try:
            candidates.append(datetime(published.year, int(month), int(day)))
        except ValueError:
            continue
    earlier = sorted({candidate.date() for candidate in candidates if candidate.date() < published.date()})
    if not earlier:
        return {"status": "same_day_or_unknown"}
    earliest = earlier[0]
    latest_prior = earlier[-1]
    days_since_latest_prior = (published.date() - latest_prior).days
    status = "possibly_stale"
    if days_since_latest_prior >= env_int("SINA_STOCK_NEWS_STALE_DAYS", 3, minimum=1):
        status = "stale_or_rehash"
    reacted = bool(re.search(r"(股价|收盘|大跌|大涨|下跌|上涨|跌幅|涨幅|蒸发|市值).{0,20}(\d+(?:\.\d+)?%)", text))
    return {
        "status": status,
        "earliest_prior_date": earliest.isoformat(),
        "latest_prior_date": latest_prior.isoformat(),
        "days_since_latest_prior": days_since_latest_prior,
        "mentions_price_reaction": reacted,
        "instruction": "文章发布时间不等于事件首次披露时间；若正文显示事件已在更早日期发生或股价已反应，增量判断应倾向已有预期/已定价/低重要性。",
    }


def normalize_title_token(value: str) -> str:
    return re.sub(r"\s+", "", (value or "").replace("Ａ", "A")).upper()


def normalize_keyword_text(value: str) -> str:
    return re.sub(r"\s+", "", (value or "").replace("Ａ", "A")).upper()


def holding_name_tokens(holding: dict[str, Any]) -> list[str]:
    values = [
        str(holding.get("name") or ""),
        str(holding.get("full_name") or ""),
        *(str(item) for item in holding.get("aliases") or []),
    ]
    tokens: list[str] = []
    for value in values:
        token = normalize_title_token(value)
        if token and token not in tokens:
            tokens.append(token)
    return tokens


def is_announcement_like(item: dict[str, str], holding: dict[str, Any]) -> bool:
    """Sina stock-news often republishes company disclosures; iFinD is authoritative for those."""
    title = item.get("title", "").strip()
    if not title:
        return False
    normalized_title = normalize_title_token(title)
    for token in holding_name_tokens(holding):
        if token and re.match(rf"^{re.escape(token)}[：:]", normalized_title):
            return True
    if COMPANY_COLON_PATTERN.match(title):
        prefix = re.split(r"[：:]", title, maxsplit=1)[0]
        if normalize_title_token(prefix) in holding_name_tokens(holding):
            return True
    return any(keyword in title for keyword in ANNOUNCEMENT_KEYWORDS)


def holding_keywords(holding: dict[str, Any], key: str, defaults: dict[str, tuple[str, ...]]) -> list[str]:
    symbol = str(holding.get("symbol") or "").upper()
    values = [str(item) for item in holding.get(key) or []]
    values.extend(defaults.get(symbol, ()))
    unique: list[str] = []
    for value in values:
        if value and value not in unique:
            unique.append(value)
    return unique


def title_contains_any(title: str, keywords: list[str]) -> bool:
    normalized = normalize_keyword_text(title)
    return any(normalize_keyword_text(keyword) in normalized for keyword in keywords if keyword)


def is_direct_holding_mention(item: dict[str, str], holding: dict[str, Any]) -> bool:
    title = normalize_keyword_text(item.get("title", ""))
    symbol = str(holding.get("symbol") or "").upper()
    code = symbol.split(".")[0] if symbol else ""
    tokens = holding_name_tokens(holding)
    if code and code in title:
        return True
    return any(token and token in title for token in tokens)


def is_relevant_to_holding(item: dict[str, str], holding: dict[str, Any]) -> tuple[bool, str]:
    """Guard against noisy Sina per-stock pages that mix broad industry articles into unrelated stocks."""
    if is_direct_holding_mention(item, holding):
        return True, "direct_mention"

    title = item.get("title", "")
    include_keywords = holding_keywords(holding, "news_keywords", DEFAULT_RELEVANCE_KEYWORDS)
    exclude_keywords = holding_keywords(holding, "news_exclude_keywords", DEFAULT_RELEVANCE_EXCLUDE_KEYWORDS)
    if title_contains_any(title, exclude_keywords):
        return False, "excluded_keyword"
    if include_keywords and title_contains_any(title, include_keywords):
        return True, "business_keyword"
    return False, "needs_llm_relevance"


def llm_relevance_enabled(dry_run: bool) -> bool:
    if env_flag("SINA_STOCK_NEWS_DISABLE_LLM_RELEVANCE", False):
        return False
    if dry_run:
        return env_flag("SINA_STOCK_NEWS_LLM_RELEVANCE_DRY_RUN", False)
    return env_flag("SINA_STOCK_NEWS_LLM_RELEVANCE", True)


def llm_relevance_judgment(item: dict[str, str], holding: dict[str, Any]) -> dict[str, Any]:
    keywords = holding_keywords(holding, "news_keywords", DEFAULT_RELEVANCE_KEYWORDS)
    aliases = holding.get("aliases") or []
    user_prompt = RELEVANCE_USER_PROMPT.format(
        name=str(holding.get("name") or ""),
        symbol=str(holding.get("symbol") or ""),
        full_name=str(holding.get("full_name") or ""),
        aliases="、".join(str(alias) for alias in aliases) or "无",
        keywords="、".join(keywords) or "无",
        business_summary=str(holding.get("business_summary") or ""),
        title=item.get("title", ""),
        published_at=item.get("published_at", ""),
        url=item.get("url", ""),
    )
    parsed, model = call_chat_completion_with_prompts(
        RELEVANCE_SYSTEM_PROMPT,
        user_prompt,
        user_agent="surveil-sina-stock-news-relevance/0.1",
        truncate_user_prompt=False,
    )
    parsed["_model"] = model
    return parsed


def is_llm_relevant(judgment: dict[str, Any]) -> bool:
    raw_relevant = judgment.get("relevant")
    if isinstance(raw_relevant, str):
        relevant = raw_relevant.strip().lower() in {"true", "yes", "1", "y", "是", "相关"}
    else:
        relevant = bool(raw_relevant)
    confidence = str(judgment.get("confidence") or "").strip().lower()
    relation = str(judgment.get("relation") or "").strip().lower()
    if relation == "not_relevant":
        return False
    return relevant and confidence in {"high", "medium", "高", "中"}


def check_relevance(
    item: dict[str, str],
    holding: dict[str, Any],
    *,
    dry_run: bool,
) -> tuple[bool, str, dict[str, Any] | None]:
    relevant, relevance_reason = is_relevant_to_holding(item, holding)
    if relevant:
        return True, relevance_reason, None
    if relevance_reason == "excluded_keyword":
        return False, relevance_reason, None
    if not llm_relevance_enabled(dry_run):
        return False, relevance_reason, None
    try:
        judgment = llm_relevance_judgment(item, holding)
    except Exception as exc:  # noqa: BLE001 - relevance guard should not break monitoring
        return False, f"llm_relevance_failed:{exc}", None
    if is_llm_relevant(judgment):
        return True, "llm_relevance", judgment
    return False, "llm_not_relevant", judgment


def fetch_html(symbol: str, timeout: int = 15) -> str:
    url = BASE_URL.format(symbol=urllib.parse.quote(symbol))
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "User-Agent": "surveil-sina-stock-news/0.1",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read()
    return body.decode("gb18030", errors="replace")


def parse_news_items(html_text: str) -> list[dict[str, str]]:
    match = re.search(r'<div\s+class=["\']datelist["\']>\s*<ul>(.*?)</ul>', html_text, re.I | re.S)
    if not match:
        return []
    block = match.group(1)
    pattern = re.compile(
        r"(\d{4}-\d{2}-\d{2})&nbsp;(\d{2}:\d{2})&nbsp;&nbsp;"
        r"<a\s+target=['\"]_blank['\"]\s+href=['\"]([^'\"]+)['\"]>(.*?)</a>",
        re.I | re.S,
    )
    items: list[dict[str, str]] = []
    for date_text, time_text, url, title_html in pattern.findall(block):
        title = strip_markup(title_html)
        if not title:
            continue
        items.append(
            {
                "published_at": f"{date_text} {time_text}",
                "url": absolute_url(html.unescape(url.strip())),
                "title": title,
            }
        )
    return items


def format_published_at(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, (int, float)) or (isinstance(value, str) and re.fullmatch(r"\d{10,13}", value.strip())):
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp = timestamp / 1000
        return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")
    return str(value).strip()


def normalize_api_news_item(row: dict[str, Any]) -> dict[str, str] | None:
    title = strip_markup(str(row.get("title") or row.get("name") or row.get("content") or ""))
    if not title:
        return None
    url = str(row.get("url") or row.get("wapurl") or row.get("link") or "").strip()
    published_at = format_published_at(
        row.get("published_at")
        or row.get("publish_time")
        or row.get("pubtime")
        or row.get("ctime")
        or row.get("cTime")
        or row.get("time")
        or row.get("date")
        or ""
    )
    item = {"published_at": published_at, "url": absolute_url(url), "title": title}
    docid = str(row.get("docid") or row.get("docId") or row.get("id") or "").strip()
    if docid:
        item["docid"] = docid
    if row.get("content"):
        item["content"] = strip_markup(str(row.get("content") or ""))
    return item


def fetch_sina_zy_stock_news(symbol: str, limit: int) -> list[dict[str, str]]:
    market, zy_symbol = sina_zy_symbol(symbol)
    payload = client_from_env(news_provider()).stock_news_search(market=market, symbol=zy_symbol, page=1, num=limit)
    data = result_data(payload)
    if isinstance(data, dict):
        rows = data.get("items") or data.get("list") or data.get("data") or []
    elif isinstance(data, list):
        rows = data
    else:
        rows = []
    items: list[dict[str, str]] = []
    for row in rows:
        if isinstance(row, dict):
            item = normalize_api_news_item(row)
            if item:
                items.append(item)
    return items


def load_state() -> dict[str, Any]:
    init_db(DEFAULT_DB_PATH).close()
    with connect_sqlite(DEFAULT_DB_PATH) as conn:
        row = conn.execute("SELECT state_json FROM source_state WHERE source = ?", (STATE_KEY,)).fetchone()
    if not row:
        return {}
    try:
        parsed = json.loads(row[0] or "{}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def save_state(state: dict[str, Any]) -> None:
    with connect_sqlite(DEFAULT_DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO source_state (source, state_json, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(source) DO UPDATE SET state_json = excluded.state_json, updated_at = excluded.updated_at
            """,
            (STATE_KEY, json.dumps(state, ensure_ascii=False, sort_keys=True), utc_now()),
        )
        conn.commit()


def canonical_article_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url.strip())
    if not parsed.scheme or not parsed.netloc:
        return url.strip()
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    ignored_prefixes = ("utm_", "spm")
    ignored_keys = {
        "from",
        "source",
        "cre",
        "mod",
        "loc",
        "r",
        "sec",
        "sudaref",
        "display",
        "retcode",
    }
    kept = [
        (key, value)
        for key, value in query
        if key.lower() not in ignored_keys and not key.lower().startswith(ignored_prefixes)
    ]
    normalized_query = urllib.parse.urlencode(kept, doseq=True)
    return urllib.parse.urlunsplit(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            parsed.path or "/",
            normalized_query,
            "",
        )
    )


def article_event_key(item: dict[str, str]) -> str:
    canonical_url = canonical_article_url(item.get("url", ""))
    if canonical_url:
        return content_hash(SOURCE, canonical_url)[:24]
    return content_hash(SOURCE, item.get("published_at", ""), item.get("title", ""))[:24]


def normalize_dedupe_title(title: str) -> str:
    value = normalize_keyword_text(title)
    value = re.sub(r"\d+(?:\.\d+)?%?", "", value)
    value = re.sub(r"[A-Z]{2,}", "", value)
    for token in (
        "相关",
        "产品",
        "销售",
        "已经",
        "已有",
        "距高位",
        "提示",
        "公司",
        "股价",
        "上涨",
        "下跌",
        "两天",
        "三天",
        "连续",
        "明显",
        "趋势",
        "存在",
        "风险",
    ):
        value = value.replace(token, "")
    value = re.sub(r"[^\u4e00-\u9fffA-Z0-9]+", "", value)
    return value


def char_bigrams(value: str) -> set[str]:
    if len(value) < 2:
        return {value} if value else set()
    return {value[index : index + 2] for index in range(len(value) - 1)}


def similar_news_title(left: str, right: str) -> bool:
    left_norm = normalize_dedupe_title(left)
    right_norm = normalize_dedupe_title(right)
    if not left_norm or not right_norm:
        return False
    if left_norm in right_norm or right_norm in left_norm:
        return min(len(left_norm), len(right_norm)) >= 8
    left_grams = char_bigrams(left_norm)
    right_grams = char_bigrams(right_norm)
    if not left_grams or not right_grams:
        return False
    overlap = len(left_grams & right_grams)
    smaller_ratio = overlap / max(1, min(len(left_grams), len(right_grams)))
    jaccard = overlap / max(1, len(left_grams | right_grams))
    return smaller_ratio >= 0.65 and jaccard >= 0.45


def parse_published_time(value: str) -> datetime | None:
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(value.strip(), fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def published_close(left: str, right: str, max_hours: int = 48) -> bool:
    left_dt = parse_published_time(left)
    right_dt = parse_published_time(right)
    if not left_dt or not right_dt:
        return True
    return abs((left_dt - right_dt).total_seconds()) <= max_hours * 3600


def source_event_id_for_item(item: dict[str, str], holding: dict[str, Any]) -> str:
    return f"article:{article_event_key(item)}"


def legacy_source_event_id_for_item(item: dict[str, str], holding: dict[str, Any]) -> str:
    symbol = str(holding.get("symbol") or "").upper()
    source_id = content_hash(SOURCE, symbol, item["published_at"], item["title"], item["url"])[:24]
    return f"{symbol}:{source_id}"


def event_exists(source_event_id: str) -> bool:
    with connect_sqlite(DEFAULT_DB_PATH) as conn:
        row = conn.execute(
            "SELECT 1 FROM events WHERE source = ? AND source_event_id = ? LIMIT 1",
            (SOURCE, source_event_id),
        ).fetchone()
    return bool(row)


def find_existing_article_event(item: dict[str, str], holding: dict[str, Any]) -> int | None:
    source_event_id = source_event_id_for_item(item, holding)
    legacy_event_id = legacy_source_event_id_for_item(item, holding)
    canonical_url = canonical_article_url(item.get("url", ""))
    title = item.get("title", "").strip()
    with connect_sqlite(DEFAULT_DB_PATH) as conn:
        row = conn.execute(
            "SELECT id FROM events WHERE source = ? AND source_event_id IN (?, ?) LIMIT 1",
            (SOURCE, source_event_id, legacy_event_id),
        ).fetchone()
        if row:
            return int(row[0])
        if canonical_url:
            row = conn.execute(
                """
                SELECT id FROM events
                WHERE source = ? AND url IN (?, ?)
                ORDER BY id ASC LIMIT 1
                """,
                (SOURCE, canonical_url, item.get("url", "")),
            ).fetchone()
            if row:
                return int(row[0])
        if title:
            rows = conn.execute(
                """
                SELECT id, title, published_at FROM events
                WHERE source = ?
                  AND event_type = 'stock_news'
                  AND substr(published_at, 1, 10) >= date(?, '-2 day')
                  AND substr(published_at, 1, 10) <= date(?, '+1 day')
                ORDER BY id ASC
                """,
                (SOURCE, item.get("published_at", "")[:10], item.get("published_at", "")[:10]),
            ).fetchall()
            for row in rows:
                if str(row[1] or "") == title or (
                    published_close(item.get("published_at", ""), str(row[2] or ""))
                    and similar_news_title(title, str(row[1] or ""))
                ):
                    return int(row[0])
    return None


def merge_holding_into_event(event_id: int, holding: dict[str, Any], *, item: dict[str, str], reason: str) -> None:
    symbol = str(holding.get("symbol") or "").upper()
    name = str(holding.get("name") or "")
    if not symbol:
        return
    with connect_sqlite(DEFAULT_DB_PATH) as conn:
        row = conn.execute(
            "SELECT symbols_json, raw_json FROM events WHERE id = ?",
            (event_id,),
        ).fetchone()
        if not row:
            return
        try:
            symbols = json.loads(row[0] or "[]")
        except json.JSONDecodeError:
            symbols = []
        if not isinstance(symbols, list):
            symbols = []
        try:
            raw = json.loads(row[1] or "{}")
        except json.JSONDecodeError:
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        related = raw.get("related_holdings")
        if not isinstance(related, list):
            related = []
        changed = False
        if symbol not in symbols:
            symbols.append(symbol)
            changed = True
        if not any(isinstance(entry, dict) and entry.get("symbol") == symbol for entry in related):
            related.append(
                {
                    "symbol": symbol,
                    "name": name,
                    "matched_at": utc_now(),
                    "reason": reason,
                    "published_at": item.get("published_at", ""),
                    "url": item.get("url", ""),
                }
            )
            changed = True
        if not changed:
            return
        raw["related_holdings"] = related
        conn.execute(
            "UPDATE events SET symbols_json = ?, raw_json = ? WHERE id = ?",
            (
                json.dumps(symbols, ensure_ascii=False, sort_keys=True),
                json.dumps(raw, ensure_ascii=False, sort_keys=True),
                event_id,
            ),
        )
        conn.commit()


def relevance_cache_key(item: dict[str, str], holding: dict[str, Any]) -> str:
    symbol = str(holding.get("symbol") or "").upper()
    return content_hash("sina_stock_news_relevance", symbol, item["title"], item["url"])[:24]


def event_from_item(
    item: dict[str, str],
    holding: dict[str, Any],
    *,
    relevance_reason: str = "",
    relevance_judgment: dict[str, Any] | None = None,
    article_text: str = "",
    freshness: dict[str, Any] | None = None,
) -> dict[str, Any]:
    symbol = str(holding.get("symbol") or "").upper()
    name = str(holding.get("name") or "")
    title = item["title"]
    url = item["url"]
    published_at = item["published_at"]
    source_event_id = source_event_id_for_item(item, holding)
    summary = f"{name}（{symbol}）相关新闻：{title}"
    full_text = article_text.strip() or summary
    return {
        "source": SOURCE,
        "source_event_id": source_event_id,
        "event_type": "stock_news",
        "title": title,
        "summary": summary,
        "full_text": full_text,
        "url": url,
        "published_at": published_at,
        "symbols": [symbol] if symbol else [],
        "themes": ["新浪财经个股资讯"],
        "raw": {
            "symbol": symbol,
            "name": name,
            "published_at": published_at,
            "url": url,
            "canonical_url": canonical_article_url(url),
            "title": title,
            "relevance_reason": relevance_reason,
            "relevance_judgment": relevance_judgment or {},
            "article_text_fetched": bool(article_text.strip()),
            "freshness": freshness or {"status": "unknown"},
        },
        "content_hash": content_hash(SOURCE, canonical_article_url(url), title, full_text[:2000]),
    }


def item_date(item: dict[str, str]) -> str:
    return item.get("published_at", "")[:10]


def run_once(
    *,
    dry_run: bool = False,
    limit: int | None = None,
    since_date: str = "",
    baseline: bool | None = None,
) -> int:
    init_db(DEFAULT_DB_PATH).close()
    import_holdings(DEFAULT_CONFIG_PATH, DEFAULT_DB_PATH)
    holdings = load_enabled_holdings(DEFAULT_DB_PATH)
    if not holdings:
        print("没有启用的持仓，新浪个股新闻跳过。")
        return 0

    state = load_state()
    loaded_cache = state.get("relevance_cache") if isinstance(state.get("relevance_cache"), dict) else {}
    relevance_cache: dict[str, dict[str, Any]] = dict(loaded_cache or {})
    notify_baseline = os.getenv("SURVEIL_NOTIFY_BASELINE", "").strip() == "1"
    baseline_only = baseline if baseline is not None else (not state.get("initialized") and not notify_baseline)
    per_stock_limit = env_int("SINA_STOCK_NEWS_PER_STOCK_LIMIT", 12, minimum=1)
    timeout = env_int("SINA_STOCK_NEWS_TIMEOUT_SECONDS", 15, minimum=5)
    article_timeout = env_int("SINA_STOCK_NEWS_ARTICLE_TIMEOUT_SECONDS", 15, minimum=5)
    sleep_seconds = env_int("SINA_STOCK_NEWS_SLEEP_SECONDS", 1, minimum=0)
    fetch_articles = env_flag("SINA_STOCK_NEWS_FETCH_ARTICLE_TEXT", True)
    fetch_articles_in_dry_run = env_flag("SINA_STOCK_NEWS_FETCH_ARTICLE_TEXT_DRY_RUN", False)

    processed = 0
    new_count = 0
    matched_count = 0
    filtered_announcements = 0
    filtered_irrelevant = 0
    filtered_ai_generated = 0
    article_fetched_count = 0
    relevance_cache_hits = 0
    relevance_llm_calls = 0
    seen_ids: list[str] = []
    for holding in holdings:
        symbol = str(holding.get("symbol") or "").upper()
        if not symbol:
            continue
        try:
            if news_provider() in {"zy_api", "api", "openapi", "official_api", "zy_mcp", "mcp"}:
                items = fetch_sina_zy_stock_news(symbol, per_stock_limit)
            else:
                html_text = fetch_html(sina_symbol(symbol), timeout=timeout)
                items = parse_news_items(html_text)[:per_stock_limit]
        except Exception as exc:  # noqa: BLE001 - isolate a single stock failure
            print(f"Sina stock news fetch failed {symbol}: {exc}", flush=True)
            continue
        for item in reversed(items):
            if since_date and item_date(item) and item_date(item) < since_date:
                continue
            if is_announcement_like(item, holding):
                filtered_announcements += 1
                if dry_run:
                    print(
                        f"[filtered-announcement] {symbol} {item['published_at']} {item['title']} "
                        f"url={item['url']}",
                        flush=True,
                    )
                continue
            rule_relevant, rule_reason = is_relevant_to_holding(item, holding)
            relevance_judgment: dict[str, Any] | None = None
            if rule_relevant or rule_reason == "excluded_keyword" or not llm_relevance_enabled(dry_run):
                relevant = rule_relevant
                relevance_reason = rule_reason
            else:
                cache_key = relevance_cache_key(item, holding)
                cached = relevance_cache.get(cache_key) if not dry_run else None
                if isinstance(cached, dict):
                    relevance_cache_hits += 1
                    relevant = bool(cached.get("relevant"))
                    relevance_reason = str(cached.get("reason") or "cached_relevance")
                    relevance_judgment = cached.get("judgment") if isinstance(cached.get("judgment"), dict) else None
                else:
                    relevant, relevance_reason, relevance_judgment = check_relevance(item, holding, dry_run=dry_run)
                    if not dry_run:
                        relevance_llm_calls += 1 if relevance_reason.startswith("llm_") else 0
                        relevance_cache[cache_key] = {
                            "relevant": relevant,
                            "reason": relevance_reason,
                            "judgment": relevance_judgment or {},
                            "updated_at": utc_now(),
                        }
            if not relevant:
                filtered_irrelevant += 1
                if dry_run:
                    print(
                        f"[filtered-irrelevant:{relevance_reason}] {symbol} {item['published_at']} "
                        f"{item['title']} url={item['url']}",
                        flush=True,
                    )
                continue
            matched_count += 1
            source_event_id = source_event_id_for_item(item, holding)
            existing_event_id = None if dry_run else find_existing_article_event(item, holding)
            known_event = existing_event_id is not None
            article_text = item.get("content", "")
            freshness = {"status": "unknown"}
            if fetch_articles and not article_text and not known_event and (not dry_run or fetch_articles_in_dry_run):
                try:
                    article_text = fetch_article_text(item["url"], timeout=article_timeout)
                    if article_text:
                        article_fetched_count += 1
                    freshness = freshness_hint(item["published_at"], article_text)
                except Exception as exc:  # noqa: BLE001 - article body is useful but non-critical
                    if "sina_ai_generated_content" in str(exc):
                        filtered_ai_generated += 1
                        if dry_run:
                            print(
                                f"[filtered-ai-generated] {symbol} {item['published_at']} "
                                f"{item['title']} url={item['url']}",
                                flush=True,
                            )
                        continue
                    freshness = {"status": "fetch_failed", "error": str(exc)}
            event = event_from_item(
                item,
                holding,
                relevance_reason=relevance_reason,
                relevance_judgment=relevance_judgment,
                article_text=article_text,
                freshness=freshness,
            )
            seen_ids.append(event["source_event_id"])
            if baseline_only:
                event["baseline_only"] = True
            if limit is not None and processed >= limit:
                continue
            processed += 1
            if dry_run:
                print(
                    f"[dry-run] {event['source_event_id']} {event['published_at']} "
                    f"{event['title']} url={event['url']}",
                    flush=True,
                )
                continue
            if existing_event_id is not None:
                merge_holding_into_event(
                    existing_event_id,
                    holding,
                    item=item,
                    reason=relevance_reason,
                )
                print(f"seen article event #{existing_event_id}: {event['title']}", flush=True)
                continue
            event_id, inserted = upsert_event(event, DEFAULT_DB_PATH)
            if not inserted:
                merge_holding_into_event(event_id, holding, item=item, reason=relevance_reason)
                print(f"seen event #{event_id}: {event['title']}", flush=True)
                continue
            new_count += 1
            if baseline_only:
                print(f"baseline event #{event_id}: {event['title']}", flush=True)
                continue
            print(f"new event #{event_id}: {event['title']}", flush=True)
            analysis = analyze_event(event_id, task="sina_stock_news_portfolio", db_path=DEFAULT_DB_PATH)
            print(f"analysis #{event_id}: {analysis.get('core_content', '')}", flush=True)
            status = maybe_deliver_event(event_id, analysis, db_path=DEFAULT_DB_PATH)
            print(f"delivery #{event_id}: {status}", flush=True)
        if sleep_seconds:
            time.sleep(sleep_seconds)

    if not dry_run:
        save_state(
            {
                "initialized": True,
                "last_run_at": utc_now(),
                "last_event_ids": seen_ids[:300],
                "since_date": since_date,
                "relevance_cache": dict(list(relevance_cache.items())[-1000:]),
            }
        )
    print(
        f"Sina stock news finished: matched={matched_count}, processed={processed}, "
        f"new={new_count}, filtered_announcements={filtered_announcements}, "
        f"filtered_irrelevant={filtered_irrelevant}, filtered_ai_generated={filtered_ai_generated}, "
        f"article_fetched={article_fetched_count}, "
        f"relevance_cache_hits={relevance_cache_hits}, relevance_llm_calls={relevance_llm_calls}, "
        f"baseline={baseline_only}",
        flush=True,
    )
    return new_count


def main() -> int:
    parser = argparse.ArgumentParser(description="新浪财经持仓个股资讯低频监控")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--deliver", action="store_true", help="兼容 systemd 调用；实际由事件重要性决定是否发送")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--since-date", default=os.getenv("SINA_STOCK_NEWS_SINCE_DATE", ""))
    parser.add_argument("--baseline", action="store_true", help="建立基线，不推送")
    parser.add_argument("--no-baseline", action="store_true", help="即使首次运行也分析新事件")
    args = parser.parse_args()

    load_env(ROOT / ".env")
    baseline: bool | None = None
    if args.baseline:
        baseline = True
    if args.no_baseline:
        baseline = False
    run_once(dry_run=args.dry_run, limit=args.limit, since_date=args.since_date, baseline=baseline)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
