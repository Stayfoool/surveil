"""LLM importance gate for RSS and TrendForce article notifications."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any

from llm_analysis import call_chat_completion_with_prompts, llm_config
from skeptic_evaluator import skeptic_lines


GATE_SYSTEM_PROMPT = """你是半导体、AI 基础设施和二级市场研究助理。
任务：判断一条资讯/报告是否值得第一时间推送给投资者。

重点关注：
- 是否可能显著影响 A 股/美股/海外相关股票未来几个交易日到数月的价格预期。
- 是否是增量利好/增量利空，还是已有预期、利好/利空落地、利好/利空出尽。
- 是否直接涉及持仓股、持仓股上下游/同行/竞争对手，或半导体/AI 强主题。
- 是否有订单、涨价、停产、扩产、客户认证、资本开支、业绩指引、监管、供需缺口等硬变量。
- 是否属于“星际之门/Stargate-like”的超大资本开支事件：政府、云厂商、半导体龙头、AI 基础设施龙头或产业联盟计划投入超大金额建设 AI 数据中心、半导体工厂、HBM/存储产能、先进封装、CPO/光互联、电力/液冷等基础设施。

请克制：
- 只有标题/摘要、缺少量化数据时，不要硬判高重要性。
- 普通月报、价格表、营销、ESG、活动、泛泛趋势，通常不要即时推送。
- 已被市场广泛讨论且没有新增数据的内容，通常进日报。
- 但超大资本开支“预告/据报/拟宣布/将公布”不能因为尚未正式公布就自动降为 medium：只要金额足够重大、主体可信、产业方向明确、且有明确会议/发布时间/高层表态/政策背景，应判为 high、push_now=true，并在 reason 中标注“待确认/预告性质”和需要跟踪的正式公告。

只输出 JSON，不要 Markdown。"""


GATE_USER_PROMPT = """请判断以下内容是否需要第一时间推送，输出 JSON：
{
  "importance": "high/medium/low",
  "push_now": true,
  "market_impact": "是否可能显著影响相关股票价格，以及方向",
  "incremental_classification": "增量利好/增量利空/已有预期/符合预期/利好落地/利空落地/可能利好出尽/可能利空出尽/中性信息/无法判断",
  "affected_targets": ["最相关股票或产业链环节，最多5个"],
  "daily_summary": "如果不即时推送，日报里的一句话摘要",
  "reason": "为什么推或不推，说明是否有硬变量和超预期",
  "confidence": "高/中/低"
}

判定规则：
- push_now 只有在 importance=high 且信息可能显著影响股票预期时才为 true。
- medium/low 默认进入日报，不即时推送。
- 如果信息不足，importance=low 或 medium，push_now=false。
- 对半导体/AI 基础设施“超大资本开支预告”单独处理：即使尚未正式公布，只要金额、主体和产业方向明确，并可能重估设备、材料、存储、光通信、PCB、先进封装、电力、液冷等产业链预期，应 importance=high、push_now=true；同时在 reason 和 daily_summary 中明确写“待确认/预告性质”，列出需要验证的正式公布时间、投资拆分、产能、设备订单和供应链受益方向。

来源：{source}
来源模块：{source_module}
标题：{title}
发布时间：{published_at}
正文/摘要：
{content}
"""


def ensure_article_reviews_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS article_reviews (
            source TEXT NOT NULL,
            item_id TEXT NOT NULL,
            url TEXT,
            title TEXT NOT NULL,
            source_module TEXT,
            published_at TEXT,
            importance TEXT NOT NULL,
            push_now INTEGER NOT NULL DEFAULT 0,
            market_impact TEXT,
            incremental_classification TEXT,
            affected_targets_json TEXT NOT NULL,
            reason TEXT,
            daily_summary TEXT,
            confidence TEXT,
            gate_json TEXT NOT NULL,
            skeptic_json TEXT,
            pre_skeptic_importance TEXT,
            pushed_at TEXT,
            created_at TEXT NOT NULL,
            PRIMARY KEY (source, item_id)
        )
        """
    )
    columns = {row[1] for row in conn.execute("PRAGMA table_info(article_reviews)").fetchall()}
    if "skeptic_json" not in columns:
        conn.execute("ALTER TABLE article_reviews ADD COLUMN skeptic_json TEXT")
    if "pre_skeptic_importance" not in columns:
        conn.execute("ALTER TABLE article_reviews ADD COLUMN pre_skeptic_importance TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_article_reviews_created ON article_reviews(created_at)")
    conn.commit()


