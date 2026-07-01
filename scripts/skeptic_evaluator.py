"""Skeptic evaluator for stale, priced-in, or over-linked push candidates."""

from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from typing import Any

from llm_analysis import call_chat_completion_with_prompts, llm_config
from source_health import record_source_failure, record_source_success


SKEPTIC_SYSTEM_PROMPT = """你是投资情报系统里的 Skeptic Evaluator。
你的职责不是重新写利好解读，而是专门挑错，判断一条准备即时推送的资讯是否存在：
- 旧闻、重复转载、缺少增量
- 大概率已经 price in
- 对相关股票过度联想、产业链传导太远
- 缺少订单、价格、产能、政策、业绩、客户等硬变量
- 标题党、AI 生成、营销稿或证据不足

请克制：只有在证据明确时才建议 block；如果只是有疑虑但仍可能重要，建议 downgrade 到日报。
只输出 JSON，不要 Markdown。"""


SKEPTIC_USER_PROMPT = """请复核这条准备即时推送的资讯，输出 JSON：
{
  "skeptic_verdict": "pass/downgrade/block/need_human_review",
  "old_news_risk": "low/medium/high",
  "price_in_risk": "low/medium/high",
  "over_linking_risk": "low/medium/high",
  "hard_variable_score": 0,
  "relation_strength_score": 0,
  "reason": "挑错理由，必须具体",
  "what_would_change_mind": "需要什么证据才能提高置信度",
  "final_push_suggestion": "push_now/daily/ignore"
}

当前资讯：
来源：{source}
标题：{title}
发布时间：{published_at}
正文/摘要：
{content}

原始门控判断：
{gate_review}

系统历史证据：
{history_evidence}
"""


def env_flag(name: str, default: bool = True) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "y", "on", "是"}


def skeptic_enabled() -> bool:
    return env_flag("SKEPTIC_EVALUATOR_ENABLED", True)


