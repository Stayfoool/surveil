"""LLM importance gate for official core-company news."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any

from llm_analysis import call_chat_completion_with_prompts, format_llm_analysis, llm_config
from industry_hardline import apply_hardline_review_override, explain_hardline
from skeptic_evaluator import skeptic_lines


OFFICIAL_NEWS_SOURCES = {
    "openai_news",
    "nvidia_blog",
    "nvidia_developer_blog",
    "samsung_semiconductor_news",
    "samsung_global_semiconductor",
    "skhynix_newsroom",
    "micron_news_releases",
}


GATE_SYSTEM_PROMPT = """你是半导体、AI 基础设施和二级市场研究助理。
任务：判断一条核心公司官网新闻是否需要“第一时间”推送给投资者。
核心公司包括 OpenAI、NVIDIA、Samsung Semiconductor、SK hynix、Micron 等。

请重点关注会影响半导体/AI 产业链供需、资本开支、技术路线、价格、订单、产能、竞争格局、A 股映射和美股映射的新闻。
高重要性示例：
- 新一代 GPU/ASIC/CPU/互联/液冷/服务器平台/AI 基础设施架构发布或重大技术方案，例如 Rubin 100% 液冷。
- HBM/DRAM/NAND/存储供货、样品、量产、涨价、产能、客户资格认证。
- 大客户采购、战略合作、供应协议、资本开支、建厂、先进封装、数据中心扩张。
- “星际之门/Stargate-like”超大资本开支事件：政府、云厂商、半导体龙头、AI 基础设施龙头或产业联盟计划投入超大金额建设 AI 数据中心、半导体工厂、HBM/存储产能、先进封装、CPO/光互联、电力/液冷等基础设施。
- 会直接影响 CPO、光模块、PCB、MLCC、电子布、玻璃基板、特气、电力、液冷等产业链的明确变化。

低/中重要性通常进入日报：
- 普通营销、案例、开发者教程、招聘、活动预告、泛泛生态合作。
- 没有新增订单、价格、产能、技术路线、客户、财务指引或产业链传导的内容。
- 注意：超大资本开支“预告/据报/拟宣布/将公布”不能因为尚未正式公布就自动降为 medium。只要金额足够重大、主体可信、产业方向明确、且有明确会议/发布时间/高层表态/政策背景，应判为 high、should_push_now=true，并在 reason 中标注“待确认/预告性质”和需要跟踪的正式公告。

只输出 JSON，不要 Markdown。"""


GATE_USER_PROMPT = """请分析以下官网新闻，输出 JSON：
{
  "importance": "high/medium/low",
  "should_push_now": true,
  "reason": "为什么需要或不需要第一时间推送",
  "industry_impact": "对半导体/AI产业链的影响",
  "a_share_relevance": "可能影响的A股方向或标的，无法判断则写无法判断",
  "daily_summary": "一句中文日报摘要",
  "analysis": {
    "core_content": "一句到两句中文核心内容",
    "themes": ["主题1", "主题2"],
    "incremental_view": {
      "classification": "增量利好/增量利空/已有预期/符合预期/利好落地/利空落地/可能利好出尽/可能利空出尽/中性信息/无法判断",
      "surprise_level": "高/中/低/无法判断",
      "priced_in": "大概率已定价/部分定价/尚未充分定价/无法判断",
      "reason": "为什么这么判断"
    },
    "initial_impact": "初步影响判断",
    "a_share": {"positive": [], "negative": []},
    "global_equity": {"positive": [], "negative": []},
    "tracking_points": ["后续跟踪点1"],
    "risks": ["风险1"],
    "watchlist_view": "是否值得纳入观察名单及理由"
  }
}

注意：
- should_push_now 只有在 importance=high 且产业链传导明确时才为 true。
- 如果只是普通博客、教程、客户案例、活动信息，importance 应为 medium 或 low，should_push_now=false。
- 对半导体/AI 基础设施“超大资本开支预告”单独处理：即使尚未正式公布，只要金额、主体和产业方向明确，并可能重估设备、材料、存储、光通信、PCB、先进封装、电力、液冷等产业链预期，应 importance=high、should_push_now=true；同时在 reason、daily_summary 和 analysis.tracking_points 中明确写“待确认/预告性质”，列出需要验证的正式公布时间、投资拆分、产能、设备订单和供应链受益方向。
- analysis 字段必须尽量符合既有研究简报格式。

