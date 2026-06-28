#!/usr/bin/env python3
"""Monitor configured portfolio holdings for important company events."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from cards import div_markdown, format_time, md_escape, now_beijing
from db_utils import connect_sqlite, retry_on_locked
from feishu import send_card
from llm_analysis import call_chat_completion, format_llm_analysis
from x_check import load_env


ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / ".env"
CONFIG_PATH = ROOT / "config" / "portfolio.json"
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "surveil.sqlite3"

CNINFO_QUERY_URL = "https://www.cninfo.com.cn/new/hisAnnouncement/query"
CNINFO_STOCK_URLS = (
    "https://www.cninfo.com.cn/new/data/szse_stock.json",
    "https://www.cninfo.com.cn/new/data/bj_stock.json",
)
SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"

IMPORTANT_TITLE_RE = re.compile(
    r"业绩|年度报告|半年度报告|季度报告|业绩预告|业绩快报|利润分配|投资者关系|调研|"
    r"解禁|限售|减持|增持|回购|重大事项|重大合同|订单|中标|诉讼|处罚|问询|澄清|停牌|复牌|"
    r"earnings|guidance|results|10-k|10-q|8-k|13d|13g|144",
    re.I,
)


PORTFOLIO_SYSTEM_PROMPT = """你是面向持仓股的实时风控和投研助理。
任务：判断输入事件是否会显著影响相关股票价格，并给出中文分析。
必须回答：
- 是否重要，重要性高/中/低；
- 对股价方向：上涨/下跌/中性/不确定；
- 是增量利好、增量利空、利好出尽、利空出尽、符合预期还是噪声；
- 影响幅度：高/中/低/无法判断；
- 持续时间：盘中/数日/数周到数月/季度以上/无法判断；
- 是一次性影响还是阶段性/长期持续影响；
- A 股和美股相关标的最多各 3 个；
- 给出跟踪点、风险和反证。
不要输出无条件买入/卖出指令。只输出 JSON。
"""


PORTFOLIO_USER_PROMPT = """请分析这个持仓相关事件：

持仓：
{holding}

事件：
{event}

请输出 JSON，字段：
{
  "core_content": "事件核心内容",
  "themes": ["主题1", "主题2"],
  "incremental_view": {
    "classification": "增量利好/增量利空/符合预期/可能利好出尽/可能利空出尽/中性信息/噪声/无法判断",
    "surprise_level": "高/中/低/无法判断",
    "priced_in": "大概率已定价/部分定价/尚未充分定价/无法判断",
    "reason": "判断理由"
  },
  "price_impact": {
    "direction": "上涨/下跌/中性/不确定",
    "magnitude": "高/中/低/无法判断",
    "duration": "盘中/数日/数周到数月/季度以上/无法判断",
    "persistence": "一次性/阶段性持续/长期持续/无法判断",
    "reason": "为什么会这样影响股价"
  },
  "initial_impact": "一句话初步影响",
  "a_share": {"positive": [], "negative": []},
  "global_equity": {"positive": [], "negative": []},
  "tracking_points": ["后续跟踪点"],
  "risks": ["风险或反证"],
  "watchlist_view": "对持仓的观察/处理建议，不能写无条件买卖"
}
"""


@dataclass(frozen=True)
class Holding:
    symbol: str
    name: str
    market: str
    enabled: bool
    raw: dict[str, Any]


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    if not path.exists():
        return {"poll_interval_seconds": 120, "lookback_days": 7, "holdings": []}
    return json.loads(path.read_text(encoding="utf-8"))


def holdings_from_config(config: dict[str, Any]) -> list[Holding]:
    holdings = []
    for item in config.get("holdings", []):
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol") or "").strip()
        if not symbol:
            continue
        holdings.append(
            Holding(
                symbol=symbol,
                name=str(item.get("name") or symbol).strip(),
                market=str(item.get("market") or infer_market(symbol)).upper(),
                enabled=bool(item.get("enabled", True)),
                raw=item,
            )
        )
    return holdings


def infer_market(symbol: str) -> str:
    normalized = symbol.upper()
    if normalized.endswith((".SZ", ".SH", ".BJ")):
        return "CN"
    if re.fullmatch(r"\d{6}", normalized):
        return "CN"
    return "US"


def normalize_cn_symbol(symbol: str) -> tuple[str, str]:
    upper = symbol.upper()
    code = upper.split(".", 1)[0]
    if upper.endswith(".SH") or code.startswith(("5", "6", "9")):
        return code, "sse"
    if upper.endswith(".BJ") or code.startswith(("8", "9")):
        return code, "bj"
    return code, "szse"


def infer_cninfo_org_id(code: str, exchange: str) -> str:
    if exchange == "sse":
        return f"gssh0{code}"
    if exchange == "bj":
        return f"gfbj0{code}"
    return f"gssz0{code}"


def connect_db() -> sqlite3.Connection:
    conn = connect_sqlite(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS portfolio_seen_events (
            event_id TEXT PRIMARY KEY,
            holding_symbol TEXT NOT NULL,
            source TEXT NOT NULL,
            title TEXT NOT NULL,
            url TEXT,
            published_at TEXT,
            first_seen_at TEXT NOT NULL
        )
        """
    )
    return conn


