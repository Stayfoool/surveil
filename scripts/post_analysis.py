"""Lightweight Chinese analysis for market-related X posts."""

from __future__ import annotations

import re

from company_knowledge import A_SHARE_THEMES, COMPANIES
from llm_analysis import analyze_with_llm, llm_config

TICKER_RE = re.compile(r"\$[A-Z][A-Z0-9.]{0,9}\b")

THEME_KEYWORDS = {
    "卖方研报/评级/目标价": [
        "analyst",
        "analyst firm",
        "institutional report",
        "institutional reports",
        "price target",
        "pt",
        "bernstein",
        "mizuho",
    ],
    "MLCC/被动元件": [
        "mlcc",
        "capacitor",
        "capacitors",
        "passive component",
        "passive components",
        "high-capacitance",
        "0402",
        "x6s",
        "supplier capacity",
    ],
    "AI 算力/资本开支": [
        "ai",
        "capex",
        "hyperscaler",
        "openai",
        "anthropic",
        "xai",
        "gpu",
        "datacenter",
        "data center",
        "neocloud",
    ],
    "AI 应用/大模型": [
        "llm",
        "large language model",
        "deepseek",
        "chatgpt",
        "agentic",
        "ai application",
        "ai applications",
        "inference",
        "robotics",
    ],
    "先进封装/玻璃基板/PCB": [
        "advanced packaging",
        "cowos",
        "copos",
        "foplp",
        "glass core",
        "glass substrate",
        "substrate",
        "package substrate",
        "pcb",
        "interposer",
    ],
    "半导体设备/材料": [
        "equipment",
        "materials",
        "lithography",
        "etch",
        "deposition",
        "photoresist",
        "cleaning",
        "cmp",
        "fab tool",
        "fab tools",
    ],
    "光互连/CPO/激光器": [
        "photonics",
        "optical",
        "cpo",
        "npo",
        "laser",
        "cw laser",
        "eml",
        "pluggable",
        "interconnect",
    ],
    "存储/HBM/DRAM": [
        "memory",
        "hbm",
        "dram",
        "nand",
        "ssd",
        "micron",
        "sk hynix",
        "samsung",
    ],
    "半导体材料/衬底/外延": [
        "inp",
        "substrate",
        "epiwafer",
        "epitaxy",
        "sic",
        "soi",
        "wafer",
        "mbe",
    ],
    "估值/泡沫/融资风险": [
        "bubble",
        "valuation",
        "debt",
        "contagion",
        "correction",
        "fed tightening",
        "selling",
    ],
}

BULLISH_WORDS = [
    "beneficiary",
    "beneficiaries",
    "shortage",
    "constrained",
    "constraint",
    "pricing power",
    "scrambling",
    "supply crunch",
    "bottleneck",
    "demand",
    "undervalued",
    "monopoly",
    "ramp",
]

RISK_WORDS = [
    "bubble",
    "risk",
    "correction",
    "contagion",
    "debt",
    "selling",
    "tightening",
    "expensive",
    "stagnant",
]

REVIEW_WORDS = [
    "my ideas",
    "core three themes",
    "large losses",
    "blended average",
    "entry point",
    "medium term timeframes",
    "short term timeframes",
    "ended up green",
    "ended up red",
    "missed a few",
    "i mentioned",
    "i chose",
    "i still think",
    "i changed",
    "i'd prefer to judge",
]


def extract_tickers(text: str) -> list[str]:
    seen: set[str] = set()
    tickers: list[str] = []
    for ticker in TICKER_RE.findall(text):
        if ticker not in seen:
            seen.add(ticker)
            tickers.append(ticker)
    return tickers


def company_label(ticker: str) -> str:
    company = COMPANIES.get(ticker)
    if not company:
        return ticker
    cn = f"{company.cn_name} / " if company.cn_name else ""
    return f"{ticker}（{cn}{company.name}，{company.listing}）"


def contains_keyword(text: str, keyword: str) -> bool:
    keyword_lower = keyword.lower()
    if re.fullmatch(r"[a-z0-9]+", keyword_lower):
        return re.search(rf"(?<![a-z0-9]){re.escape(keyword_lower)}(?![a-z0-9])", text) is not None
    return keyword_lower in text


