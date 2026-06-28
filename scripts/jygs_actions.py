#!/usr/bin/env python3
"""Low-frequency JiuYanGongShe action monitor and opportunity analysis."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from db_utils import connect_sqlite
from env_utils import load_env
from event_pipeline import content_hash, json_dumps, normalize_importance, should_push_analysis, utc_now
from feishu import send_card
from llm_analysis import call_chat_completion_with_prompts
from market_db import DEFAULT_DB_PATH, init_db


ROOT = Path(__file__).resolve().parents[1]
BJ = ZoneInfo("Asia/Shanghai")
BASE_URL = "https://app.jiuyangongshe.com/jystock-app"
WEB_URL = "https://www.jiuyangongshe.com/action"
PROMPT_VERSION = "jygs-action-v1"
PAGE_TIME_BY_PATH: dict[str, str] = {}
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/148.0.0.0 Safari/537.36 Edg/148.0.0.0"
)


JYGS_SYSTEM_PROMPT = """你是 A 股异动机会发现与复盘系统的投研助理。
任务：阅读一只股票的韭研公社异动解析，判断它的涨势是否可能持续，以及后续应该如何跟踪。
要求：
- 只输出 JSON，不要 Markdown，不要输出 JSON 外解释。
- 不要给无条件买入建议，只能输出研究信号、观察名单和风险提示。
- 必须判断是否为增量利好/增量利空/已有预期/利好落地/可能利好出尽/中性信息/无法判断。
- 必须判断题材强度、个股地位、持续性评分、预计持续时间、失效条件和复盘跟踪点。
- 对涨停或大涨股票，要特别防止“涨了所以继续涨”的循环论证；必须写反证和风险。
- 重点识别可能持续数周到数月的机会，但要克制，不能硬凑长期逻辑。
"""


JYGS_USER_PROMPT = """请分析以下异动条目，并输出 JSON：

字段格式：
{
  "importance": "high/medium/low",
  "core_content": "一句到两句中文核心内容",
  "incremental_view": {
    "classification": "增量利好/增量利空/已有预期/符合预期/利好落地/可能利好出尽/中性信息/无法判断",
    "surprise_level": "高/中/低/无法判断",
    "priced_in": "大概率已定价/部分定价/尚未充分定价/无法判断",
    "reason": "为什么这么判断"
  },
  "prediction": {
    "direction": "继续上涨/震荡分化/冲高回落/下跌/无法判断",
    "duration_bucket": "不持续/盘中或次日/1-3个交易日/1-2周/1-3个月/季度以上/无法判断",
    "continuation_score": 0,
    "confidence": "高/中/低",
    "thesis": "持续性的核心依据",
    "invalidation": "什么情况说明判断错了"
  },
  "theme_strength": {
    "score": 0,
    "reason": "题材强度、扩散程度、政策/产业/资金催化"
  },
  "stock_position": {
    "role": "龙头/补涨/跟风/低位挖掘/趋势核心/无法判断",
    "reason": "个股在题材里的地位"
  },
  "tracking_points": ["后续跟踪点1", "后续跟踪点2"],
  "risks": ["风险1", "风险2"],
  "watchlist_view": "是否进入观察名单及理由"
}