def json_loads_dict(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def article_gate_enabled() -> bool:
    if os.getenv("SURVEIL_ARTICLE_GATE", "1").strip() == "0":
        return False
    return llm_config() is not None


def article_item_id(item: dict[str, Any]) -> str:
    return str(item.get("id") or item.get("url") or item.get("title") or "")


def normalize_review(parsed: dict[str, Any]) -> dict[str, Any]:
    importance = str(parsed.get("importance") or "low").strip().lower()
    if importance not in {"high", "medium", "low"}:
        importance = "low"
    push_now = bool(parsed.get("push_now")) and importance == "high"
    targets = parsed.get("affected_targets")
    if not isinstance(targets, list):
        targets = []
    return {
        "importance": importance,
        "push_now": push_now,
        "market_impact": str(parsed.get("market_impact") or "").strip(),
        "incremental_classification": str(parsed.get("incremental_classification") or "").strip(),
        "affected_targets": [str(item).strip() for item in targets if str(item).strip()][:5],
        "daily_summary": str(parsed.get("daily_summary") or "").strip(),
        "reason": str(parsed.get("reason") or "").strip(),
        "confidence": str(parsed.get("confidence") or "").strip(),
        "raw": parsed,
    }


def failed_review(item: dict[str, Any], error: Exception) -> dict[str, Any]:
    reason = str(error).strip()
    if len(reason) > 500:
        reason = reason[:497] + "..."
    return {
        "importance": "low",
        "push_now": False,
        "market_impact": "门控模型失败，无法判断是否显著影响股价。",
        "incremental_classification": "无法判断",
        "affected_targets": [],
        "daily_summary": str(item.get("title") or "门控失败条目"),
        "reason": f"门控模型失败：{reason}",
        "confidence": "低",
        "raw": {"error": reason},
        "model": "gate_failed",
    }


def review_article(source: str, item: dict[str, Any]) -> dict[str, Any]:
    text = str(item.get("full_text") or item.get("content") or item.get("summary") or "").strip()
    user_prompt = (
        GATE_USER_PROMPT.replace("{source}", source)
        .replace("{source_module}", str(item.get("source_module") or item.get("source_display") or ""))
        .replace("{title}", str(item.get("title") or ""))
        .replace("{published_at}", str(item.get("published_at") or ""))
        .replace("{content}", text[:6000])
    )
    parsed, model = call_chat_completion_with_prompts(
        GATE_SYSTEM_PROMPT,
        user_prompt,
        user_agent="surveil-article-gate/0.1",
        truncate_user_prompt=False,
        thinking_override=os.getenv("LLM_GATE_THINKING_TYPE", "enabled"),
        max_tokens_override=int(os.getenv("LLM_GATE_MAX_OUTPUT_TOKENS", "1400")),
    )
    review = normalize_review(parsed)
    review["model"] = model
    return review


def save_review(conn: sqlite3.Connection, source: str, item: dict[str, Any], review: dict[str, Any]) -> None:
    ensure_article_reviews_table(conn)
    now = datetime.now(timezone.utc).isoformat()
    item_id = article_item_id(item)
    conn.execute(
        """
        INSERT INTO article_reviews (
            source, item_id, url, title, source_module, published_at,
            importance, push_now, market_impact, incremental_classification,
            affected_targets_json, reason, daily_summary, confidence,
            gate_json, skeptic_json, pre_skeptic_importance, pushed_at, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source, item_id) DO UPDATE SET
            source_module = excluded.source_module,
            published_at = excluded.published_at,
            importance = excluded.importance,
            push_now = excluded.push_now,
            market_impact = excluded.market_impact,
            incremental_classification = excluded.incremental_classification,
            affected_targets_json = excluded.affected_targets_json,
            reason = excluded.reason,
            daily_summary = excluded.daily_summary,
            confidence = excluded.confidence,
            gate_json = excluded.gate_json,
            skeptic_json = excluded.skeptic_json,
            pre_skeptic_importance = excluded.pre_skeptic_importance
        """,
        (
            source,
            item_id,
            str(item.get("url") or ""),
            str(item.get("title") or ""),
            str(item.get("source_module") or item.get("source_display") or ""),
            str(item.get("published_at") or ""),
            str(review.get("importance") or "low"),
            1 if review.get("push_now") else 0,
            str(review.get("market_impact") or ""),
            str(review.get("incremental_classification") or ""),
            json.dumps(review.get("affected_targets") or [], ensure_ascii=False),
            str(review.get("reason") or ""),
            str(review.get("daily_summary") or ""),
            str(review.get("confidence") or ""),
            json.dumps(review, ensure_ascii=False),
            json.dumps(review.get("skeptic") or {}, ensure_ascii=False),
            str(review.get("pre_skeptic_importance") or ""),
            "",
            now,
        ),
    )
    conn.commit()


def review_exists(conn: sqlite3.Connection, source: str, item_id: str) -> dict[str, Any] | None:
    ensure_article_reviews_table(conn)
    row = conn.execute(
        """
        SELECT importance, push_now, market_impact, incremental_classification,
               affected_targets_json, reason, daily_summary, confidence, gate_json,
               skeptic_json, pre_skeptic_importance, pushed_at
        FROM article_reviews
        WHERE source = ? AND item_id = ?
        """,
        (source, item_id),
    ).fetchone()
    if not row:
        return None
    (
        importance,
        push_now,
        market_impact,
        incremental,
        targets_json,
        reason,
        daily_summary,
        confidence,
        gate_json,
        skeptic_json,
        pre_skeptic_importance,
        pushed_at,
    ) = row
    raw = json_loads_dict(gate_json)
    try:
        targets = json.loads(targets_json or "[]")
    except json.JSONDecodeError:
        targets = []
    return {
        "importance": importance,
        "push_now": bool(push_now),
        "market_impact": market_impact or "",
        "incremental_classification": incremental or "",
        "affected_targets": targets if isinstance(targets, list) else [],
        "reason": reason or "",
        "daily_summary": daily_summary or "",
        "confidence": confidence or "",
        "raw": raw,
        "skeptic": json_loads_dict(skeptic_json) if skeptic_json else raw.get("skeptic", {}),
        "pre_skeptic_importance": pre_skeptic_importance or raw.get("pre_skeptic_importance", ""),
        "pushed_at": pushed_at or "",
    }


def mark_pushed(conn: sqlite3.Connection, source: str, item_id: str) -> None:
    ensure_article_reviews_table(conn)
    conn.execute(
        "UPDATE article_reviews SET pushed_at = ? WHERE source = ? AND item_id = ?",
        (datetime.now(timezone.utc).isoformat(), source, item_id),
    )
    conn.commit()


def gate_lines(review: dict[str, Any]) -> list[str]:
    targets = review.get("affected_targets") or []
    lines = [
        f"重要性门控：{review.get('importance', 'low')}",
        f"是否即时推送：{'是' if review.get('push_now') else '否'}",
    ]
    if review.get("incremental_classification"):
        lines.append(f"门控增量判断：{review['incremental_classification']}")
    if review.get("market_impact"):
        lines.append(f"门控市场影响：{review['market_impact']}")
    if targets:
        lines.append("门控涉及标的/环节：" + "；".join(str(item) for item in targets[:5]))
    if review.get("reason"):
        lines.append(f"门控理由：{review['reason']}")
    lines.extend(skeptic_lines(review))
    return lines