def parse_dt(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def days_old(value: str) -> float | None:
    parsed = parse_dt(value)
    if not parsed:
        return None
    return max(0.0, (datetime.now(timezone.utc) - parsed).total_seconds() / 86400)


def normalize_text(value: str) -> str:
    return " ".join(str(value or "").lower().split())


def title_similarity(left: str, right: str) -> float:
    a = normalize_text(left)
    b = normalize_text(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def history_candidates(conn: sqlite3.Connection, *, source: str, item: dict[str, Any], limit: int = 12) -> list[dict[str, Any]]:
    """Collect recent similar items from local history tables."""
    title = str(item.get("title") or "")
    current_url = str(item.get("url") or "")
    current_id = str(item.get("id") or item.get("url") or item.get("title") or "")
    lookback_days = max(
        int(os.getenv("SKEPTIC_DUPLICATE_LOOKBACK_DAYS", "14") or "14"),
        int(os.getenv("SKEPTIC_STALE_NEWS_DAYS", "7") or "7"),
    )
    cutoff = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).isoformat()
    candidates: list[dict[str, Any]] = []

    def add_candidate(table: str, row_source: str, row_id: str, row_title: str, row_url: str, seen_at: str, published_at: str) -> None:
        if row_source == source and row_id == current_id:
            return
        if current_url and row_url and current_url == row_url:
            return
        sim = title_similarity(title, row_title)
        if sim < 0.68 and normalize_text(title) not in normalize_text(row_title) and normalize_text(row_title) not in normalize_text(title):
            return
        candidates.append(
            {
                "table": table,
                "source": row_source,
                "item_id": row_id,
                "title": row_title,
                "url": row_url,
                "published_at": published_at,
                "seen_at": seen_at,
                "similarity": round(sim, 3),
                "age_days": days_old(published_at or seen_at),
            }
        )

    if table_exists(conn, "seen_items"):
        for row in conn.execute(
            """
            SELECT source, item_id, url, title, published_at, first_seen_at
            FROM seen_items
            WHERE first_seen_at >= ?
            ORDER BY first_seen_at DESC
            LIMIT 800
            """,
            (cutoff,),
        ).fetchall():
            add_candidate("seen_items", row[0] or "", row[1] or "", row[3] or "", row[2] or "", row[5] or "", row[4] or "")

    if table_exists(conn, "article_reviews"):
        for row in conn.execute(
            """
            SELECT source, item_id, url, title, published_at, created_at
            FROM article_reviews
            WHERE created_at >= ?
            ORDER BY created_at DESC
            LIMIT 600
            """,
            (cutoff,),
        ).fetchall():
            add_candidate("article_reviews", row[0] or "", row[1] or "", row[3] or "", row[2] or "", row[5] or "", row[4] or "")

    if table_exists(conn, "official_news_reviews"):
        for row in conn.execute(
            """
            SELECT source, item_id, url, title, published_at, created_at
            FROM official_news_reviews
            WHERE created_at >= ?
            ORDER BY created_at DESC
            LIMIT 300
            """,
            (cutoff,),
        ).fetchall():
            add_candidate("official_news_reviews", row[0] or "", row[1] or "", row[3] or "", row[2] or "", row[5] or "", row[4] or "")

    candidates.sort(key=lambda row: (float(row.get("similarity") or 0), str(row.get("seen_at") or "")), reverse=True)
    return candidates[:limit]


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1", (table,)).fetchone()
    return row is not None


def deterministic_skeptic(*, item: dict[str, Any], history: list[dict[str, Any]]) -> dict[str, Any]:
    published_age = days_old(str(item.get("published_at") or ""))
    stale_days = int(os.getenv("SKEPTIC_STALE_NEWS_DAYS", "7"))
    duplicate_days = int(os.getenv("SKEPTIC_DUPLICATE_LOOKBACK_DAYS", "14"))
    strong_duplicate = next(
        (
            row
            for row in history
            if float(row.get("similarity") or 0) >= 0.92
            and (row.get("age_days") is None or float(row.get("age_days") or 0) <= duplicate_days)
        ),
        None,
    )
    old_duplicate = next(
        (
            row
            for row in history
            if float(row.get("similarity") or 0) >= 0.82
            and row.get("age_days") is not None
            and float(row.get("age_days") or 0) >= 2
        ),
        None,
    )
    if strong_duplicate:
        return {
            "skeptic_verdict": "downgrade",
            "old_news_risk": "high",
            "price_in_risk": "medium",
            "over_linking_risk": "low",
            "hard_variable_score": 50,
            "relation_strength_score": 50,
            "reason": f"本地历史中存在高度相似资讯：{strong_duplicate.get('source')} / {strong_duplicate.get('title')}",
            "what_would_change_mind": "需要当前报道提供新的价格、订单、产能、财务指引或监管文件。",
            "final_push_suggestion": "daily",
            "history_evidence": history,
            "mode": "deterministic_duplicate",
        }
    if old_duplicate:
        return {
            "skeptic_verdict": "downgrade",
            "old_news_risk": "high",
            "price_in_risk": "high",
            "over_linking_risk": "medium",
            "hard_variable_score": 40,
            "relation_strength_score": 45,
            "reason": f"相似主题在约 {old_duplicate.get('age_days')} 天前已出现，当前内容可能是二次传播或旧闻再报道。",
            "what_would_change_mind": "需要证明当前报道有新变量，而不仅是复述旧主题。",
            "final_push_suggestion": "daily",
            "history_evidence": history,
            "mode": "deterministic_old_duplicate",
        }
    if published_age is not None and published_age >= stale_days:
        return {
            "skeptic_verdict": "downgrade",
            "old_news_risk": "high",
            "price_in_risk": "medium",
            "over_linking_risk": "medium",
            "hard_variable_score": 40,
            "relation_strength_score": 50,
            "reason": f"发布时间距今约 {published_age:.1f} 天，超过即时推送的新鲜度阈值。",
            "what_would_change_mind": "需要当前来源明确披露此前未出现的新数据或新公告。",
            "final_push_suggestion": "daily",
            "history_evidence": history,
            "mode": "deterministic_stale",
        }
    return {
        "skeptic_verdict": "pass",
        "old_news_risk": "low",
        "price_in_risk": "low",
        "over_linking_risk": "low",
        "hard_variable_score": 60,
        "relation_strength_score": 60,
        "reason": "本地历史未发现明确旧闻或高度重复证据。",
        "what_would_change_mind": "",
        "final_push_suggestion": "push_now",
        "history_evidence": history,
        "mode": "deterministic_pass",
    }


def normalize_skeptic(parsed: dict[str, Any], *, fallback: dict[str, Any]) -> dict[str, Any]:
    verdict = str(parsed.get("skeptic_verdict") or fallback.get("skeptic_verdict") or "pass").strip().lower()
    if verdict not in {"pass", "downgrade", "block", "need_human_review"}:
        verdict = "pass"
    final_push = str(parsed.get("final_push_suggestion") or fallback.get("final_push_suggestion") or "push_now").strip().lower()
    if final_push not in {"push_now", "daily", "ignore"}:
        final_push = "daily" if verdict in {"downgrade", "need_human_review"} else "ignore" if verdict == "block" else "push_now"

    def safe_int(value: Any, default: Any) -> int:
        raw = str(value if value is not None else default).strip()
        match = re.search(r"-?\d+", raw)
        return int(match.group(0)) if match else 0

    return {
        "skeptic_verdict": verdict,
        "old_news_risk": str(parsed.get("old_news_risk") or fallback.get("old_news_risk") or "low").strip().lower(),
        "price_in_risk": str(parsed.get("price_in_risk") or fallback.get("price_in_risk") or "low").strip().lower(),
        "over_linking_risk": str(parsed.get("over_linking_risk") or fallback.get("over_linking_risk") or "low").strip().lower(),
        "hard_variable_score": safe_int(parsed.get("hard_variable_score"), fallback.get("hard_variable_score") or 0),
        "relation_strength_score": safe_int(parsed.get("relation_strength_score"), fallback.get("relation_strength_score") or 0),
        "reason": str(parsed.get("reason") or fallback.get("reason") or "").strip(),
        "what_would_change_mind": str(parsed.get("what_would_change_mind") or fallback.get("what_would_change_mind") or "").strip(),
        "final_push_suggestion": final_push,
        "history_evidence": fallback.get("history_evidence") or [],
    }


def text_blob(*values: Any) -> str:
    return "\n".join(str(value or "") for value in values)


def contains_any(text: str, keywords: tuple[str, ...]) -> bool:
    lower = text.lower()
    return any(keyword.lower() in lower for keyword in keywords)


def has_quantified_hard_variable(text: str) -> bool:
    lower = text.lower()
    amount_patterns = (
        r"\d+(?:\.\d+)?\s*(?:亿|万亿|兆)\s*(?:韩元|美元|人民币|元)?",
        r"\d+(?:\.\d+)?\s*(?:billion|bn|million|trillion)\s*(?:won|usd|dollars|rmb|yuan)?",
        r"\d+(?:\.\d+)?\s*(?:台|套|条|座|家|%|％)",
    )
    return any(re.search(pattern, lower, flags=re.IGNORECASE) for pattern in amount_patterns)


def is_hbm_industry_hard_variable(item: dict[str, Any], review: dict[str, Any]) -> bool:
    """Protect quantified HBM/storage capex or equipment events from over-downgrade.

    This is intentionally narrow: the original gate must already judge the item as high
    importance. The override only prevents the skeptic from suppressing industry-level
    hard variables just because the direct A-share supplier is still unconfirmed.
    """
    original_importance = str(review.get("pre_skeptic_importance") or review.get("importance") or "").lower()
    if original_importance != "high":
        return False
    text = text_blob(
        item.get("title"),
        item.get("summary"),
        item.get("content"),
        item.get("full_text"),
        review.get("market_impact"),
        review.get("reason"),
        review.get("daily_summary"),
    )
    if not contains_any(text, ("hbm", "hbm3", "hbm3e", "hbm4", "hbm4e", "高带宽内存", "高頻寬記憶體")):
        return False
    if not contains_any(text, ("sk海力士", "sk hynix", "海力士", "三星", "samsung", "美光", "micron", "存储大厂", "記憶體大廠")):
        return False
    if not contains_any(
        text,
        (
            "采购",
            "訂購",
            "订购",
            "订单",
            "設備",
            "设备",
            "测试",
            "檢測",
            "检测",
            "tester",
            "test equipment",
            "封装",
            "封測",
            "封测",
            "扩产",
            "擴產",
            "capex",
            "capital expenditure",
            "投资",
            "工厂",
            "fab",
        ),
    ):
        return False
    return has_quantified_hard_variable(text)


def apply_industry_hard_variable_override(updated: dict[str, Any], *, item: dict[str, Any], push_key: str) -> dict[str, Any]:
    if not is_hbm_industry_hard_variable(item, updated):
        return updated
    skeptic = updated.get("skeptic") if isinstance(updated.get("skeptic"), dict) else {}
    if not skeptic or str(skeptic.get("skeptic_verdict") or "pass") == "block":
        return updated
    restored = dict(updated)
    restored[push_key] = True
    restored["importance"] = "high"
    restored["industry_hard_variable_override"] = True
    restored["daily_summary"] = (
        str(restored.get("daily_summary") or "").strip()
        or "HBM/HBM4 产业链出现带金额或数量的设备、测试、封装或扩产硬变量，供应商待确认。"
    )
    note = (
        "产业硬变量覆盖：HBM/HBM4 相关存储龙头出现明确金额或数量的设备、测试、封装或扩产信息，"
        "即使供应商或 A 股映射待确认，也保留即时推送，并标注“受益标的待确认”。"
    )
    reason = str(restored.get("reason") or "").strip()
    if note not in reason:
        restored["reason"] = f"{reason}\n{note}".strip()
    targets = list(restored.get("affected_targets") or [])
    for target in ("HBM/HBM4 测试设备", "半导体后道测试", "受益标的待确认"):
        if target not in targets:
            targets.append(target)
    restored["affected_targets"] = targets[:5]
    skeptic = dict(skeptic)
    skeptic["industry_hard_variable_override"] = True
    skeptic["final_push_suggestion_before_override"] = skeptic.get("final_push_suggestion")
    skeptic["final_push_suggestion"] = "push_now"
    restored["skeptic"] = skeptic
    return restored


def llm_skeptic_review(
    *,
    source: str,
    item: dict[str, Any],
    gate_review: dict[str, Any],
    fallback: dict[str, Any],
) -> dict[str, Any]:
    config = llm_config()
    if config is None:
        result = dict(fallback)
        result["mode"] = "llm_unavailable"
        return result
    text = str(item.get("full_text") or item.get("content") or item.get("summary") or "").strip()
    user_prompt = (
        SKEPTIC_USER_PROMPT.replace("{source}", source)
        .replace("{title}", str(item.get("title") or ""))
        .replace("{published_at}", str(item.get("published_at") or ""))
        .replace("{content}", text[:5000])
        .replace("{gate_review}", json.dumps(gate_review, ensure_ascii=False)[:5000])
        .replace("{history_evidence}", json.dumps(fallback.get("history_evidence") or [], ensure_ascii=False)[:4000])
    )
    parsed, model = call_chat_completion_with_prompts(
        SKEPTIC_SYSTEM_PROMPT,
        user_prompt,
        user_agent="surveil-skeptic-evaluator/0.1",
        truncate_user_prompt=False,
        thinking_override=os.getenv("LLM_SKEPTIC_THINKING_TYPE", os.getenv("LLM_GATE_THINKING_TYPE", "enabled")),
        max_tokens_override=int(os.getenv("LLM_SKEPTIC_MAX_OUTPUT_TOKENS", "1200")),
    )
    result = normalize_skeptic(parsed, fallback=fallback)
    result["model"] = model
    result["mode"] = "llm"
    return result


def apply_skeptic_review(
    conn: sqlite3.Connection,
    *,
    source: str,
    item: dict[str, Any],
    review: dict[str, Any],
    push_key: str,
) -> dict[str, Any]:
    """Return a review possibly downgraded by the skeptic evaluator."""
    if not skeptic_enabled() or not review.get(push_key):
        return review
    history = history_candidates(conn, source=source, item=item)
    deterministic = deterministic_skeptic(item=item, history=history)
    if deterministic["skeptic_verdict"] == "pass":
        try:
            skeptic = llm_skeptic_review(source=source, item=item, gate_review=review, fallback=deterministic)
            record_source_success(conn, "signal_pipeline", "skeptic_evaluator")
        except Exception as exc:  # noqa: BLE001 - skepticism must not break ingestion
            record_source_failure(conn, "signal_pipeline", "skeptic_evaluator", exc)
            skeptic = dict(deterministic)
            skeptic["mode"] = "llm_error"
            skeptic["error"] = str(exc)
    else:
        skeptic = deterministic

    updated = dict(review)
    updated["skeptic"] = skeptic
    verdict = str(skeptic.get("skeptic_verdict") or "pass")
    suggestion = str(skeptic.get("final_push_suggestion") or "push_now")
    if verdict != "pass":
        original_reason = str(updated.get("reason") or "").strip()
        skeptic_reason = str(skeptic.get("reason") or "").strip()
        if skeptic_reason:
            updated["reason"] = f"{original_reason}\nSkeptic：{skeptic_reason}".strip()
    if verdict in {"downgrade", "need_human_review"} or suggestion == "daily":
        updated[push_key] = False
        updated["pre_skeptic_importance"] = updated.get("importance", "")
        updated["importance"] = "medium"
        updated["skeptic_downgraded"] = True
    elif verdict == "block" or suggestion == "ignore":
        updated[push_key] = False
        updated["pre_skeptic_importance"] = updated.get("importance", "")
        updated["importance"] = "low"
        updated["skeptic_blocked"] = True
    updated = apply_industry_hard_variable_override(updated, item=item, push_key=push_key)
    return updated


def skeptic_lines(review: dict[str, Any]) -> list[str]:
    skeptic = review.get("skeptic") if isinstance(review.get("skeptic"), dict) else {}
    if not skeptic:
        return []
    lines = [
        f"Skeptic 结论：{skeptic.get('skeptic_verdict', '-')}",
        (
            "Skeptic 风险："
            f"旧闻 {skeptic.get('old_news_risk', '-')} / "
            f"price-in {skeptic.get('price_in_risk', '-')} / "
            f"过度联想 {skeptic.get('over_linking_risk', '-')}"
        ),
    ]
    if skeptic.get("reason"):
        lines.append(f"Skeptic 理由：{skeptic['reason']}")
    if skeptic.get("what_would_change_mind"):
        lines.append(f"需要验证：{skeptic['what_would_change_mind']}")
    return lines