异动条目：
{content}
"""


class JygsError(RuntimeError):
    """Raised when JYGS cannot be read safely."""


def today_bj() -> str:
    return datetime.now(BJ).date().isoformat()


def current_slot() -> str:
    now = datetime.now(BJ)
    return "12:30" if now.hour < 15 else "16:00"


def env_int(name: str, default: int, minimum: int = 1) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return max(minimum, int(raw))
    except ValueError:
        return default


def debug_enabled() -> bool:
    return os.getenv("JYGS_API_DEBUG", "").strip().lower() in {"1", "true", "yes", "on"}


def sanitize_for_log(value: Any) -> Any:
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            key_lower = str(key).lower()
            if key_lower in {"cookie", "token", "session", "sessiontoken", "authorization"}:
                sanitized[str(key)] = "<redacted>"
            else:
                sanitized[str(key)] = sanitize_for_log(item)
        return sanitized
    if isinstance(value, list):
        return [sanitize_for_log(item) for item in value]
    return value


def debug_log(label: str, value: Any) -> None:
    if not debug_enabled():
        return
    print(f"JYGS debug {label}: {json.dumps(sanitize_for_log(value), ensure_ascii=False)[:1200]}", file=sys.stderr, flush=True)


def jygs_cookie() -> str:
    cookie = os.getenv("JYGS_COOKIE", "").strip()
    if cookie:
        return cookie
    session = os.getenv("JYGS_SESSION", "").strip()
    if session:
        return f"SESSION={session}"
    return ""


def sign_headers(extra_headers: dict[str, str] | None = None) -> dict[str, str]:
    sign_secret = os.getenv("JYGS_SIGN_SECRET", "").strip()
    if not sign_secret:
        raise JygsError("缺少 JYGS_SIGN_SECRET。请只在私有 .env 中配置，开源仓库不要提交该值。")
    ts = str(int(time.time() * 1000))
    token = hashlib.md5(f"{sign_secret}:{ts}".encode("utf-8")).hexdigest()
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
        "Cache-Control": "no-cache",
        "Content-Type": "application/json",
        "Origin": "https://www.jiuyangongshe.com",
        "Pragma": "no-cache",
        "Referer": "https://www.jiuyangongshe.com/",
        "User-Agent": os.getenv("JYGS_USER_AGENT", "").strip() or BROWSER_USER_AGENT,
        "Platform": "3",
        "Timestamp": ts,
        "Token": token,
    }
    cookie = jygs_cookie()
    if cookie:
        headers["Cookie"] = cookie
    if extra_headers:
        headers.update(extra_headers)
    return headers


def post_api(path: str, payload: dict[str, Any], extra_headers: dict[str, str] | None = None) -> dict[str, Any]:
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    headers = sign_headers(extra_headers)
    debug_log("request", {"path": path, "payload": payload, "headers": headers})
    request = urllib.request.Request(
        f"{BASE_URL}{path}",
        data=data,
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            body = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise JygsError(f"韭研公社 API HTTP {exc.code}: {body[:500]}") from exc
    except urllib.error.URLError as exc:
        raise JygsError(f"韭研公社 API 网络错误：{exc}") from exc

    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as exc:
        raise JygsError(f"韭研公社 API 返回非 JSON：{body[:500]}") from exc
    debug_log("response", {"path": path, "errCode": parsed.get("errCode"), "msg": parsed.get("msg"), "serverTime": parsed.get("serverTime")})
    data_obj = parsed.get("data")
    if isinstance(data_obj, dict) and str(data_obj.get("pageNo") or "") == "1" and parsed.get("serverTime"):
        PAGE_TIME_BY_PATH[path] = str(parsed["serverTime"])
    err_code = str(parsed.get("errCode", "0"))
    if err_code not in {"0", ""}:
        message = str(parsed.get("msg") or "未知错误")
        if err_code == "1":
            raise JygsError(f"韭研公社登录态不可用：{message}。需要在 JYGS_COOKIE 配置用户正常登录态。")
        if err_code == "9":
            cookie_status = "已配置 JYGS_COOKIE/JYGS_SESSION" if jygs_cookie() else "未配置 JYGS_COOKIE/JYGS_SESSION"
            raise JygsError(
                f"韭研公社 API 返回 errCode=9: {message}。请求已到达后端但校验失败，当前{cookie_status}；"
                "已按浏览器形态使用 JSON 请求体。若仍失败，需要用浏览器 Network 对照 /api/v1/action/field 与 /api/v1/action/list 的完整请求细节。"
            )
        raise JygsError(f"韭研公社 API 返回错误 errCode={err_code}: {message}")
    return parsed


def fetch_action_fields(trade_date: str) -> list[dict[str, Any]]:
    response = post_api("/api/v1/action/field", {"date": trade_date, "pc": 1})
    data = response.get("data") or []
    return [item for item in data if isinstance(item, dict)]


def fetch_action_list(action_field_id: str, limit: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    start = 1
    while True:
        extra_headers = {}
        if start > 1 and PAGE_TIME_BY_PATH.get("/api/v1/action/list"):
            extra_headers["Page-Time"] = PAGE_TIME_BY_PATH["/api/v1/action/list"]
        response = post_api(
            "/api/v1/action/list",
            {
                "action_field_id": action_field_id,
                "pc": 1,
                "start": start,
                "limit": limit,
                "sort_price": 0,
                "sort_range": 0,
                "sort_time": 0,
            },
            extra_headers=extra_headers,
        )
        data = response.get("data") or []
        batch = [item for item in data if isinstance(item, dict)]
        rows.extend(batch)
        if len(batch) < limit:
            break
        start += len(batch)
        if start > env_int("JYGS_MAX_FETCH_ITEMS", 300, minimum=30):
            break
    return rows


def fetch_all_actions(trade_date: str) -> list[dict[str, Any]]:
    fields = fetch_action_fields(trade_date)
    rows: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    page_limit = env_int("JYGS_PAGE_LIMIT", 30, minimum=1)
    for field in fields:
        field_id = str(field.get("action_field_id") or field.get("id") or "").strip()
        if not field_id:
            continue
        for item in fetch_action_list(field_id, page_limit):
            article = item.get("article") if isinstance(item.get("article"), dict) else {}
            action_info = article.get("action_info") if isinstance(article.get("action_info"), dict) else {}
            key = str(
                action_info.get("article_id")
                or action_info.get("action_info_id")
                or article.get("article_id")
                or item.get("article_id")
                or item.get("id")
                or content_hash(json_dumps(item))
            )
            if key in seen_ids:
                continue
            seen_ids.add(key)
            item["_field"] = field
            rows.append(item)
    return rows


def clean_text(value: Any) -> str:
    text = str(value or "")
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).replace("\u3000", " ").strip()


def first_value(row: dict[str, Any], *keys: str) -> str:
    if not isinstance(row, dict):
        return ""
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return clean_text(value)
    return ""


def first_non_empty(*values: Any) -> str:
    for value in values:
        text = clean_text(value)
        if text:
            return text
    return ""


def as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def normalize_symbol(raw_symbol: str) -> str:
    symbol = clean_text(raw_symbol)
    if not symbol:
        return ""
    compact = symbol.strip().upper()
    if re.fullmatch(r"\d{6}\.(SZ|SH|BJ)", compact):
        return compact
    prefixed = re.fullmatch(r"(SZ|SH|BJ)(\d{6})", compact)
    if prefixed:
        return f"{prefixed.group(2)}.{prefixed.group(1)}"
    plain = re.fullmatch(r"\d{6}", compact)
    if plain:
        if compact.startswith("6"):
            return f"{compact}.SH"
        if compact.startswith(("0", "2", "3")):
            return f"{compact}.SZ"
        if compact.startswith(("4", "8", "9")):
            return f"{compact}.BJ"
    return compact


def format_jygs_price(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, int | float):
        return f"{float(value) / 100:.2f}" if abs(float(value)) >= 100 else f"{float(value):.2f}"
    return clean_text(value)


def format_jygs_pct(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, int | float):
        return f"{float(value) / 100:.2f}%"
    text = clean_text(value)
    return text if text.endswith("%") else text


def normalize_item(row: dict[str, Any], trade_date: str, run_slot: str) -> dict[str, Any]:
    article = as_dict(row.get("article"))
    action_info = as_dict(article.get("action_info"))
    field = as_dict(row.get("_field"))
    stock_list = row.get("stock_list") if isinstance(row.get("stock_list"), list) else []
    first_stock = stock_list[0] if stock_list and isinstance(stock_list[0], dict) else {}
    article_id = first_non_empty(
        first_value(action_info, "article_id"),
        first_value(article, "article_id"),
        first_value(row, "article_id", "id", "action_id"),
        content_hash(json_dumps(row))[:16],
    )
    symbol = normalize_symbol(
        first_non_empty(
            first_value(row, "stock_code", "code", "symbol"),
            first_value(first_stock, "stock_code", "code", "symbol"),
            first_value(action_info, "stock_code", "code", "symbol"),
        )
    )
    name = first_non_empty(
        first_value(row, "stock_name", "name", "title"),
        first_value(first_stock, "stock_name", "name"),
        first_value(article, "stock_name", "name"),
        "未知股票",
    )
    title = first_non_empty(first_value(article, "title", "article_title"), first_value(row, "title", "article_title"), name)
    full_text = first_non_empty(
        first_value(action_info, "expound", "content", "summary", "reason", "description"),
        first_value(article, "expound", "content", "summary", "reason", "description"),
        first_value(row, "expound", "content", "summary", "reason", "description"),
    )
    first_line = full_text.splitlines()[0].strip() if full_text else ""
    reason = first_non_empty(
        first_value(action_info, "reason"),
        first_value(row, "reason", "logic", "summary", "content"),
        first_value(article, "summary", "description"),
        first_line,
        title,
    )
    themes = first_non_empty(
        first_value(row, "plate_name", "field_name", "theme"),
        first_value(field, "name", "field_name"),
    )
    url = f"https://www.jiuyangongshe.com/a/{article_id}" if article_id else WEB_URL
    return {
        "trade_date": trade_date,
        "run_slot": run_slot,
        "symbol": symbol,
        "name": name,
        "latest_price": format_jygs_price(first_non_empty(first_value(action_info, "price"), first_value(row, "price", "latest_price"))),
        "change_pct": format_jygs_pct(action_info.get("shares_range") if action_info.get("shares_range") not in (None, "") else first_non_empty(first_value(row, "range", "change_pct", "increase"))),
        "board_status": first_non_empty(first_value(action_info, "num"), first_value(row, "num", "board_status")),
        "limit_up_time": first_non_empty(first_value(action_info, "time"), first_value(row, "time", "limit_up_time")),
        "themes": themes,
        "reason": reason or title,
        "full_text": full_text or reason or title,
        "url": url,
        "raw": row,
    }


def insert_jygs_event(item: dict[str, Any]) -> tuple[int, bool]:
    digest = content_hash(
        item.get("trade_date", ""),
        item.get("run_slot", ""),
        item.get("symbol", ""),
        item.get("name", ""),
        item.get("full_text", ""),
        item.get("reason", ""),
    )
    with connect_sqlite(DEFAULT_DB_PATH) as conn:
        try:
            cur = conn.execute(
                """
                INSERT INTO jygs_events (
                    trade_date, run_slot, symbol, name, latest_price, change_pct, board_status,
                    limit_up_time, themes, reason, full_text, url, raw_json, content_hash, first_seen_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.get("trade_date", ""),
                    item.get("run_slot", ""),
                    item.get("symbol", ""),
                    item.get("name", ""),
                    item.get("latest_price", ""),
                    item.get("change_pct", ""),
                    item.get("board_status", ""),
                    item.get("limit_up_time", ""),
                    item.get("themes", ""),
                    item.get("reason", ""),
                    item.get("full_text", ""),
                    item.get("url", ""),
                    json_dumps(item.get("raw") or {}),
                    digest,
                    utc_now(),
                ),
            )
            conn.commit()
            return int(cur.lastrowid), True
        except Exception as exc:
            if "UNIQUE" not in str(exc).upper():
                raise
            row = conn.execute(
                """
                SELECT id FROM jygs_events
                WHERE trade_date = ? AND run_slot = ? AND symbol = ? AND content_hash = ?
                """,
                (item.get("trade_date", ""), item.get("run_slot", ""), item.get("symbol", ""), digest),
            ).fetchone()
            if not row:
                raise
            return int(row[0]), False


