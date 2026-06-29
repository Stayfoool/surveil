"""LLM-backed market analysis using OpenAI-compatible chat APIs."""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from typing import Any

from env_utils import get_env


class LLMBalanceInsufficientError(RuntimeError):
    """Raised when the model provider reports insufficient balance."""


SYSTEM_PROMPT = """你是半导体、AI 基础设施和二级市场研究助理。
任务：阅读输入的文章/推文，只根据原文信息和常识性产业链知识输出中文研究简报。
要求：
- 不要输出无条件买入建议，只能用“研究候选”“观察”“潜在利好/利空”。
- 如果输入同时包含“推文原文”和“推文外链内容”，必须把外链标题、摘要、正文摘录纳入核心内容和影响判断；不要只分析推文本体。
- 如果外链抓取失败、受限、为空或是暂不支持的 PDF，要明确说明信息不足，不要臆测链接内容。
- A 股影响最多列 3 个最直接标的，必须写公司简称、代码、公司全称或上市地，以及简短原因。
- 美股/海外影响最多列 3 个最直接标的，必须写代码或上市地，以及简短原因。
- 必须判断这是不是“增量利好/增量利空/已有预期/利好落地/利空落地/可能利好出尽/可能利空出尽/中性信息”，并说明市场是否可能已定价。
- 对每个相关股票必须判断影响方向、影响程度、持续时间、一次性还是持续性，并写出判断依据。
- 如果原文不足以支撑具体标的，明确说“暂无明确直接标的”，不要硬凑。
- 区分供需/价格/订单/资本开支/技术路线/市场情绪，避免把无关内容机械映射到热门主题。
- 如果只是重复市场已知共识，或缺少超预期数据/订单/涨价/指引变化，要倾向标注为“已有预期/符合预期”“利好落地/利空落地”或“可能利好出尽/可能利空出尽”，不要机械判为增量利好。
- 用中文，结论要克制，给出风险和反证。
- 只输出 JSON，不要 Markdown，不要解释 JSON 外的内容。
"""


USER_PROMPT_TEMPLATE = """请分析以下内容，并输出 JSON：

字段格式：
{
  "core_content": "一句到两句中文核心内容",
  "themes": ["主题1", "主题2"],
  "incremental_view": {
    "classification": "增量利好/增量利空/已有预期/符合预期/利好落地/利空落地/可能利好出尽/可能利空出尽/中性信息/无法判断",
    "surprise_level": "高/中/低/无法判断",
    "priced_in": "大概率已定价/部分定价/尚未充分定价/无法判断",
    "reason": "为什么这么判断，要说明新增信息来自哪里，是否超预期"
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
    "positive": [
      {
        "name": "公司简称",
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
        "name": "公司简称",
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
  "tracking_points": ["后续跟踪点1", "后续跟踪点2"],
  "risks": ["风险1", "风险2"],
  "watchlist_view": "是否值得纳入观察名单及理由"
}

原文：
{content}
"""


def llm_config() -> tuple[str, str, str] | None:
    if os.getenv("SURVEIL_DISABLE_LLM", "").strip() == "1":
        return None
    api_key = get_env("LLM_API_KEY", "OPENAI_API_KEY", "DASHSCOPE_API_KEY", "DEEPSEEK_API_KEY")
    base_url = get_env("LLM_BASE_URL", "OPENAI_BASE_URL", "DASHSCOPE_BASE_URL", "DEEPSEEK_BASE_URL")
    model = get_env("LLM_MODEL", "OPENAI_MODEL", "DASHSCOPE_MODEL", "DEEPSEEK_MODEL")
    if api_key and not base_url and (model.startswith("deepseek") or os.getenv("DEEPSEEK_API_KEY")):
        base_url = "https://api.deepseek.com"
    if api_key and base_url and not model:
        model = "deepseek-chat" if "deepseek" in base_url.lower() else "gpt-4.1-mini"
    if not api_key or not base_url or not model:
        return None
    return api_key, base_url, model


