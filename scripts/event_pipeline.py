"""Unified event ingestion and analysis helpers."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from db_utils import connect_sqlite
from feishu import send_card_with_response
from llm_analysis import call_chat_completion_with_prompts, format_llm_analysis
from market_db import DEFAULT_DB_PATH, init_db


PORTFOLIO_EVENT_SYSTEM_PROMPT = """你是 A 股持仓监控系统的投研与风控助理。
任务：阅读一条公告、研报、快讯或异动信息，判断它对用户持仓和相关 A 股/美股标的的股价影响。
要求：
- 只输出 JSON，不要 Markdown，不要输出 JSON 外解释。
- 不要给无条件买入/卖出指令，只能输出研究信号、观察建议和风险提示。
- 必须判断重要性：high/medium/low。只有可能对股价有中高影响的信息才建议推送。
- 必须判断信息属性：增量利好/增量利空/已有预期/符合预期/利好落地/利空落地/可能利好出尽/可能利空出尽/中性信息/无法判断。
- 必须判断股价预期方向、影响幅度、持续时间，以及是一次性还是持续性影响。
- 如果只是异常波动公告、常规投资者关系活动记录、例行披露、没有新增订单/业绩/价格/产能/政策/指引变化，倾向判为已有预期、中性信息或低重要性。
- 如果原文不足以支持具体股票影响，明确写“暂无明确直接标的”，不要硬凑。
- 涉及上市公司时，写简称、代码、公司全称或上市地；A 股/美股最多各列 3 个最直接标的。
- 要区分直接影响、间接影响、情绪影响和产业链扩散，不要把无关内容机械映射到热门主题。
- 对利好/利空都要说明是否可能已被市场定价、是否存在利好出尽或利空落地风险。
- 对 `sina_stock_news` 尤其要区分“文章发布时间”和“事件首次披露/市场首次知道的时间”。如果输入 raw.freshness 显示 `stale_or_rehash`、`possibly_stale`，或正文提到更早日期已经公告/报道/股价已大涨大跌，除非文章提供新的订单、业绩、价格、产能、监管或经营事实，否则必须倾向判为“已有预期/已定价/低重要性”，不要判为增量利好或增量利空。
"""


PORTFOLIO_EVENT_USER_PROMPT = """请分析以下持仓事件，并输出 JSON。

输出字段：
{
  "importance": "high/medium/low",
  "core_content": "一句到两句中文核心内容",
  "themes": ["主题1", "主题2"],
  "related_holdings": [
    {
      "name": "持仓简称",
      "code": "持仓代码",
      "relation": "直接相关/同行相关/上下游相关/竞争相关/主题相关/无明确关系",
      "impact_direction": "positive/negative/neutral/uncertain",
      "impact_magnitude": "高/中/低/无法判断",
      "reason": "对该持仓的影响理由"
    }
  ],
  "incremental_view": {
    "classification": "增量利好/增量利空/已有预期/符合预期/利好落地/利空落地/可能利好出尽/可能利空出尽/中性信息/无法判断",
    "surprise_level": "高/中/低/无法判断",
    "priced_in": "大概率已定价/部分定价/尚未充分定价/无法判断",
    "reason": "说明新增信息是什么、是否超预期、是否只是落地或重复已知预期"
  },
  "price_impact": {
    "direction": "上涨/下跌/震荡或中性/无法判断",
    "magnitude": "高/中/低/无法判断",
    "duration": "盘中/数日/数周到数月/季度以上/无法判断",
    "persistence": "一次性/阶段性持续/长期持续/无法判断",
    "reason": "股价方向、持续性的判断依据"
  },
  "initial_impact": "初步影响判断",
  "a_share": {
    "positive": [
      {
        "name": "简称",
        "code": "代码",
        "full_name": "公司全称",
        "listing": "上市地",
        "reason": "为什么直接受益",
        "impact_magnitude": "高/中/低/无法判断",
        "duration": "盘中/数日/数周到数月/季度以上/无法判断",
        "persistence": "一次性/阶段性持续/长期持续/无法判断",
        "confidence": "高/中/低"
      }
    ],
    "negative": [
      {
        "name": "简称",
        "code": "代码",
        "full_name": "公司全称",
        "listing": "上市地",
        "reason": "为什么直接承压",
        "impact_magnitude": "高/中/低/无法判断",
        "duration": "盘中/数日/数周到数月/季度以上/无法判断",
        "persistence": "一次性/阶段性持续/长期持续/无法判断",
        "confidence": "高/中/低"
      }
    ]
  },
  "global_equity": {
    "positive": [],
    "negative": []
  },
  "tracking_points": ["后续跟踪点1", "后续跟踪点2"],
  "risks": ["风险1", "风险2"],
  "watchlist_view": "是否值得继续观察及理由",
  "push_decision": {
    "should_push": true,
    "reason": "为什么应该/不应该第一时间推送"
  }
}