def analyze_jygs_event(event_id: int) -> dict[str, Any]:
    with connect_sqlite(DEFAULT_DB_PATH) as conn:
        row = conn.execute(
            """
            SELECT trade_date, run_slot, symbol, name, latest_price, change_pct, board_status,
                   limit_up_time, themes, reason, full_text, url, raw_json
            FROM jygs_events WHERE id = ?
            """,
            (event_id,),
        ).fetchone()
    if not row:
        raise RuntimeError(f"韭研公社事件不存在：{event_id}")
    keys = [
        "trade_date",
        "run_slot",
        "symbol",
        "name",
        "latest_price",
        "change_pct",
        "board_status",
        "limit_up_time",
        "themes",
        "reason",
        "full_text",
        "url",
        "raw_json",
    ]
    payload = dict(zip(keys, row, strict=False))
    parsed, model = call_chat_completion_with_prompts(
        JYGS_SYSTEM_PROMPT,
        JYGS_USER_PROMPT.replace("{content}", json.dumps(payload, ensure_ascii=False, indent=2)),
        user_agent="surveil-jygs-llm/0.1",
    )
    prediction = parsed.get("prediction") if isinstance(parsed.get("prediction"), dict) else {}
    with connect_sqlite(DEFAULT_DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO stock_predictions (
                source, source_id, symbol, name, trade_date, prediction_direction,
                duration_bucket, confidence, thesis, invalidation, model,
                prompt_version, analysis_json, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "jygs",
                event_id,
                payload.get("symbol", ""),
                payload.get("name", ""),
                payload.get("trade_date", ""),
                str(prediction.get("direction") or ""),
                str(prediction.get("duration_bucket") or ""),
                str(prediction.get("confidence") or ""),
                str(prediction.get("thesis") or ""),
                str(prediction.get("invalidation") or ""),
                model,
                PROMPT_VERSION,
                json_dumps(parsed),
                utc_now(),
            ),
        )
        conn.commit()
    return parsed