def chat_completions_url(base_url: str) -> str:
    normalized = base_url.rstrip("/")
    if normalized.endswith("/chat/completions"):
        return normalized
    return f"{normalized}/chat/completions"


def input_limit() -> int:
    raw = os.getenv("LLM_MAX_INPUT_CHARS", "").strip()
    if not raw:
        return 12000
    try:
        return max(1000, int(raw))
    except ValueError:
        return 12000


def timeout_seconds() -> int:
    raw = os.getenv("LLM_TIMEOUT_SECONDS", "").strip()
    if not raw:
        return 90
    try:
        return max(15, int(raw))
    except ValueError:
        return 90


def retry_count() -> int:
    raw = os.getenv("LLM_RETRY_COUNT", "").strip()
    if not raw:
        return 2
    try:
        return max(0, min(5, int(raw)))
    except ValueError:
        return 2


def max_output_tokens() -> int:
    raw = os.getenv("LLM_MAX_OUTPUT_TOKENS", "").strip() or os.getenv("LLM_MAX_TOKENS", "").strip()
    if not raw:
        return 1200
    try:
        return max(300, min(8000, int(raw)))
    except ValueError:
        return 1200


def json_response_format_enabled(base_url: str) -> bool:
    raw = os.getenv("LLM_RESPONSE_FORMAT_JSON", "").strip().lower()
    if raw:
        return raw in {"1", "true", "yes", "y", "on", "是"}
    # DeepSeek's OpenAI-compatible API supports JSON object response format,
    # which materially reduces malformed JSON from flash-class models.
    return "deepseek" in base_url.lower()


def thinking_type(base_url: str, model: str) -> str:
    raw = os.getenv("LLM_THINKING_TYPE", "").strip().lower()
    if raw:
        return raw
    if "z.ai" in base_url.lower() and model.lower().startswith("glm-"):
        return "disabled"
    if "deepseek" in base_url.lower():
        return "disabled"
    return ""


def retry_sleep_seconds(attempt: int) -> float:
    raw = os.getenv("LLM_RETRY_SLEEP_SECONDS", "").strip()
    try:
        base = float(raw) if raw else 2.0
    except ValueError:
        base = 2.0
    return min(20.0, max(0.0, base) * (attempt + 1))


def is_retryable_error(exc: Exception) -> bool:
    if isinstance(exc, TimeoutError):
        return True
    if isinstance(exc, urllib.error.URLError):
        return True
    text = str(exc).lower()
    return "timed out" in text or "timeout" in text or "connection reset" in text


def log_llm_retry(message: str) -> None:
    if os.getenv("LLM_RETRY_LOG", "1").strip() == "0":
        return
    print(message, flush=True)