输入：
{content}
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def content_hash(*parts: str) -> str:
    joined = "\n".join(part.strip() for part in parts if part and part.strip())
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def load_enabled_holdings(db_path: Path = DEFAULT_DB_PATH) -> list[dict[str, Any]]:
    init_db(db_path).close()
    with connect_sqlite(db_path) as conn:
        rows = conn.execute(
            """
            SELECT symbol, name, full_name, aliases_json, raw_json
            FROM portfolio_holdings
            WHERE enabled = 1
            ORDER BY symbol
            """
        ).fetchall()
    holdings = []
    for symbol, name, full_name, aliases_json, raw_json in rows:
        try:
            aliases = json.loads(aliases_json or "[]")
        except json.JSONDecodeError:
            aliases = []
        try:
            raw = json.loads(raw_json or "{}")
        except json.JSONDecodeError:
            raw = {}
        holdings.append(
            {
                "symbol": symbol,
                "name": name,
                "full_name": full_name or "",
                "aliases": aliases,
                "news_keywords": raw.get("news_keywords") if isinstance(raw.get("news_keywords"), list) else [],
                "news_exclude_keywords": raw.get("news_exclude_keywords")
                if isinstance(raw.get("news_exclude_keywords"), list)
                else [],
                "business_summary": str(raw.get("business_summary") or ""),
                "raw": raw,
            }
        )
    return holdings