def jygs_analysis_lines(parsed: dict[str, Any], model: str = "") -> list[str]:
    lines = ["【异动持续性解读】"]
    core = str(parsed.get("core_content") or "").strip()
    if core:
        lines.append(f"核心内容：{core}")
    importance = str(parsed.get("importance") or "").strip()
    if importance:
        lines.append(f"重要性：{importance}")
    incremental = parsed.get("incremental_view") if isinstance(parsed.get("incremental_view"), dict) else {}
    if incremental:
        parts = []
        if incremental.get("classification"):
            parts.append(str(incremental["classification"]))
        if incremental.get("surprise_level"):
            parts.append(f"超预期程度：{incremental['surprise_level']}")
        if incremental.get("priced_in"):
            parts.append(f"定价状态：{incremental['priced_in']}")
        reason = str(incremental.get("reason") or "").strip()
        if reason:
            parts.append(reason)
        if parts:
            lines.append("增量判断：" + "；".join(parts))
    prediction = parsed.get("prediction") if isinstance(parsed.get("prediction"), dict) else {}
    if prediction:
        parts = []
        for label, key in [
            ("方向", "direction"),
            ("持续时间", "duration_bucket"),
            ("持续性评分", "continuation_score"),
            ("置信度", "confidence"),
        ]:
            value = prediction.get(key)
            if value not in (None, ""):
                parts.append(f"{label}：{value}")
        if parts:
            lines.append("预测：" + "；".join(parts))
        thesis = str(prediction.get("thesis") or "").strip()
        if thesis:
            lines.append(f"持续依据：{thesis}")
        invalidation = str(prediction.get("invalidation") or "").strip()
        if invalidation:
            lines.append(f"失效条件：{invalidation}")
    theme_strength = parsed.get("theme_strength") if isinstance(parsed.get("theme_strength"), dict) else {}
    if theme_strength:
        lines.append(
            "题材强度："
            + "；".join(
                str(part)
                for part in [
                    f"评分 {theme_strength.get('score')}" if theme_strength.get("score") not in (None, "") else "",
                    theme_strength.get("reason") or "",
                ]
                if str(part).strip()
            )
        )
    stock_position = parsed.get("stock_position") if isinstance(parsed.get("stock_position"), dict) else {}
    if stock_position:
        role = str(stock_position.get("role") or "").strip()
        reason = str(stock_position.get("reason") or "").strip()
        lines.append("个股地位：" + "；".join(part for part in [role, reason] if part))
    tracking = [str(item).strip() for item in parsed.get("tracking_points", []) if str(item).strip()] if isinstance(parsed.get("tracking_points"), list) else []
    if tracking:
        lines.append("跟踪点：" + "；".join(tracking[:4]))
    risks = [str(item).strip() for item in parsed.get("risks", []) if str(item).strip()] if isinstance(parsed.get("risks"), list) else []
    if risks:
        lines.append("风险：" + "；".join(risks[:4]))
    watchlist = str(parsed.get("watchlist_view") or "").strip()
    if watchlist:
        lines.append(f"观察名单：{watchlist}")
    lines.append("说明：以上是模型生成的研究信号，不构成无条件买入建议。")
    if model:
        lines.append(f"模型：{model}")
    return lines


