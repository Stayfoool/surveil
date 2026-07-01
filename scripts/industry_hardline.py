"""Rules for semiconductor/AI industry hard-variable sources.

This module keeps the narrow "industry hardline" source set separate from
broader portfolio news sources. It is intentionally limited to the five
sources requested by the user:
SEMI, TrendForce, DIGITIMES, The Elec, and Nikkei xTECH.
"""

from __future__ import annotations

import re
from typing import Iterable


HARDLINE_SOURCE_PREFIXES = (
    "semi_prnewswire_semiconductors",
    "trendforce_",
    "digitimes_",
    "nikkei_xtech_",
    "thelec_",
)


HARDLINE_SOURCE_NAMES = (
    "semi_prnewswire_semiconductors",
    "trendforce_page",
    "digitimes_tw_semiconductors_components",
    "digitimes_tw_ic_design",
    "digitimes_tw_ic_manufacturing",
    "digitimes_tw_ai_focus",
    "digitimes_tw_server",
    "digitimes_en_daily",
    "nikkei_xtech_all",
    "thelec_kr_semiconductor",
    "thelec_kr_all",
)


HARDLINE_KEYWORDS = (
    "equipment",
    "device",
    "equipment",
    "material",
    "materials",
    "capex",
    "capital expenditure",
    "investment",
    "invest",
    "funding",
    "factory",
    "fab",
    "plant",
    "capacity",
    "expansion",
    "output",
    "production",
    "price",
    "pricing",
    "raise",
    "raise prices",
    "increase",
    "shortage",
    "tighten",
    "tightening",
    "supply",
    "demand",
    "order",
    "orders",
    "backlog",
    "shipment",
    "shipment",
    "restriction",
    "ban",
    "export control",
    "control",
    "tariff",
    "HBM",
    "DRAM",
    "NAND",
    "MLCC",
    "glass core",
    "advanced packaging",
    "CPO",
    "optical",
    "photonics",
    "liquid cooling",
    "power",
    "grid",
)

STRONG_HARDLINE_KEYWORDS = (
    "capex",
    "capital expenditure",
    "investment",
    "invest",
    "equipment",
    "material",
    "materials",
    "capacity",
    "expansion",
    "price",
    "pricing",
    "shortage",
    "tighten",
    "order",
    "orders",
    "backlog",
    "restriction",
    "export control",
    "ban",
    "资本开支",
    "投资",
    "设备",
    "材料",
    "产能",
    "扩产",
    "涨价",
    "价格",
    "短缺",
    "紧缺",
    "订单",
    "管制",
    "出口管制",
    "禁令",
)


def is_hardline_source(source: str) -> bool:
    return source in HARDLINE_SOURCE_NAMES or source.startswith(HARDLINE_SOURCE_PREFIXES)


def effective_source(source: str, item: dict | None = None) -> str:
    if item and source == "trendforce_page":
        return str(item.get("page_source") or source)
    return source


def hardline_heuristic_matches(text: str) -> bool:
    lowered = str(text or "").lower()
    return any(keyword.lower() in lowered for keyword in HARDLINE_KEYWORDS)


def has_strong_keyword(text: str) -> bool:
    lowered = str(text or "").lower()
    return any(keyword.lower() in lowered for keyword in STRONG_HARDLINE_KEYWORDS)


def has_quantified_signal(text: str) -> bool:
    patterns = (
        r"\d+(?:\.\d+)?\s*(?:亿|万亿|兆)\s*(?:韩元|美元|人民币|元)?",
        r"\d+(?:\.\d+)?\s*(?:billion|bn|million|trillion)\s*(?:won|usd|dollars|rmb|yuan)?",
        r"\d+(?:\.\d+)?\s*(?:%|％)",
        r"\d+(?:\.\d+)?\s*(?:台|套|条|座|家|片|wafers?|units?|tools?)",
    )
    return any(re.search(pattern, str(text or ""), flags=re.IGNORECASE) for pattern in patterns)


def is_quantified_hardline_item(source: str, item: dict) -> bool:
    effective = effective_source(source, item)
    if not is_hardline_source(effective):
        return False
    text = collect_hardline_text(
        item.get("title"),
        item.get("summary"),
        item.get("content"),
        item.get("full_text"),
        item.get("source_module"),
        item.get("source_display"),
    )
    return has_strong_keyword(text) and has_quantified_signal(text)


def apply_hardline_review_override(source: str, item: dict, review: dict) -> dict:
    """Keep quantified hard-variable items from narrow industry sources immediate.

    This only applies to SEMI/TrendForce/DIGITIMES/The Elec/Nikkei xTECH.
    It does not apply to domestic finance wires such as Yicai or CLS.
    """
    if not is_quantified_hardline_item(source, item):
        return review
    updated = dict(review)
    updated["importance"] = "high"
    updated["push_now"] = True
    updated["industry_hardline_override"] = True
    targets = list(updated.get("affected_targets") or [])
    for target in ("产业硬变量", "受益/受损标的待确认"):
        if target not in targets:
            targets.append(target)
    updated["affected_targets"] = targets[:5]
    note = (
        "产业硬变量线覆盖：来源属于 SEMI/TrendForce/DIGITIMES/The Elec/Nikkei xTECH，"
        "且内容包含设备、材料、产能、资本开支、涨价、管制或订单等量化硬变量；"
        "即使具体 A 股映射待确认，也先即时推送并标注待验证。"
    )
    reason = str(updated.get("reason") or "").strip()
    if note not in reason:
        updated["reason"] = f"{reason}\n{note}".strip()
    raw = dict(updated.get("raw") or {})
    raw["industry_hardline_override"] = True
    updated["raw"] = raw
    return updated


def collect_hardline_text(*values: object) -> str:
    return " ".join(str(value or "") for value in values)


def source_family(source: str) -> str:
    if source.startswith("digitimes_"):
        return "DIGITIMES"
    if source.startswith("trendforce"):
        return "TrendForce"
    if source.startswith("nikkei_xtech"):
        return "Nikkei xTECH"
    if source.startswith("thelec_"):
        return "The Elec"
    if source.startswith("semi_"):
        return "SEMI"
    return source


def explain_hardline(source: str, text_parts: Iterable[object]) -> str:
    text = collect_hardline_text(*text_parts)
    effective = effective_source(source)
    if not is_hardline_source(effective):
        return ""
    if hardline_heuristic_matches(text):
        return f"{source_family(effective)} 命中设备/材料/产能/涨价/管制/订单等硬变量。"
    return f"{source_family(effective)} 属于产业硬变量线，但当前内容未明显命中硬变量关键词。"