def detect_themes(text: str) -> list[str]:
    lower = text.lower()
    themes = []
    for theme, keywords in THEME_KEYWORDS.items():
        if any(contains_keyword(lower, keyword) for keyword in keywords):
            themes.append(theme)
    if "MLCC/被动元件" in themes and "存储/HBM/DRAM" in themes:
        if not any(contains_keyword(lower, keyword) for keyword in ["hbm", "dram", "nand", "ssd", "micron", "sk hynix"]):
            themes.remove("存储/HBM/DRAM")
    return themes


def is_author_portfolio_review(text: str) -> bool:
    lower = text.lower()
    hits = sum(1 for keyword in REVIEW_WORDS if keyword in lower)
    return hits >= 3 and ("green" in lower or "red" in lower or "loss" in lower)


def sentiment_hint(text: str) -> str:
    lower = text.lower()
    if is_author_portfolio_review(text):
        return "这是作者对自己过往投资想法、入场点和持仓表现的复盘，主要价值在于理解其偏好的中期主线和风险认知，不应直接外推为新的产业链利好或利空。"
    if any(contains_keyword(lower, word) for word in ["analyst", "price target", "pt", "institutional reports"]):
        return "偏观点可信度/市场情绪评论，不应直接外推为产业链利好或利空。"
    bullish = sum(1 for word in BULLISH_WORDS if contains_keyword(lower, word))
    risky = sum(1 for word in RISK_WORDS if contains_keyword(lower, word))
    if bullish > risky:
        return "偏利好供给紧张、定价权或需求上修相关公司，但仍要看估值和兑现节奏。"
    if risky > bullish:
        return "偏风险提示，重点关注估值、融资条件、资本开支放缓或主题交易过热。"
    return "方向需要结合上下文判断，先作为观察信号而非直接交易结论。"


def compact_summary(text: str, limit: int = 220) -> str:
    cleaned = " ".join(text.split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 3] + "..."


def chinese_core_summary(text: str, themes: list[str], tickers: list[str]) -> str:
    lower = text.lower()
    if is_author_portfolio_review(text):
        return "这条推文是作者复盘自己过去提出的投资想法：核心主线仍是 Neoclouds/能源、存储和光子/光互连；他承认短期并非所有想法都赚钱，尤其 CPO 延期假报告冲击了部分台湾 CPO 多头，同时强调应以几个月的中期维度而非几周来评估观点成败。"
    if "mlcc" in lower and ("asic" in lower or "accelerator" in lower):
        return "这篇内容指出云厂商自研 ASIC 加速器推动高端 MLCC 规格集中，AI 加速器单板用量和规格要求提升，而供应商扩产滞后，2H26 可能出现高端特殊 MLCC 结构性短缺。"
    if any(keyword in lower for keyword in ["copos", "foplp", "glass core", "glass substrate"]):
        return "这篇内容指向先进封装路线变化：CoPoS/FOPLP 和玻璃基板相关方案正在被更积极验证，核心变量是 AI/HPC 芯片封装产能、良率、材料设备配套和 PCB/封装基板供应链重估。"
    if "advanced packaging" in lower or "package substrate" in lower:
        return "这篇内容围绕先进封装和封装基板供需展开，重点是 AI 芯片从晶圆制造瓶颈延伸到后段封装、基板、材料和设备环节。"
    if "deepseek" in lower or "large language model" in lower or "llm" in lower:
        return "这篇内容围绕大模型或 AI 应用进展展开，核心看点是模型能力、商业化路径、算力成本和应用落地是否能转化为可持续收入。"
    if "bernstein" in lower and ("kioxia" in lower or "$intc" in lower or "pt" in lower):
        return "这条推文是在批评 Bernstein 等卖方研报/目标价的可靠性：作者举 Kioxia 看空判断和 Intel 目标价偏低作为例子，核心不是产业链新催化，而是提醒不要机械依赖面向散户传播的机构报告。"
    if "analyst" in lower and ("pt" in lower or "price target" in lower or "institutional report" in lower):
        return "这条推文主要讨论分析师报告或目标价的参考价值，属于市场观点可信度评论，不是明确的供需或订单催化。"
    if "mizuho" in lower and ("cpo" in lower or "optical" in lower):
        return "这条推文引用卖方研究，上调光引擎/CPO 相关预期，并强调 InP DFB 激光器仍是下一阶段光互连供应链的关键环节。"
    if "cw laser" in lower or "scrambling" in lower and "laser" in lower:
        return "这条推文强调 AI 加速器厂商可能在争抢连续波激光器产能，指向上游激光器和光引擎供应链紧张。"
    if "memory" in lower or "hbm" in lower or "dram" in lower:
        return "这条推文围绕存储周期和 AI 需求展开，重点是 HBM/DRAM 供需、价格和龙头盈利弹性。"
    if contains_keyword(lower, "ai") and (
        contains_keyword(lower, "capex")
        or contains_keyword(lower, "datacenter")
        or contains_keyword(lower, "superintelligence")
    ):
        return "这条推文讨论 AI 基础设施资本开支的持续性，核心变量是云厂商现金流、融资环境和上游半导体供给约束。"
    if themes:
        return f"这条推文主要围绕{'、'.join(themes)}展开，涉及产业链供需、估值重估和后续验证信号。"
    if tickers:
        return f"这条推文主要提到 {', '.join(tickers)} 等标的，需要结合原文语境判断是业绩催化、供给约束还是估值风险。"
    return "这条推文属于产业观点或市场观察，暂未识别出明确的单一交易方向。"