def event_id(source: str, holding: Holding, raw_id: str, title: str, url: str) -> str:
    stable = "|".join([source, holding.symbol, raw_id or title, url])
    return hashlib.sha256(stable.encode("utf-8")).hexdigest()[:32]


def save_new_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def operation() -> list[dict[str, Any]]:
        new_events = []
        now = datetime.now(timezone.utc).isoformat()
        with connect_db() as conn:
            for event in events:
                try:
                    conn.execute(
                        """
                        INSERT INTO portfolio_seen_events (
                            event_id, holding_symbol, source, title, url, published_at, first_seen_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            event["id"],
                            event["holding_symbol"],
                            event["source"],
                            event["title"],
                            event.get("url", ""),
                            event.get("published_at", ""),
                            now,
                        ),
                    )
                except sqlite3.IntegrityError:
                    continue
                new_events.append(event)
        return new_events

    return retry_on_locked(operation)


def cninfo_headers() -> dict[str, str]:
    return {
        "User-Agent": "Mozilla/5.0 surveil-portfolio-monitor/0.1",
        "Referer": "https://www.cninfo.com.cn/new/commonUrl/pageOfSearch?url=disclosure/list/search",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Accept": "application/json,text/javascript,*/*;q=0.01",
    }


def cninfo_query(holding: Holding, lookback_days: int) -> list[dict[str, Any]]:
    code, exchange = normalize_cn_symbol(holding.symbol)
    org_id = str(holding.raw.get("cninfo_org_id") or infer_cninfo_org_id(code, exchange))
    column = "sse" if exchange == "sse" else "szse"
    end = datetime.now().date()
    start = end - timedelta(days=lookback_days)
    params = {
        "pageNum": "1",
        "pageSize": "30",
        "column": column,
        "tabName": "fulltext",
        "plate": "",
        "stock": f"{code},{org_id}",
        "searchkey": "",
        "secid": "",
        "category": "",
        "trade": "",
        "seDate": f"{start:%Y-%m-%d}~{end:%Y-%m-%d}",
        "sortName": "",
        "sortType": "",
        "isHLtitle": "true",
    }
    request = urllib.request.Request(
        CNINFO_QUERY_URL,
        data=urllib.parse.urlencode(params).encode("utf-8"),
        headers=cninfo_headers(),
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=25) as response:
        payload = json.loads(response.read().decode("utf-8", errors="replace"))
    events = []
    for item in payload.get("announcements") or []:
        title = clean_html(str(item.get("announcementTitle") or ""))
        url = urllib.parse.urljoin("https://www.cninfo.com.cn/new/disclosure/detail?", str(item.get("adjunctUrl") or ""))
        adjunct_url = str(item.get("adjunctUrl") or "")
        if adjunct_url:
            url = f"https://static.cninfo.com.cn/{adjunct_url}"
        published_at = timestamp_ms_to_iso(item.get("announcementTime"))
        raw_id = str(item.get("announcementId") or item.get("adjunctUrl") or title)
        event = {
            "id": event_id("cninfo_announcement", holding, raw_id, title, url),
            "source": "cninfo_announcement",
            "source_display": "巨潮资讯公告",
            "holding_symbol": holding.symbol,
            "holding_name": holding.name,
            "title": title,
            "url": url,
            "published_at": published_at,
            "summary": title,
            "raw": item,
        }
        if should_keep_event(holding, event):
            events.append(event)
    return events


def clean_html(value: str) -> str:
    value = re.sub(r"<[^>]+>", "", value)
    return value.replace("&nbsp;", " ").strip()


def timestamp_ms_to_iso(value: Any) -> str:
    try:
        return datetime.fromtimestamp(int(value) / 1000, timezone.utc).isoformat()
    except Exception:
        return ""


def sec_headers() -> dict[str, str]:
    return {
        "User-Agent": os.getenv("SEC_USER_AGENT", "surveil-portfolio-monitor contact@example.com"),
        "Accept": "application/json",
    }


def sec_query(holding: Holding, lookback_days: int) -> list[dict[str, Any]]:
    cik = str(holding.raw.get("cik") or "").strip().lstrip("0")
    if not cik:
        return []
    cik_padded = cik.zfill(10)
    request = urllib.request.Request(SEC_SUBMISSIONS_URL.format(cik=cik_padded), headers=sec_headers())
    with urllib.request.urlopen(request, timeout=25) as response:
        payload = json.loads(response.read().decode("utf-8", errors="replace"))
    recent = payload.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    accessions = recent.get("accessionNumber", [])
    docs = recent.get("primaryDocument", [])
    accepted = recent.get("acceptanceDateTime", [])
    wanted_forms = {str(form).upper() for form in holding.raw.get("sec_forms", ["10-K", "10-Q", "8-K"]) }
    cutoff = datetime.now().date() - timedelta(days=lookback_days)
    events = []
    for index, form in enumerate(forms):
        form_upper = str(form).upper()
        filing_date = str(dates[index] if index < len(dates) else "")
        try:
            if datetime.fromisoformat(filing_date).date() < cutoff:
                continue
        except ValueError:
            pass
        if wanted_forms and form_upper not in wanted_forms:
            continue
        accession = str(accessions[index] if index < len(accessions) else "")
        doc = str(docs[index] if index < len(docs) else "")
        accession_nodash = accession.replace("-", "")
        url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession_nodash}/{doc}" if accession and doc else ""
        title = f"{holding.name} {form_upper} filing"
        event = {
            "id": event_id("sec_filing", holding, accession, title, url),
            "source": "sec_filing",
            "source_display": "SEC EDGAR filing",
            "holding_symbol": holding.symbol,
            "holding_name": holding.name,
            "title": title,
            "url": url,
            "published_at": str(accepted[index] if index < len(accepted) else filing_date),
            "summary": f"{holding.name} filed {form_upper} on {filing_date}.",
            "raw": {"form": form_upper, "filingDate": filing_date, "accessionNumber": accession},
        }
        if should_keep_event(holding, event):
            events.append(event)
    return events


def should_keep_event(holding: Holding, event: dict[str, Any]) -> bool:
    text = " ".join([event.get("title", ""), event.get("summary", "")])
    keywords = [str(item) for item in holding.raw.get("keywords", []) if str(item).strip()]
    if any(keyword.lower() in text.lower() for keyword in keywords):
        return True
    return IMPORTANT_TITLE_RE.search(text) is not None


def fetch_events_for_holding(holding: Holding, lookback_days: int) -> list[dict[str, Any]]:
    if not holding.enabled:
        return []
    if holding.market == "CN":
        return cninfo_query(holding, lookback_days)
    if holding.market == "US":
        return sec_query(holding, lookback_days)
    return []


def analyze_portfolio_event(event: dict[str, Any], holding: Holding) -> list[str]:
    holding_text = json.dumps(
        {
            "symbol": holding.symbol,
            "name": holding.name,
            "market": holding.market,
            "position_note": holding.raw.get("position_note", ""),
            "keywords": holding.raw.get("keywords", []),
        },
        ensure_ascii=False,
    )
    event_text = json.dumps(
        {
            "source": event.get("source_display") or event.get("source"),
            "title": event.get("title"),
            "published_at": event.get("published_at"),
            "summary": event.get("summary"),
            "url": event.get("url"),
        },
        ensure_ascii=False,
    )
    parsed, model = call_chat_completion_with_portfolio_prompt(holding_text, event_text)
    return format_portfolio_analysis(parsed, model)


def call_chat_completion_with_portfolio_prompt(holding_text: str, event_text: str) -> tuple[dict[str, Any], str]:
    from llm_analysis import chat_completions_url, llm_config, parse_json_object, timeout_seconds

    config = llm_config()
    if not config:
        raise RuntimeError("LLM 未配置")
    api_key, base_url, model = config
    prompt = PORTFOLIO_USER_PROMPT.replace("{holding}", holding_text).replace("{event}", event_text)
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": PORTFOLIO_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
    }
    request = urllib.request.Request(
        chat_completions_url(base_url),
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json",
            "User-Agent": "surveil-portfolio-llm/0.1",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds()) as response:
            body = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"组合事件 LLM 请求失败：HTTP {exc.code}\n{body}") from exc
    result = json.loads(body)
    raw = str((result.get("choices") or [{}])[0].get("message", {}).get("content") or "")
    return parse_json_object(raw), model


def format_portfolio_analysis(parsed: dict[str, Any], model: str) -> list[str]:
    lines = format_llm_analysis(parsed, model)
    price = parsed.get("price_impact") or {}
    if isinstance(price, dict):
        parts = []
        for label, key in [
            ("方向", "direction"),
            ("幅度", "magnitude"),
            ("持续", "duration"),
            ("性质", "persistence"),
        ]:
            value = str(price.get(key) or "").strip()
            if value:
                parts.append(f"{label}：{value}")
        reason = str(price.get("reason") or "").strip()
        if parts or reason:
            insert_at = 3 if len(lines) > 3 else len(lines)
            lines.insert(insert_at, "股价预期：" + "；".join(parts) + (f"。{reason}" if reason else ""))
    return lines


def fallback_analysis(event: dict[str, Any]) -> list[str]:
    return [
        "【持仓事件解读】",
        f"核心内容：{event.get('title', '')}",
        "增量判断：需要人工复核；当前 LLM 不可用，先按重要事件推送。",
        "股价预期：方向不确定；幅度无法判断；持续时间无法判断。",
        "风险：请结合公告正文、市场预期、估值位置和盘口反应进一步确认。",
    ]


def build_portfolio_card(event: dict[str, Any], holding: Holding, analysis_lines: list[str]) -> dict[str, Any]:
    url = event.get("url", "")
    elements: list[dict[str, Any]] = [
        div_markdown(f"**发送时间**：{md_escape(now_beijing())}"),
        div_markdown(f"**持仓标的**：{md_escape(holding.name)}（{md_escape(holding.symbol)}）"),
        div_markdown(f"**来源**：{md_escape(event.get('source_display') or event.get('source', ''))}"),
        div_markdown(f"**发布时间**：{md_escape(format_time(str(event.get('published_at', ''))))}"),
        {"tag": "hr"},
        div_markdown(f"**标题**\n{md_escape(event.get('title', ''))}"),
    ]
    summary = event.get("summary") or ""
    if summary and summary != event.get("title"):
        elements.append(div_markdown(f"**原文摘要/引用**\n{md_escape(summary)}"))
    elements.extend(
        [
            {"tag": "hr"},
            div_markdown("**持仓影响分析**\n" + md_escape("\n".join(analysis_lines[1:]))),
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
                        "multi_url": {"url": url, "pc_url": url, "ios_url": url, "android_url": url},
                    }
                ],
            }
        )
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "red",
            "title": {"tag": "plain_text", "content": f"持仓重要事件：{holding.name}"},
        },
        "elements": elements,
    }


def notify_event(event: dict[str, Any], holding: Holding) -> None:
    try:
        analysis = analyze_portfolio_event(event, holding)
    except Exception as exc:
        print(f"{holding.symbol} 组合事件 LLM 分析失败，回退：{exc}")
        analysis = fallback_analysis(event)
    send_card(build_portfolio_card(event, holding, analysis))


def run_once(config: dict[str, Any]) -> int:
    lookback_days = int(config.get("lookback_days") or 7)
    all_events: list[dict[str, Any]] = []
    holdings = holdings_from_config(config)
    for holding in holdings:
        if not holding.enabled:
            continue
        try:
            events = fetch_events_for_holding(holding, lookback_days)
        except Exception as exc:
            print(f"{holding.symbol} 事件抓取失败：{exc}")
            continue
        all_events.extend(events)
    new_events = save_new_events(all_events)
    if not new_events:
        print("持仓监控：没有发现新的重要事件。")
        return 0
    by_symbol = {holding.symbol: holding for holding in holdings}
    print(f"持仓监控：发现 {len(new_events)} 条新的重要事件。")
    for event in new_events:
        holding = by_symbol.get(event["holding_symbol"])
        if not holding:
            continue
        print(f"{holding.symbol} {event.get('title')} {event.get('url')}")
        notify_event(event, holding)
    return len(new_events)


def main() -> int:
    load_env(ENV_PATH)
    parser = argparse.ArgumentParser(description="Monitor configured portfolio holdings.")
    parser.add_argument("--config", default=str(CONFIG_PATH))
    parser.add_argument("--interval", type=int, default=0)
    args = parser.parse_args()

    config = load_config(Path(args.config))
    if args.interval <= 0:
        run_once(config)
        return 0
    print(f"开始持仓监控，轮询间隔 {args.interval} 秒。")
    while True:
        try:
            run_once(config)
        except Exception as exc:
            print(f"持仓监控本轮失败：{exc}")
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