来源：{source}
标题：{title}
发布时间：{published_at}
正文：
{content}
"""


def ensure_official_news_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS official_news_reviews (
            source TEXT NOT NULL,
            item_id TEXT NOT NULL,
            url TEXT,
            title TEXT NOT NULL,
            published_at TEXT,
            importance TEXT NOT NULL,
            should_push_now INTEGER NOT NULL DEFAULT 0,
            reason TEXT,
            daily_summary TEXT,
            analysis_json TEXT NOT NULL,
            skeptic_json TEXT,
            pre_skeptic_importance TEXT,
            pushed_at TEXT,
            created_at TEXT NOT NULL,
            PRIMARY KEY (source, item_id)
        )
        """
    )
    columns = {row[1] for row in conn.execute("PRAGMA table_info(official_news_reviews)").fetchall()}
    if "skeptic_json" not in columns:
        conn.execute("ALTER TABLE official_news_reviews ADD COLUMN skeptic_json TEXT")
    if "pre_skeptic_importance" not in columns:
        conn.execute("ALTER TABLE official_news_reviews ADD COLUMN pre_skeptic_importance TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_official_news_created ON official_news_reviews(created_at)")
    conn.commit()


def official_news_enabled() -> bool:
    return llm_config() is not None


def is_official_news_source(source: str) -> bool:
    return source in OFFICIAL_NEWS_SOURCES


def review_exists(conn: sqlite3.Connection, source: str, item_id: str) -> dict[str, Any] | None:
    ensure_official_news_table(conn)
    row = conn.execute(
        """
        SELECT importance, should_push_now, reason, daily_summary, analysis_json,
               skeptic_json, pre_skeptic_importance, pushed_at
        FROM official_news_reviews
        WHERE source = ? AND item_id = ?
        """,
        (source, item_id),
    ).fetchone()
    if not row:
        return None
    importance, should_push_now, reason, daily_summary, analysis_json, skeptic_json, pre_skeptic_importance, pushed_at = row
    parsed = json.loads(analysis_json)
    review = {
        "importance": importance,
        "should_push_now": bool(should_push_now),
        "reason": reason or "",
        "daily_summary": daily_summary or "",
        "analysis": parsed,
        "pushed_at": pushed_at or "",
    }
    try:
        skeptic = json.loads(skeptic_json or "{}")
    except json.JSONDecodeError:
        skeptic = {}
    if isinstance(skeptic, dict) and skeptic:
        review["skeptic"] = skeptic
        review["pre_skeptic_importance"] = pre_skeptic_importance or ""
    elif isinstance(parsed, dict) and isinstance(parsed.get("_skeptic"), dict):
        review["skeptic"] = parsed["_skeptic"]
        review["pre_skeptic_importance"] = parsed.get("_pre_skeptic_importance", "")
    return review