def upsert_event(event: dict[str, Any], db_path: Path = DEFAULT_DB_PATH) -> tuple[int, bool]:
    """Insert an event and return (event_id, inserted)."""
    init_db(db_path).close()
    now = utc_now()
    source = str(event["source"])
    source_event_id = str(event["source_event_id"])
    title = str(event.get("title") or "").strip()
    summary = str(event.get("summary") or "").strip()
    full_text = str(event.get("full_text") or "").strip()
    digest = event.get("content_hash") or content_hash(source, source_event_id, title, summary, full_text)
    payload = (
        source,
        source_event_id,
        str(event.get("event_type") or "unknown"),
        title,
        summary,
        full_text,
        str(event.get("url") or ""),
        str(event.get("published_at") or ""),
        now,
        json_dumps(event.get("symbols") or []),
        json_dumps(event.get("themes") or []),
        json_dumps(event.get("raw") or {}),
        digest,
        1 if event.get("baseline_only") else 0,
    )
    with connect_sqlite(db_path) as conn:
        try:
            cur = conn.execute(
                """
                INSERT INTO events (
                    source, source_event_id, event_type, title, summary, full_text, url,
                    published_at, first_seen_at, symbols_json, themes_json, raw_json,
                    content_hash, baseline_only
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                payload,
            )
            conn.commit()
            return int(cur.lastrowid), True
        except sqlite3.IntegrityError:
            row = conn.execute(
                "SELECT id, full_text FROM events WHERE source = ? AND source_event_id = ?",
                (source, source_event_id),
            ).fetchone()
            if not row:
                raise
            event_id = int(row[0])
            existing_full_text = str(row[1] or "")
            if full_text and len(full_text) > len(existing_full_text):
                conn.execute(
                    """
                    UPDATE events
                    SET summary = ?, full_text = ?, raw_json = ?, content_hash = ?
                    WHERE id = ?
                    """,
                    (summary, full_text, json_dumps(event.get("raw") or {}), digest, event_id),
                )
                conn.commit()
            return event_id, False


def analyze_event(event_id: int, task: str = "portfolio_event", db_path: Path = DEFAULT_DB_PATH) -> dict[str, Any]:
    with connect_sqlite(db_path) as conn:
        row = conn.execute(
            """
            SELECT source, event_type, title, summary, full_text, url, published_at, symbols_json, raw_json
            FROM events
            WHERE id = ?
            """,
            (event_id,),
        ).fetchone()
        if not row:
            raise RuntimeError(f"事件不存在：{event_id}")
        source, event_type, title, summary, full_text, url, published_at, symbols_json, raw_json = row
        existing = conn.execute(
            "SELECT id, analysis_json FROM event_analyses WHERE event_id = ? AND task = ? ORDER BY id DESC LIMIT 1",
            (event_id, task),
        ).fetchone()
        if existing:
            return json.loads(existing[1])

    text = build_portfolio_event_input(
        {
            "source": source,
            "event_type": event_type,
            "title": title,
            "published_at": published_at,
            "url": url,
            "summary": summary,
            "full_text": full_text,
            "symbols_json": symbols_json,
            "raw_json": raw_json,
        },
        db_path=db_path,
    )
    parsed, model = call_chat_completion_with_prompts(
        PORTFOLIO_EVENT_SYSTEM_PROMPT,
        PORTFOLIO_EVENT_USER_PROMPT.replace("{content}", text),
        user_agent="surveil-portfolio-event-llm/0.1",
    )
    parsed["_model"] = model
    importance = infer_importance(parsed)
    classification = ""
    incremental = parsed.get("incremental_view")
    if isinstance(incremental, dict):
        classification = str(incremental.get("classification") or "")
    direction = ""
    impact_duration = ""
    price_impact = parsed.get("price_impact")
    if isinstance(price_impact, dict):
        direction = str(price_impact.get("direction") or "")
        impact_duration = str(price_impact.get("duration") or "")
    should_push = 1 if should_push_analysis(parsed, importance) else 0
    with connect_sqlite(db_path) as conn:
        conn.execute(
            """
            INSERT INTO event_analyses (
                event_id, task, model, importance, classification, direction,
                impact_duration, should_push, analysis_json, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                task,
                model,
                importance,
                classification,
                direction,
                impact_duration,
                should_push,
                json_dumps(parsed),
                utc_now(),
            ),
        )
        conn.commit()
    return parsed


def build_portfolio_event_input(event: dict[str, Any], db_path: Path = DEFAULT_DB_PATH) -> str:
    try:
        symbols = json.loads(str(event.get("symbols_json") or "[]"))
    except json.JSONDecodeError:
        symbols = []
    symbol_set = {str(symbol).upper() for symbol in symbols if str(symbol).strip()}
    holdings = load_enabled_holdings(db_path)
    related_holdings = [holding for holding in holdings if str(holding.get("symbol", "")).upper() in symbol_set]
    context = {
        "event": event,
        "event_symbols": sorted(symbol_set),
        "directly_related_holdings": related_holdings,
        "all_configured_holdings": [
            {
                "symbol": holding.get("symbol", ""),
                "name": holding.get("name", ""),
                "full_name": holding.get("full_name", ""),
                "aliases": holding.get("aliases", []),
            }
            for holding in holdings
        ],
    }
    return json.dumps(context, ensure_ascii=False, indent=2)


def infer_importance(parsed: dict[str, Any]) -> str:
    explicit = str(parsed.get("importance") or parsed.get("importance_level") or "").strip()
    if explicit:
        return explicit
    incremental = parsed.get("incremental_view")
    classification = ""
    surprise = ""
    if isinstance(incremental, dict):
        classification = str(incremental.get("classification") or "")
        surprise = str(incremental.get("surprise_level") or "")
    if "增量利好" in classification or "增量利空" in classification:
        return "high" if surprise == "高" else "medium"
    if "无法判断" in classification:
        return "low"
    return "medium" if parsed.get("a_share") or parsed.get("global_equity") else "low"


def normalize_importance(value: str) -> str:
    normalized = value.strip().lower()
    mapping = {
        "高": "high",
        "重要": "high",
        "中": "medium",
        "中等": "medium",
        "低": "low",
        "不重要": "low",
    }
    return mapping.get(normalized, normalized)