def jygs_event_card(event_id: int, parsed: dict[str, Any]) -> dict[str, Any]:
    from cards import div_markdown, md_escape, text_chunks

    with connect_sqlite(DEFAULT_DB_PATH) as conn:
        row = conn.execute(
            """
            SELECT trade_date, run_slot, symbol, name, latest_price, change_pct, board_status,
                   limit_up_time, themes, reason, full_text, url
            FROM jygs_events WHERE id = ?
            """,
            (event_id,),
        ).fetchone()
        prediction_row = conn.execute(
            """
            SELECT model FROM stock_predictions
            WHERE source = 'jygs' AND source_id = ?
            ORDER BY id DESC LIMIT 1
            """,
            (event_id,),
        ).fetchone()
    if not row:
        raise RuntimeError(f"韭研公社事件不存在：{event_id}")
    (
        trade_date,
        run_slot,
        symbol,
        name,
        latest_price,
        change_pct,
        board_status,
        limit_up_time,
        themes,
        reason,
        full_text,
        url,
    ) = row
    model = str(prediction_row[0]) if prediction_row else ""
    title = f"{name} {symbol} 异动解析"
    facts = [
        f"**来源**：韭研公社 / 全部异动解析",
        f"**日期/场次**：{trade_date} {run_slot}",
        f"**标的**：{name} {symbol}",
        f"**题材**：{themes or '未知'}",
    ]
    market_parts = []
    if latest_price:
        market_parts.append(f"最新价 {latest_price}")
    if change_pct:
        market_parts.append(f"涨跌幅 {change_pct}")
    if board_status:
        market_parts.append(str(board_status))
    if limit_up_time:
        market_parts.append(f"涨停时间 {limit_up_time}")
    if market_parts:
        facts.append("**盘口/状态**：" + "；".join(market_parts))
    if reason:
        facts.append(f"**异动原因**：{reason}")
    elements = [div_markdown(md_escape(line)) for line in facts]
    elements.append({"tag": "hr"})
    for index, chunk in enumerate(text_chunks(full_text or reason or "", limit=1000), start=1):
        label = "原文全文" if index == 1 else f"原文全文（续 {index}）"
        elements.append(div_markdown(f"**{label}**\n{md_escape(chunk)}"))
    elements.append({"tag": "hr"})
    elements.append(div_markdown("**模型解读**\n" + md_escape("\n".join(jygs_analysis_lines(parsed, model)))))
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
        "header": {"template": "blue", "title": {"tag": "plain_text", "content": title[:60]}},
        "elements": elements,
    }


