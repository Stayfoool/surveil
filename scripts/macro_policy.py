"""Macro liquidity and Fed-policy relevance rules.

This line is separate from semiconductor industry hard-variable monitoring.
It focuses on US monetary-policy expectations and market liquidity shocks
that can affect A-share risk appetite, growth-stock valuation, FX, and rates.
"""

from __future__ import annotations

import re
from typing import Any


PRIMARY_DATA_KEYWORDS = (
    "非农",
    "nonfarm",
    "payroll",
    "nfp",
    "cpi",
    "消费者价格指数",
    "pce",
    "个人消费支出",
    "核心pce",
    "core pce",
)

FED_EVENT_KEYWORDS = (
    "美联储",
    "联储",
    "federal reserve",
    "fed",
    "fomc",
    "沃什",
    "沃尔什",
    "warsh",
    "鲍威尔",
    "powell",
    "主席讲话",
    "议息",
    "会议纪要",
    "点阵图",
    "降息",
    "加息",
    "利率路径",
)

SECONDARY_DATA_KEYWORDS = (
    "adp",
    "jolts",
    "职位空缺",
    "初请",
    "续请",
    "ppi",
    "生产者价格指数",
    "ism",
    "制造业pmi",
    "服务业pmi",
)

IGNORED_DATA_KEYWORDS = (
    "零售销售",
    "retail sales",
)

MARKET_REACTION_KEYWORDS = (
    "2年期美债",
    "二年期美债",
    "两年期美债",
    "10年期美债",
    "十年期美债",
    "美债收益率",
    "treasury yield",
    "ust yield",
    "dxy",
    "美元指数",
    "美元走强",
    "美元走弱",
    "纳指期货",
    "标普期货",
    "黄金",
    "人民币",
    "离岸人民币",
    "risk appetite",
    "风险偏好",
)

LARGE_MOVE_PATTERNS = (
    r"(?:大跌|大涨|跳水|飙升|急跌|急升|重挫|拉升|明显下行|明显上行|创.*新低|创.*新高)",
    r"(?:下跌|上涨|回落|上行|下行).{0,12}(?:\d+(?:\.\d+)?\s*(?:bp|基点|个基点|%|％))",
    r"(?:\d+(?:\.\d+)?\s*(?:bp|基点|个基点)).{0,12}(?:下跌|上涨|回落|上行|下行)",
)

SURPRISE_PATTERNS = (
    r"(?:高于|低于|不及|超过|逊于|强于|弱于).{0,12}(?:预期|市场预期)",
    r"(?:预期|市场预期).{0,12}(?:高于|低于|不及|超过|逊于|强于|弱于)",
    r"(?:意外|超预期|不及预期|大幅偏离|显著偏离)",
)


def text_blob(*values: Any) -> str:
    return " ".join(str(value or "") for value in values)


def contains_any(text: str, keywords: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(keyword.lower() in lowered for keyword in keywords)


def has_large_move(text: str) -> bool:
    return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in LARGE_MOVE_PATTERNS)


def has_surprise(text: str) -> bool:
    return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in SURPRISE_PATTERNS)


def is_retail_sales_only(text: str) -> bool:
    return contains_any(text, IGNORED_DATA_KEYWORDS) and not (
        contains_any(text, PRIMARY_DATA_KEYWORDS)
        or contains_any(text, FED_EVENT_KEYWORDS)
        or contains_any(text, MARKET_REACTION_KEYWORDS)
    )


def macro_policy_match(item: dict[str, Any]) -> dict[str, Any]:
    text = text_blob(item.get("title"), item.get("summary"), item.get("content"), item.get("full_text"))
    if is_retail_sales_only(text):
        return {"matched": False, "tier": "ignored", "reason": "零售销售不纳入宏观政策线。"}

    primary = contains_any(text, PRIMARY_DATA_KEYWORDS)
    fed_event = contains_any(text, FED_EVENT_KEYWORDS)
    secondary = contains_any(text, SECONDARY_DATA_KEYWORDS)
    market_reaction = contains_any(text, MARKET_REACTION_KEYWORDS)
    large_move = has_large_move(text)
    surprise = has_surprise(text)

    if primary or fed_event:
        return {
            "matched": True,
            "tier": "primary",
            "push_bias": "high",
            "reason": "命中美联储/FOMC/现任主席沃什、前主席鲍威尔相关报道，或非农、CPI、PCE 等核心宏观事件。",
            "tags": [
                tag
                for tag, ok in (
                    ("primary_data", primary),
                    ("fed_event", fed_event),
                    ("market_reaction", market_reaction),
                    ("large_move", large_move),
                    ("surprise", surprise),
                )
                if ok
            ],
        }
    if secondary and (large_move or surprise or market_reaction):
        return {
            "matched": True,
            "tier": "secondary_major",
            "push_bias": "conditional",
            "reason": "命中 ADP/JOLTS/初请/PPI/ISM 等次重点数据，且伴随重大偏离或市场反应。",
            "tags": [
                tag
                for tag, ok in (
                    ("secondary_data", secondary),
                    ("market_reaction", market_reaction),
                    ("large_move", large_move),
                    ("surprise", surprise),
                )
                if ok
            ],
        }
    if market_reaction and large_move:
        return {
            "matched": True,
            "tier": "market_reaction",
            "push_bias": "conditional",
            "reason": "美债收益率、美元或主要风险资产出现明显波动，可能影响 A 股风险偏好。",
            "tags": ["market_reaction", "large_move"],
        }
    return {"matched": False, "tier": "", "reason": ""}


def macro_prompt_note(item: dict[str, Any]) -> str:
    match = macro_policy_match(item)
    if not match.get("matched"):
        return ""
    return (
        "宏观流动性/美联储政策线提示："
        f"{match.get('reason')} 重点判断其对美债收益率、美元、纳指期货、人民币、"
        "A 股风险偏好、成长股/半导体估值的影响；区分偏鸽利好、衰退恐慌、事件前避险和已定价。"
    )


def apply_macro_review_override(review: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    match = macro_policy_match(item)
    if not match.get("matched"):
        return review
    updated = dict(review)
    if match.get("tier") == "primary":
        updated["importance"] = "high"
        updated["push_now"] = True
    elif str(updated.get("importance") or "").lower() == "high":
        updated["push_now"] = True
    updated["macro_policy_line"] = match
    targets = list(updated.get("affected_targets") or [])
    for target in ("美债收益率/美元", "A股风险偏好", "成长股估值"):
        if target not in targets:
            targets.append(target)
    updated["affected_targets"] = targets[:5]
    note = (
        "宏观政策线覆盖：该条涉及美联储/FOMC/主席沃什、前主席鲍威尔、非农/CPI/PCE，"
        "或次重点数据的重大偏离/市场反应；按对 A 股风险偏好和成长股估值的影响优先处理。"
    )
    reason = str(updated.get("reason") or "").strip()
    if note not in reason:
        updated["reason"] = f"{reason}\n{note}".strip()
    raw = dict(updated.get("raw") or {})
    raw["macro_policy_line"] = match
    updated["raw"] = raw
    return updated


def is_macro_event(item: dict[str, Any]) -> bool:
    return bool(macro_policy_match(item).get("matched"))