def should_push_analysis(parsed: dict[str, Any], importance: str | None = None) -> bool:
    normalized = normalize_importance(str(importance or infer_importance(parsed)))
    push_decision = parsed.get("push_decision")
    if isinstance(push_decision, dict) and "should_push" in push_decision:
        raw = push_decision.get("should_push")
        if isinstance(raw, bool):
            return raw and normalized in {"high", "medium"}
        if isinstance(raw, str):
            wants_push = raw.strip().lower() in {"true", "yes", "1", "y", "是", "推送"}
            return wants_push and normalized in {"high", "medium"}
        return bool(raw) and normalized in {"high", "medium"}
    if normalized in {"high", "medium"}:
        return True
    return False


def maybe_deliver_event(event_id: int, analysis: dict[str, Any], db_path: Path = DEFAULT_DB_PATH) -> str:
    """Deliver when Feishu is configured; otherwise record a skipped delivery."""
    if not should_push_analysis(analysis):
        record_delivery(event_id, "feishu", "skipped", {"reason": "LLM 判断重要性低，不推送"}, db_path=db_path)
        return "skipped"
    webhook = os.getenv("FEISHU_WEBHOOK", "").strip()
    if not webhook:
        record_delivery(event_id, "feishu", "skipped", {"reason": "FEISHU_WEBHOOK 未配置"}, db_path=db_path)
        return "skipped"
    with connect_sqlite(db_path) as conn:
        row = conn.execute(
            "SELECT source, title, summary, full_text, url, published_at FROM events WHERE id = ?",
            (event_id,),
        ).fetchone()
    if not row:
        raise RuntimeError(f"事件不存在：{event_id}")
    source, title, summary, full_text, url, published_at = row
    lines = format_llm_analysis(analysis, str(analysis.get("_model") or "llm"))
    card = simple_event_card(source, title, summary or full_text, url, published_at, lines)
    try:
        response = send_card_with_response(card)
    except Exception as exc:  # noqa: BLE001 - keep delivery failures isolated
        record_delivery(
            event_id,
            "feishu",
            "failed",
            {
                "error": str(exc),
                "webhook_fingerprint": feishu_webhook_fingerprint(),
            },
            error=str(exc),
            db_path=db_path,
        )
        return "failed"
    status = "sent" if response.ok else "skipped"
    record_delivery(
        event_id,
        "feishu",
        status,
        {
            "title": title,
            "webhook_fingerprint": feishu_webhook_fingerprint(),
            "feishu_code": response.code,
            "feishu_message": response.message,
            "feishu_body": response.body[:1000],
        },
        db_path=db_path,
    )
    return status


def feishu_webhook_fingerprint() -> str:
    webhook = os.getenv("FEISHU_WEBHOOK", "").strip()
    if not webhook:
        return ""
    return hashlib.sha256(webhook.encode("utf-8")).hexdigest()[:12]


def record_delivery(
    event_id: int,
    channel: str,
    status: str,
    payload: dict[str, Any],
    *,
    error: str = "",
    db_path: Path = DEFAULT_DB_PATH,
) -> None:
    with connect_sqlite(db_path) as conn:
        conn.execute(
            """
            INSERT INTO deliveries (event_id, channel, status, sent_at, error, payload_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (event_id, channel, status, utc_now() if status == "sent" else "", error, json_dumps(payload)),
        )
        conn.commit()


def simple_event_card(
    source: str,
    title: str,
    text: str,
    url: str,
    published_at: str,
    analysis_lines: list[str],
) -> dict[str, Any]:
    from cards import div_markdown, md_escape, text_chunks

    elements: list[dict[str, Any]] = [
        div_markdown(f"**来源**：{md_escape(source)}"),
        div_markdown(f"**发布时间**：{md_escape(published_at or '未知')}"),
        div_markdown(f"**标题**\n{md_escape(title)}"),
    ]
    for index, chunk in enumerate(text_chunks(text or "", limit=1000), start=1):
        label = "原文/摘要" if index == 1 else f"原文/摘要（续 {index}）"
        elements.append(div_markdown(f"**{label}**\n{md_escape(chunk)}"))
    elements.append(div_markdown("**模型解读**\n" + md_escape("\n".join(analysis_lines))))
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
        "header": {"template": "blue", "title": {"tag": "plain_text", "content": title[:60] or source}},
        "elements": elements,
    }