def a_share_impact(themes: list[str], text: str = "") -> list[str]:
    if is_author_portfolio_review(text):
        return [
            "A 股影响：",
            "最直接的潜在利好/观察：",
            "1. 中际旭创（300308.SZ，深交所创业板）：作者反复把 Photonics/CPO/光互连列为核心主线之一，对 AI 光模块链条的中期景气仍偏认可，但这条推文本身不是新增订单或业绩催化。",
            "2. 新易盛（300502.SZ，深交所创业板）：同属高速光模块方向，受益逻辑来自 AI 光互连中期趋势，而非本条推文的短期新增信息。",
            "3. 源杰科技（688498.SH，上交所科创板）：若 CPO/光引擎/激光器链条继续受关注，上游激光芯片方向具备主题相关性，但需跟踪真实订单和客户验证。",
            "潜在利空/风险：原文提到 CPO delays 的假报告曾显著冲击部分 CPO 多头，说明该方向对卖方报告、客户节奏和验证进展非常敏感；如果后续出现真实延迟或订单不及预期，A 股光模块/激光器链条可能阶段性承压。",
        ]

    if set(themes).issubset({"卖方研报/评级/目标价"}):
        return [
            "A 股影响：",
            "最直接的潜在利好/观察：无明确直接 A 股标的。这条推文核心是对卖方研报和目标价可靠性的评价，不是新增产业链订单、涨价或供需变化。",
            "潜在利空/风险：若类似报告影响市场情绪，可能短期扰动相关半导体/存储标的估值，但不构成独立基本面信号。",
        ]

    positives: list[tuple[str, str]] = []
    negatives: list[str] = []
    for theme in themes:
        mapping = A_SHARE_THEMES.get(theme)
        if not mapping:
            continue
        positives.extend(mapping.get("positive", []))
        negatives.extend(mapping.get("negative", []))
    lower = text.lower()
    if any(keyword in lower for keyword in ["dfb", "laser", "cw laser", "inp"]):
        positives.sort(
            key=lambda item: (
                0
                if any(word in item[1].lower() for word in ["激光", "dfb", "inp"])
                else 1
                if any(word in item[1] for word in ["光模块", "光器件", "光互连"])
                else 2
            )
        )
    elif any(keyword in lower for keyword in ["cpo", "optical", "photonics", "光"]):
        positives.sort(key=lambda item: 0 if any(word in item[1] for word in ["光模块", "光器件", "光互连"]) else 1)
    elif any(keyword in lower for keyword in ["hbm", "dram", "memory"]):
        positives.sort(key=lambda item: 0 if any(word in item[1] for word in ["内存", "存储"]) else 1)
    elif "mlcc" in lower:
        positives.sort(key=lambda item: 0 if any(word in item[1] for word in ["MLCC", "被动元件", "陶瓷"]) else 1)

    deduped: list[tuple[str, str]] = []
    seen: set[str] = set()
    for name, reason in positives:
        if name in seen:
            continue
        seen.add(name)
        deduped.append((name, reason))
        if len(deduped) >= 3:
            break

    lines = ["A 股影响："]
    if deduped:
        lines.append("最直接的潜在利好/观察：")
        for index, (name, reason) in enumerate(deduped, start=1):
            lines.append(f"{index}. {name}：{reason}")
    else:
        lines.append("潜在利好/观察：暂未从主题中映射出高相关 A 股标的。")
    if negatives:
        lines.append("潜在利空/风险：" + "；".join(list(dict.fromkeys(negatives))[:2]))
    else:
        lines.append("潜在利空/风险：主要关注主题交易过热、海外供应链缓解或资本开支不及预期。")
    return lines