def call_chat_completion_with_prompts(
    system_prompt: str,
    user_prompt: str,
    *,
    user_agent: str = "surveil-llm-analysis/0.1",
    truncate_user_prompt: bool = True,
    thinking_override: str | None = None,
    max_tokens_override: int | None = None,
) -> tuple[dict[str, Any], str]:
    config = llm_config()
    if not config:
        raise RuntimeError("LLM 未配置")
    api_key, base_url, model = config
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": user_prompt.strip()[: input_limit()] if truncate_user_prompt else user_prompt.strip(),
            },
        ],
        "temperature": 0.2,
        "max_tokens": max_tokens_override or max_output_tokens(),
    }
    thinking = (thinking_override or thinking_type(base_url, model)).strip().lower()
    if "deepseek" in base_url.lower() and thinking == "enabled" and os.getenv("LLM_ALLOW_DEEPSEEK_THINKING", "").strip() != "1":
        thinking = "disabled"
    if thinking in {"enabled", "disabled"}:
        payload["thinking"] = {"type": thinking}
    if json_response_format_enabled(base_url):
        payload["response_format"] = {"type": "json_object"}
    request = urllib.request.Request(
        chat_completions_url(base_url),
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json",
            "User-Agent": user_agent,
        },
        method="POST",
    )
    last_error: Exception | None = None
    attempts = retry_count() + 1
    for attempt in range(attempts):
        started_at = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds()) as response:
                body = response.read().decode("utf-8", errors="replace")
            elapsed = time.monotonic() - started_at
            if attempt > 0:
                log_llm_retry(f"LLM 重试成功：第 {attempt + 1}/{attempts} 次，用时 {elapsed:.1f}s")
            break
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            lower_body = body.lower()
            if exc.code == 402 or "insufficient balance" in lower_body or "余额不足" in body:
                raise LLMBalanceInsufficientError(f"LLM 余额不足：{body}") from exc
            if 500 <= exc.code < 600 and attempt < attempts - 1:
                last_error = RuntimeError(f"LLM 请求失败：HTTP {exc.code}\n{body}")
                log_llm_retry(f"LLM 请求 HTTP {exc.code}，准备重试 {attempt + 2}/{attempts}")
                time.sleep(retry_sleep_seconds(attempt))
                continue
            raise RuntimeError(f"LLM 请求失败：HTTP {exc.code}\n{body}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            last_error = exc
            if attempt < attempts - 1 and is_retryable_error(exc):
                log_llm_retry(f"LLM 网络/超时错误：{exc}，准备重试 {attempt + 2}/{attempts}")
                time.sleep(retry_sleep_seconds(attempt))
                continue
            raise RuntimeError(f"LLM 网络请求失败：{exc}") from exc
    else:
        raise RuntimeError(f"LLM 网络请求失败：{last_error}")

    result = json.loads(body)
    choices = result.get("choices") or []
    if not choices:
        raise RuntimeError(f"LLM 响应缺少 choices：{body[:500]}")
    message = choices[0].get("message") or {}
    raw_content = str(
        message.get("content")
        or message.get("reasoning_content")
        or message.get("output_text")
        or ""
    ).strip()
    if not raw_content:
        raise RuntimeError("LLM 响应为空")
    return parse_json_object(raw_content), model


def call_chat_completion(
    content: str,
    *,
    thinking_override: str | None = None,
    max_tokens_override: int | None = None,
) -> tuple[dict[str, Any], str]:
    trimmed = content.strip()[: input_limit()]
    return call_chat_completion_with_prompts(
        SYSTEM_PROMPT,
        USER_PROMPT_TEMPLATE.replace("{content}", trimmed),
        truncate_user_prompt=False,
        thinking_override=thinking_override,
        max_tokens_override=max_tokens_override,
    )


def parse_json_object(raw: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.S)
    if fenced:
        try:
            parsed = json.loads(fenced.group(1))
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

    balanced = extract_balanced_json_object(raw)
    if balanced:
        try:
            parsed = json.loads(balanced)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        try:
            parsed = json.loads(raw[start : end + 1])
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
    raise ValueError("无法从 LLM 输出解析 JSON")


def extract_balanced_json_object(raw: str) -> str | None:
    start = raw.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escape = False
    for index in range(start, len(raw)):
        char = raw[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return raw[start : index + 1]
    return None


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def field(value: Any, key: str) -> str:
    if isinstance(value, dict):
        return str(value.get(key) or "").strip()
    return ""


def company_line(item: Any) -> str:
    if not isinstance(item, dict):
        return str(item)
    name = field(item, "name") or field(item, "code") or "未命名标的"
    code = field(item, "code")
    full_name = field(item, "full_name")
    listing = field(item, "listing")
    confidence = field(item, "confidence")
    impact_magnitude = field(item, "impact_magnitude")
    duration = field(item, "duration")
    persistence = field(item, "persistence")
    reason = field(item, "reason")
    label_parts = [part for part in [name, code] if part]
    suffix = "，".join(part for part in [full_name, listing] if part)
    label = " ".join(label_parts)
    if suffix:
        label = f"{label}（{suffix}）"
    qualifiers = []
    if impact_magnitude:
        qualifiers.append(f"影响：{impact_magnitude}")
    if duration:
        qualifiers.append(f"持续：{duration}")
    if persistence:
        qualifiers.append(persistence)
    if confidence:
        qualifiers.append(f"置信度：{confidence}")
    if qualifiers:
        reason = f"{reason}（{'，'.join(qualifiers)}）" if reason else "，".join(qualifiers)
    return f"{label}：{reason}" if reason else label


def append_equity_section(lines: list[str], title: str, section: Any) -> None:
    lines.append(f"{title}：")
    positives = as_list(section.get("positive") if isinstance(section, dict) else [])[:3]
    negatives = as_list(section.get("negative") if isinstance(section, dict) else [])[:3]
    if positives:
        lines.append("最直接的潜在利好/观察：")
        for index, item in enumerate(positives, start=1):
            lines.append(f"{index}. {company_line(item)}")
    else:
        lines.append("最直接的潜在利好/观察：暂无明确直接标的。")
    if negatives:
        lines.append("潜在利空/风险：")
        for index, item in enumerate(negatives, start=1):
            lines.append(f"{index}. {company_line(item)}")
    else:
        lines.append("潜在利空/风险：暂无明确直接标的。")


def format_llm_analysis(parsed: dict[str, Any], model: str) -> list[str]:
    lines = ["【LLM 快速解读】"]
    core = str(parsed.get("core_content") or "").strip()
    if core:
        lines.append(f"核心内容：{core}")
    themes = [str(item).strip() for item in as_list(parsed.get("themes")) if str(item).strip()]
    if themes:
        lines.append(f"主题：{'、'.join(themes[:6])}")
    incremental = parsed.get("incremental_view") or {}
    incremental_line_added = False
    if isinstance(incremental, dict):
        classification = field(incremental, "classification")
        surprise = field(incremental, "surprise_level")
        priced_in = field(incremental, "priced_in")
        reason = field(incremental, "reason")
        incremental_parts = []
        if classification:
            incremental_parts.append(classification)
        if surprise:
            incremental_parts.append(f"超预期程度：{surprise}")
        if priced_in:
            incremental_parts.append(f"定价状态：{priced_in}")
        if incremental_parts or reason:
            line = "；".join(incremental_parts)
            if reason:
                line = f"{line}。{reason}" if line else reason
            lines.append(f"增量判断：{line}")
            incremental_line_added = True
    elif str(incremental or "").strip():
        lines.append(f"增量判断：{str(incremental).strip()}")
        incremental_line_added = True
    if not incremental_line_added:
        lines.append("增量判断：无法判断。模型输出缺少明确增量分类，需人工复核是否为增量利好/利空、已有预期或利好/利空落地。")
    impact = str(parsed.get("initial_impact") or "").strip()
    if impact:
        lines.append(f"初步影响：{impact}")
    append_equity_section(lines, "A 股影响", parsed.get("a_share") or {})
    append_equity_section(lines, "美股/海外影响", parsed.get("global_equity") or {})
    tracking = [str(item).strip() for item in as_list(parsed.get("tracking_points")) if str(item).strip()]
    if tracking:
        lines.append("跟踪点：" + "；".join(tracking[:4]))
    risks = [str(item).strip() for item in as_list(parsed.get("risks")) if str(item).strip()]
    if risks:
        lines.append("风险：" + "；".join(risks[:4]))
    watchlist = str(parsed.get("watchlist_view") or "").strip()
    if watchlist:
        lines.append(f"观察名单：{watchlist}")
    lines.append("说明：以上是模型生成的研究信号，不构成无条件买入建议。")
    lines.append(f"模型：{model}")
    return lines


def analyze_with_llm(
    text: str,
    *,
    thinking_override: str | None = None,
    max_tokens_override: int | None = None,
) -> list[str] | None:
    if not text.strip() or not llm_config():
        return None
    parsed, model = call_chat_completion(
        text,
        thinking_override=thinking_override,
        max_tokens_override=max_tokens_override,
    )
    return format_llm_analysis(parsed, model)