def save_review(conn: sqlite3.Connection, source: str, item: dict[str, Any], review: dict[str, Any]) -> None:
    ensure_official_news_table(conn)
    now = datetime.now(timezone.utc).isoformat()
    analysis_payload = review.get("analysis") if isinstance(review.get("analysis"), dict) else dict(review)
    analysis_payload = dict(analysis_payload)
    if review.get("skeptic"):
        analysis_payload["_skeptic"] = review["skeptic"]
        analysis_payload["_pre_skeptic_importance"] = review.get("pre_skeptic_importance", "")
    conn.execute(
        """
        INSERT INTO official_news_reviews (
            source, item_id, url, title, published_at, importance, should_push_now,
            reason, daily_summary, analysis_json, skeptic_json,
            pre_skeptic_importance, pushed_at, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source, item_id) DO UPDATE SET
            importance = excluded.importance,
            should_push_now = excluded.should_push_now,
            reason = excluded.reason,
            daily_summary = excluded.daily_summary,
            analysis_json = excluded.analysis_json,
            skeptic_json = excluded.skeptic_json,
            pre_skeptic_importance = excluded.pre_skeptic_importance
        """,
        (
            source,
            str(item.get("id") or item.get("url") or item.get("title") or ""),
            str(item.get("url") or ""),
            str(item.get("title") or ""),
            str(item.get("published_at") or ""),
            str(review.get("importance") or "low").lower(),
            1 if review.get("should_push_now") else 0,
            str(review.get("reason") or ""),
            str(review.get("daily_summary") or ""),
            json.dumps(analysis_payload, ensure_ascii=False),
            json.dumps(review.get("skeptic") or {}, ensure_ascii=False),
            str(review.get("pre_skeptic_importance") or ""),
            "",
            now,
        ),
    )
    conn.commit()


def mark_pushed(conn: sqlite3.Connection, source: str, item_id: str) -> None:
    ensure_official_news_table(conn)
    conn.execute(
        "UPDATE official_news_reviews SET pushed_at = ? WHERE source = ? AND item_id = ?",
        (datetime.now(timezone.utc).isoformat(), source, item_id),
    )
    conn.commit()


def normalize_review(parsed: dict[str, Any]) -> dict[str, Any]:
    importance = str(parsed.get("importance") or "low").strip().lower()
    if importance not in {"high", "medium", "low"}:
        importance = "low"
    should_push_now = bool(parsed.get("should_push_now")) and importance == "high"
    analysis = parsed.get("analysis") if isinstance(parsed.get("analysis"), dict) else parsed
    return {
        "importance": importance,
        "should_push_now": should_push_now,
        "reason": str(parsed.get("reason") or "").strip(),
        "industry_impact": str(parsed.get("industry_impact") or "").strip(),
        "a_share_relevance": str(parsed.get("a_share_relevance") or "").strip(),
        "daily_summary": str(parsed.get("daily_summary") or "").strip(),
        "analysis": analysis,
    }


def review_official_news(source: str, item: dict[str, Any]) -> dict[str, Any]:
    text = str(item.get("full_text") or item.get("content") or item.get("summary") or "").strip()
    title = str(item.get("title") or "").strip()
    hardline_note = explain_hardline(source, (title, text, item.get("source_module")))
    if hardline_note:
        text = f"【产业硬变量线提示】{hardline_note}\n\n{text}"
    user_prompt = (
        GATE_USER_PROMPT.replace("{source}", source)
        .replace("{title}", title)
        .replace("{published_at}", str(item.get("published_at") or ""))
        .replace("{content}", text[:12000])
    )
    parsed, model = call_chat_completion_with_prompts(
        GATE_SYSTEM_PROMPT,
        user_prompt,
        user_agent="surveil-official-news-gate/0.1",
        truncate_user_prompt=False,
        thinking_override=os.getenv("LLM_GATE_THINKING_TYPE", "enabled"),
        max_tokens_override=int(os.getenv("LLM_GATE_MAX_OUTPUT_TOKENS", "1400")),
    )
    review = normalize_review(parsed)
    review["model"] = model
    return review


def apply_official_hardline_override(source: str, item: dict[str, Any], review: dict[str, Any]) -> dict[str, Any]:
    return apply_hardline_review_override(source, item, review)


def analysis_lines_from_review(review: dict[str, Any]) -> list[str]:
    parsed = review.get("analysis") if isinstance(review.get("analysis"), dict) else review
    model = str(review.get("model") or "LLM")
    lines = format_llm_analysis(parsed, model)
    prefix = [
        f"官网新闻重要性：{review.get('importance', 'low')}",
        f"是否即时推送：{'是' if review.get('should_push_now') else '否'}",
    ]
    reason = str(review.get("reason") or "").strip()
    if reason:
        prefix.append(f"分流理由：{reason}")
    prefix.extend(skeptic_lines(review))
    return [lines[0], *prefix, *lines[1:]]