def deliver_jygs_event(event_id: int, parsed: dict[str, Any]) -> str:
    importance = normalize_importance(str(parsed.get("importance") or ""))
    if not should_push_analysis(parsed, importance):
        return "skipped"
    if not os.getenv("FEISHU_WEBHOOK", "").strip():
        return "skipped"
    send_card(jygs_event_card(event_id, parsed))
    return "sent"


def run(trade_date: str, run_slot: str, dry_run: bool, analyze: bool, deliver: bool, limit: int | None) -> int:
    init_db(DEFAULT_DB_PATH).close()
    rows = fetch_all_actions(trade_date)
    if limit:
        rows = rows[:limit]
    print(f"JYGS fetched rows={len(rows)} date={trade_date} slot={run_slot}", flush=True)
    new_count = 0
    for row in rows:
        item = normalize_item(row, trade_date, run_slot)
        if dry_run:
            print(f"[dry-run] {item['symbol']} {item['name']} {item['themes']} {item['reason'][:80]}", flush=True)
            continue
        event_id, inserted = insert_jygs_event(item)
        if not inserted:
            continue
        new_count += 1
        print(f"new jygs #{event_id}: {item['symbol']} {item['name']}", flush=True)
        if analyze:
            parsed = analyze_jygs_event(event_id)
            print(f"prediction #{event_id}: {parsed.get('core_content', '')}", flush=True)
            if deliver:
                status = deliver_jygs_event(event_id, parsed)
                print(f"delivery #{event_id}: {status}", flush=True)
    print(f"JYGS finished: fetched={len(rows)}, new={new_count}", flush=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="韭研公社全部异动解析低频监控")
    parser.add_argument("--date", default=today_bj())
    parser.add_argument("--slot", default=current_slot())
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-analyze", action="store_true")
    parser.add_argument("--deliver", action="store_true", help="配置飞书后推送中/高重要性异动分析")
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    load_env(ROOT / ".env")
    try:
        return run(
            trade_date=args.date,
            run_slot=args.slot,
            dry_run=args.dry_run,
            analyze=not args.no_analyze,
            deliver=args.deliver,
            limit=args.limit,
        )
    except JygsError as exc:
        print(f"JYGS skipped: {exc}", file=sys.stderr, flush=True)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