def global_equity_impact(themes: list[str], text: str = "") -> list[str]:
    if is_author_portfolio_review(text):
        tickers = extract_tickers(text)
        highlighted = "；".join(company_label(ticker) for ticker in tickers[:8])
        return [
            "美股/海外影响：",
            f"作者提到的海外观察标的较多：{highlighted} 等。",
            "最直接的潜在利好/观察：NBIS、MU、MRVL、LITE、AAOI、AXTI、SIVE 等分别对应 Neoclouds/能源、存储、光互连和光子链条，是作者中期主线的代表。",
            "潜在利空/风险：这条更像组合复盘，不是新增基本面信息；对已经大涨或入场点较高的标的，作者也明确提示短期波动和买点风险。",
        ]

    positives: list[tuple[str, str]] = []
    negatives: list[str] = []
    for theme in themes:
        mapping = A_SHARE_THEMES.get(theme)
        if not mapping:
            continue
        positives.extend(mapping.get("us_positive", []))
        negatives.extend(mapping.get("us_negative", []))
    deduped: list[tuple[str, str]] = []
    seen: set[str] = set()
    for name, reason in positives:
        if name in seen:
            continue
        seen.add(name)
        deduped.append((name, reason))
        if len(deduped) >= 3:
            break
    lines = ["美股/海外影响："]
    if deduped:
        lines.append("最直接的潜在利好/观察：")
        for index, (name, reason) in enumerate(deduped, start=1):
            lines.append(f"{index}. {name}：{reason}")
    else:
        lines.append("最直接的潜在利好/观察：暂未从主题中映射出高相关海外上市标的。")
    if negatives:
        lines.append("潜在利空/风险：" + "；".join(list(dict.fromkeys(negatives))[:2]))
    else:
        lines.append("潜在利空/风险：关注需求不及预期、扩产快于预期或供应链瓶颈缓解。")
    return lines


def incremental_view(text: str, themes: list[str]) -> list[str]:
    lower = text.lower()
    if is_author_portfolio_review(text):
        return [
            "增量判断：已有预期/观点复盘；不是新的订单、涨价、产能、客户验证或业绩指引信息，不应判为增量利好或增量利空。",
            "持续性：作为作者中期主线偏好的确认，可能对跟踪框架有持续参考价值；但对股价本身更多是情绪和观点层面的影响，需等待后续基本面数据验证。",
        ]
    if any(
        keyword in lower
        for keyword in [
            "cut forecast",
            "lowered forecast",
            "revised down",
            "miss",
            "shortfall",
            "delay",
            "delayed",
            "cancel",
            "cancelled",
            "weak demand",
            "oversupply",
        ]
    ):
        return [
            "增量判断：增量利空；原文出现下修、延期、需求转弱或供给过剩等新的负面变化。",
            "持续性：如果影响订单、价格、产能利用率或盈利预期，可能阶段性持续；若只是单次事件，偏一次性。",
        ]
    if any(keyword in lower for keyword in ["forecast", "raised", "revised up", "surge", "shortage", "structural shortage", "beat"]):
        return [
            "增量判断：增量利好；新增供需/指引/短缺信息带来边际变化。",
            "持续性：如果涉及产能、订单或技术路线变化，通常偏阶段性持续到数据验证结束；若只是单次事件，更偏一次性。",
        ]
    if any(keyword in lower for keyword in ["already priced", "priced in", "consensus", "as expected", "in line", "widely expected"]):
        return [
            "增量判断：已有预期/符合预期，可能属于利好或利空落地；缺少超预期信息时，不应机械视为增量利好/利空。",
            "持续性：通常偏短，更多是情绪脉冲，后续要看能否转化为订单或业绩。",
        ]
    if any(keyword in lower for keyword in ["confirmed", "announced", "officially", "launch", "started shipments", "mass production"]):
        return [
            "增量判断：可能属于利好/利空落地；事件已经披露或进入兑现阶段，关键看是否超出此前市场预期。",
            "持续性：若只是预期兑现，短期影响可能偏一次性；若带来后续订单、价格或份额变化，才可能阶段性持续。",
        ]
    if "analyst" in lower or "price target" in lower or "pt" in lower:
        return [
            "增量判断：更像观点或情绪变化，不是基本面增量。",
            "持续性：通常偏一次性，影响更多来自市场讨论而非产业兑现。",
        ]
    if themes:
        return [
            "增量判断：需要结合是否有订单、涨价、产能、指引或验证数据判断，暂时不能机械判定。",
            "持续性：若只是新闻/观点，偏一次性；若对应供需或资本开支变化，可能阶段性持续。",
        ]
    return [
        "增量判断：无法判断；原文没有足够信息确认是增量利好/利空、已有预期，还是利好/利空落地。",
        "持续性：需要结合市场此前预期、股价反应、订单/价格/业绩数据再判断。",
    ]


def analyze_post_rule(text: str) -> list[str]:
    tickers = extract_tickers(text)
    themes = detect_themes(text)
    lines = [
        "【快速解读】",
        f"核心内容：{chinese_core_summary(text, themes, tickers)}",
    ]
    if themes:
        lines.append(f"主题：{'、'.join(themes)}")
    if tickers:
        lines.append("涉及标的：" + "；".join(company_label(ticker) for ticker in tickers))
    lines.extend(
        [
            f"初步影响：{sentiment_hint(text)}",
            *incremental_view(text, themes),
            *a_share_impact(themes, text),
            *global_equity_impact(themes, text),
            "跟踪点：后续是否有订单、涨价、产能约束、客户验证、业绩指引或产业链数据交叉验证。",
            "风险：这只是研究信号，不构成买入建议；需结合估值、仓位、流动性、财报窗口和消息是否已被市场定价。",
            "分析方式：本条为本地规则兜底解读，不是大模型语义分析。",
        ]
    )
    return lines


def llm_failure_lines(error: Exception | None = None) -> list[str]:
    config = llm_config()
    if config:
        _, base_url, model = config
        config_line = f"当前模型配置：{base_url} / {model}"
    else:
        config_line = "当前模型配置：未配置 LLM_API_KEY / LLM_BASE_URL / LLM_MODEL"
    reason = str(error or "LLM 未配置或返回空结果").strip()
    if len(reason) > 800:
        reason = reason[:797] + "..."
    return [
        "【LLM 解析失败】",
        "本条未生成投资解读，避免使用规则映射造成误导。",
        config_line,
        f"失败原因：{reason}",
        "处理建议：请检查模型 API Key、Base URL、模型名、网络连通性和模型响应格式；修复后可重新触发分析。",
    ]


def analyze_post(
    text: str,
    *,
    thinking_override: str | None = None,
    max_tokens_override: int | None = None,
) -> list[str]:
    try:
        llm_lines = analyze_with_llm(
            text,
            thinking_override=thinking_override,
            max_tokens_override=max_tokens_override,
        )
    except Exception as exc:
        print(f"LLM 分析失败：{exc}")
        return llm_failure_lines(exc)
    if not llm_lines:
        return llm_failure_lines()
    return llm_lines
