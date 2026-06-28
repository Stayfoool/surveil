#!/usr/bin/env python3
"""Batch import iFinD notices/reports for configured portfolio holdings."""

from __future__ import annotations

import argparse
import json
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from env_utils import get_env, load_env
from event_pipeline import analyze_event, content_hash, load_enabled_holdings, maybe_deliver_event, upsert_event
from feishu import send_card
from ifind_client import IfindClient, IfindNoDataError
from ifind_notice_pdf import parse_notice_pdf
from llm_analysis import LLMBalanceInsufficientError
from market_db import DEFAULT_DB_PATH, init_db
from portfolio_import import import_holdings


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = ROOT / "config" / "portfolio.json"
BJ = ZoneInfo("Asia/Shanghai")


DEFAULT_NOTICE_OUTPUT = "reportDate:Y,thscode:Y,secName:Y,ctime:Y,reportTitle:Y,pdfURL:Y,seq:Y"
DEFAULT_RESEARCH_OUTPUT = (
    "9926116a34:Y,affac2ba88:Y,bfe68d5844:Y,f100fdf0e8:Y,"
    "915a23893e:Y,383a6d166f:Y,dab16cb859:Y,e8d7b618b1:Y,029d48543d:Y"
)


def date_range(days: int) -> tuple[str, str]:
    today = datetime.now(BJ).date()
    start = today - timedelta(days=max(0, days - 1))
    return start.strftime("%Y-%m-%d"), today.strftime("%Y-%m-%d")


def parse_date(value: str) -> date | None:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def notice_query_date_range(days: int) -> tuple[str, str]:
    """iFinD often records evening notices under next day's reportDate."""
    start, end = date_range(days)
    end_date = parse_date(end)
    if not end_date:
        return start, end
    return start, (end_date + timedelta(days=1)).strftime("%Y-%m-%d")


def env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def chunked(values: list[str], size: int) -> list[list[str]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


def normalize_report_rows(response: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for table in response.get("tables") or []:
        if not isinstance(table, dict):
            continue
        table_data = table.get("table") if isinstance(table.get("table"), dict) else {}
        thscode = table.get("thscode") or table.get("code") or ""
        row_count = max((len(value) for value in table_data.values() if isinstance(value, list)), default=0)
        for index in range(row_count):
            row = {"thscode": thscode}
            for key, values in table_data.items():
                if isinstance(values, list) and index < len(values):
                    row[key] = values[index]
            rows.append(row)
    if rows:
        return rows

    # Some iFinD endpoints return a flat list under data/list.
    for key in ("data", "list"):
        data = response.get(key)
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
    return []


def empty_rows_for_no_data(exc: IfindNoDataError, label: str) -> list[dict[str, Any]]:
    print(f"iFinD {label} 无数据，跳过该批次。", flush=True)
    return []


def parse_json_object_env(name: str) -> dict[str, Any]:
    raw = get_env(name, default="")
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{name} 必须是 JSON object") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{name} 必须是 JSON object")
    return parsed


def parse_json_object_env_any(*names: str) -> dict[str, Any]:
    for name in names:
        if get_env(name, default=""):
            return parse_json_object_env(name)
    return {}


def safe_raw_row(kind: str, row: dict[str, Any]) -> dict[str, Any]:
    safe = dict(row)
    if kind == "notice":
        for key in ("pdfURL", "pdfUrl", "PDFURL", "url", "URL", "link", "链接", "bfe68d5844"):
            if key in safe:
                safe[key] = "<ifind_notice_url_redacted>"
    return safe


def event_from_report_row(
    kind: str,
    row: dict[str, Any],
    holding_by_symbol: dict[str, dict[str, Any]],
    *,
    parse_pdf: bool = True,
) -> dict[str, Any]:
    symbol = str(
        row.get("thscode")
        or row.get("THSCODE")
        or row.get("code")
        or row.get("e8d7b618b1")
        or row.get("证券代码")
        or ""
    ).strip().upper()
    title = str(
        row.get("reportTitle")
        or row.get("title")
        or row.get("annTitle")
        or row.get("noticeTitle")
        or row.get("report_title")
        or row.get("affac2ba88")
        or row.get("报告名称")
        or ""
    ).strip()
    seq = str(row.get("seq") or row.get("id") or row.get("SEQ") or content_hash(symbol, title, json.dumps(row, ensure_ascii=False))[:16])
    published_at = str(
        row.get("ctime")
        or row.get("reportDate")
        or row.get("publishDate")
        or row.get("date")
        or row.get("9926116a34")
        or row.get("研报发布日期")
        or ""
    ).strip()
    url = str(row.get("pdfURL") or row.get("url") or row.get("URL") or row.get("bfe68d5844") or row.get("链接") or "").strip()
    name = str(row.get("secName") or row.get("name") or row.get("SECNAME") or row.get("029d48543d") or row.get("证券名称") or "").strip()
    holding = holding_by_symbol.get(symbol, {})
    research_summary = str(row.get("f100fdf0e8") or row.get("研究摘要") or row.get("summary") or "").strip()
    summary = "；".join(
        part
        for part in [
            f"股票：{name or holding.get('name', '')} {symbol}".strip(),
            f"标题：{title}".strip(),
            f"发布时间：{published_at}".strip(),
            f"摘要：{research_summary}".strip(),
        ]
        if part and not part.endswith("：")
    )
    source = "ifind_notice" if kind == "notice" else "ifind_report"
    event_type = "announcement" if kind == "notice" else "research_report"
    full_text = ""
    raw = safe_raw_row(kind, row)
    if kind == "notice" and parse_pdf:
        full_text, pdf_meta = parse_notice_pdf(row)
        raw["_pdf_parse"] = pdf_meta
        if not full_text:
            raw["_text_quality"] = "公告 PDF 正文未抽取成功，模型只能基于标题/元数据保守判断。"
    return {
        "source": source,
        "source_event_id": f"{symbol}:{seq}",
        "event_type": event_type,
        "title": title or f"{name or symbol} {event_type}",
        "summary": summary,
        "full_text": full_text,
        "url": "" if kind == "notice" else url,
        "published_at": published_at,
        "symbols": [symbol] if symbol else [],
        "themes": [],
        "raw": raw,
        "content_hash": content_hash(source, symbol, seq, title, published_at, full_text[:2000]),
    }


def report_type_for(kind: str) -> str:
    if kind == "notice":
        return get_env("IFIND_NOTICE_REPORT_TYPE", default="")
    return get_env("IFIND_RESEARCH_REPORT_TYPE", "IFIND_REPORT_REPORT_TYPE", default="")


def outputpara_for(kind: str) -> str:
    if kind == "notice":
        return get_env("IFIND_NOTICE_OUTPUTPARA", default=DEFAULT_NOTICE_OUTPUT)
    return get_env("IFIND_RESEARCH_OUTPUTPARA", "IFIND_REPORT_OUTPUTPARA", default=DEFAULT_RESEARCH_OUTPUT)


def markdown_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("*", "\\*").replace("_", "\\_")


def card_div(content: str) -> dict[str, Any]:
    return {"tag": "div", "text": {"tag": "lark_md", "content": content}}


def analysis_brief(analysis: dict[str, Any]) -> str:
    failure = analysis.get("_analysis_failed")
    if failure:
        return f"模型解析失败：{failure}"
    for key in ("core_content", "classification", "summary", "incremental_judgment"):
        value = str(analysis.get(key) or "").strip()
        if value:
            return value
    return "未生成解读"


def normalize_analysis_importance(analysis: dict[str, Any]) -> str:
    if analysis.get("_analysis_failed"):
        return "analysis_failed"
    return str(analysis.get("importance") or analysis.get("impact_level") or "unknown").strip() or "unknown"


def failed_analysis_payload(exc: Exception) -> dict[str, Any]:
    message = str(exc).strip()
    if len(message) > 1000:
        message = message[:1000] + "..."
    if "余额不足" in message or "insufficient balance" in message.lower():
        core_content = "本条公告模型解析失败，当前大模型余额不足。"
        reason = "模型服务返回余额不足，不能继续生成投资解读。"
    else:
        core_content = "本条公告模型解析失败，未生成投资解读。"
        reason = "LLM 调用失败，不能可靠判断增量性。"
    return {
        "importance": "analysis_failed",
        "core_content": core_content,
        "incremental_view": {
            "classification": "无法判断",
            "surprise_level": "无法判断",
            "priced_in": "无法判断",
            "reason": reason,
        },
        "price_impact": {
            "direction": "无法判断",
            "magnitude": "无法判断",
            "duration": "无法判断",
            "persistence": "无法判断",
            "reason": reason,
        },
        "initial_impact": core_content,
        "push_decision": {"should_push": False, "reason": reason},
        "_analysis_failed": message,
    }


def send_batch_summary_card(
    *,
    kind: str,
    start_date: str,
    end_date: str,
    fetched: int,
    new_count: int,
    processed: list[dict[str, Any]],
    skipped_seen: int,
    deliver: bool,
    batch_warning: str = "",
    analysis_skipped_count: int = 0,
) -> None:
    if kind != "notice" or not deliver:
        return
    if os.getenv("IFIND_NOTICE_SUMMARY_ENABLED", "1").strip() == "0":
        return
    webhook = os.getenv("FEISHU_WEBHOOK", "").strip()
    if not webhook:
        print("iFinD notice summary skipped: FEISHU_WEBHOOK 未配置", flush=True)
        return
    pushed = [item for item in processed if item.get("delivery_status") == "sent"]
    skipped = [item for item in processed if item.get("delivery_status") == "skipped"]
    failed = [item for item in processed if item.get("delivery_status") == "failed"]
    elements: list[dict[str, Any]] = [
        card_div(f"**查询区间**：{markdown_escape(start_date)} 至 {markdown_escape(end_date)}"),
        card_div(
            "**本批次结果**："
            f"抓取 {fetched} 条；新增 {new_count} 条；已见 {skipped_seen} 条；"
            f"单独推送 {len(pushed)} 条；低重要性/未推送 {len(skipped)} 条；发送失败 {len(failed)} 条"
        ),
    ]
    if batch_warning:
        elements.append(card_div(f"**批次提示**：{markdown_escape(batch_warning)}"))
    if analysis_skipped_count:
        elements.append(card_div(f"**模型分析跳过**：{analysis_skipped_count} 条，因为模型余额不足或已达上限。"))
    if not processed:
        elements.append(card_div("**公告清单**\n本批次没有新增公告。"))
    else:
        elements.append({"tag": "hr"})
        elements.append(card_div("**新增公告清单**"))
        for index, item in enumerate(processed[:20], start=1):
            analysis = item.get("analysis") if isinstance(item.get("analysis"), dict) else {}
            importance = normalize_analysis_importance(analysis)
            status = str(item.get("delivery_status") or "not_sent")
            symbol = "、".join(item.get("symbols") or [])
            title = str(item.get("title") or "")
            brief = analysis_brief(analysis)
            elements.append(
                card_div(
                    f"{index}. **{markdown_escape(title)}**\n"
                    f"标的：{markdown_escape(symbol or '未知')}；重要性：{markdown_escape(importance)}；"
                    f"推送状态：{markdown_escape(status)}\n"
                    f"解读：{markdown_escape(brief[:300])}"
                )
            )
        if len(processed) > 20:
            elements.append(card_div(f"另有 {len(processed) - 20} 条新增公告未展开。"))
    if skipped_seen:
        elements.append({"tag": "hr"})
        elements.append(card_div(f"**已见公告**：{skipped_seen} 条，未重复推送。"))

    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "blue" if not failed else "orange",
            "title": {"tag": "plain_text", "content": "iFinD 持仓公告批次报告"},
        },
        "elements": elements,
    }
    try:
        sent = send_card(card)
    except Exception as exc:  # noqa: BLE001 - summary failure should not fail the batch
        print(f"iFinD notice summary delivery failed: {exc}", flush=True)
        return
    print(f"iFinD notice summary delivery: {'sent' if sent else 'skipped'}", flush=True)


def fetch_research_rows(
    client: IfindClient,
    symbols: list[str],
    start_date: str,
    end_date: str,
) -> list[dict[str, Any]]:
    formula_template = get_env("IFIND_RESEARCH_FORMULA", "IFIND_REPORT_FORMULA", default="")
    if formula_template:
        formula = (
            formula_template.replace("{codes}", ",".join(symbols))
            .replace("{start_date}", start_date)
            .replace("{end_date}", end_date)
            .replace("{outputpara}", outputpara_for("report"))
        )
        return normalize_report_rows(client.data_pool({"formula": formula}))

    reportname = get_env("IFIND_RESEARCH_REPORTNAME", "IFIND_REPORT_REPORTNAME", default="")
    if reportname:
        functionpara = parse_json_object_env_any("IFIND_RESEARCH_FUNCTIONPARA", "IFIND_REPORT_FUNCTIONPARA")
        functionpara.setdefault("startdate", start_date)
        functionpara.setdefault("enddate", end_date)
        functionpara.setdefault("codes", ",".join(symbols))
        payload = {
            "reportname": reportname,
            "functionpara": functionpara,
            "outputpara": get_env("IFIND_RESEARCH_OUTPUTPARA", "IFIND_REPORT_OUTPUTPARA", default=DEFAULT_RESEARCH_OUTPUT),
        }
        return normalize_report_rows(client.data_pool(payload))

    legacy_report_type = report_type_for("report")
    if legacy_report_type:
        rows: list[dict[str, Any]] = []
        for group in chunked(symbols, max(1, env_int("IFIND_BATCH_CODE_CHUNK", 20))):
            response = client.report_query(
                ",".join(group),
                begin_date=start_date,
                end_date=end_date,
                report_type=legacy_report_type,
                outputpara=outputpara_for("report"),
            )
            rows.extend(normalize_report_rows(response))
        return rows

    print(
        research_config_message()
    )
    return []


def has_research_config() -> bool:
    return any(
        [
            get_env("IFIND_RESEARCH_FORMULA", "IFIND_REPORT_FORMULA", default=""),
            get_env("IFIND_RESEARCH_REPORTNAME", "IFIND_REPORT_REPORTNAME", default=""),
            report_type_for("report"),
        ]
    )


def research_config_message() -> str:
    return (
        "iFinD 研报入口尚未配置。前端和官方示例显示 report_query/reportType 更偏公告查询；"
        "研报/机构研究建议配置 IFIND_RESEARCH_FORMULA=THS_RPT(...)，"
        "或 IFIND_RESEARCH_REPORTNAME + IFIND_RESEARCH_FUNCTIONPARA 走 /data_pool。"
    )


def run_batch(
    kind: str,
    days: int,
    analyze: bool,
    deliver: bool,
    dry_run: bool,
    limit: int | None,
    parse_pdf_dry_run: bool = False,
) -> int:
    init_db(DEFAULT_DB_PATH).close()
    import_holdings(DEFAULT_CONFIG_PATH, DEFAULT_DB_PATH)
    holdings = load_enabled_holdings(DEFAULT_DB_PATH)
    if not holdings:
        print("没有启用的持仓，跳过。")
        return 0

    report_type = report_type_for(kind)
    if kind == "notice" and not report_type:
        print("IFIND_NOTICE_REPORT_TYPE 未配置，先使用 iFinD 默认公告类型参数。")
        report_type = "901"
    if kind == "report" and not has_research_config():
        print(research_config_message())
        print("iFinD report batch finished: fetched=0, new=0")
        return 0

    if kind == "notice":
        start_date, end_date = notice_query_date_range(days)
    else:
        start_date, end_date = date_range(days)
    client = IfindClient.from_env()
    symbols = [holding["symbol"] for holding in holdings if holding.get("symbol")]
    holding_by_symbol = {holding["symbol"]: holding for holding in holdings}
    total_new = 0
    total_seen = 0
    total_existing = 0
    processed_summaries: list[dict[str, Any]] = []
    analysis_skipped_count = 0
    batch_warning = ""
    max_chunk = env_int("IFIND_BATCH_CODE_CHUNK", 20)
    max_events = limit if limit is not None else env_int("IFIND_BATCH_MAX_EVENTS", 200)
    llm_balance_exhausted = False

    if kind == "report":
        row_groups = [fetch_research_rows(client, symbols, start_date, end_date)]
    else:
        row_groups = []
        for group in chunked(symbols, max(1, max_chunk)):
            label = ",".join(group)
            try:
                response = client.report_query(
                    label,
                    begin_date=start_date,
                    end_date=end_date,
                    report_type=report_type,
                    outputpara=outputpara_for(kind),
                )
                row_groups.append(normalize_report_rows(response))
            except IfindNoDataError as exc:
                row_groups.append(empty_rows_for_no_data(exc, label))

    for rows in row_groups:
        for row in rows:
            if max_events and total_seen >= max_events:
                break
            total_seen += 1
            event = event_from_report_row(
                kind,
                row,
                holding_by_symbol,
                parse_pdf=(not dry_run or parse_pdf_dry_run),
            )
            if dry_run:
                text_len = len(str(event.get("full_text") or ""))
                pdf_status = ""
                raw = event.get("raw") if isinstance(event.get("raw"), dict) else {}
                pdf_meta = raw.get("_pdf_parse") if isinstance(raw.get("_pdf_parse"), dict) else {}
                if pdf_meta:
                    pdf_status = f" pdf={pdf_meta.get('status')} chars={pdf_meta.get('extracted_chars', 0)}"
                print(f"[dry-run] {event['source']} {event['source_event_id']} {event['title']} text={text_len}{pdf_status}")
                continue
            event_id, inserted = upsert_event(event, DEFAULT_DB_PATH)
            if inserted:
                total_new += 1
                print(f"new event #{event_id}: {event['title']}")
                analysis: dict[str, Any] = {}
                delivery_status = "not_sent"
                if analyze and not llm_balance_exhausted:
                    try:
                        analysis = analyze_event(event_id, task=f"{kind}_portfolio", db_path=DEFAULT_DB_PATH)
                    except LLMBalanceInsufficientError as exc:
                        llm_balance_exhausted = True
                        batch_warning = "大模型余额不足，后续公告已跳过模型分析。"
                        analysis_skipped_count += 1
                        analysis = failed_analysis_payload(exc)
                        delivery_status = "analysis_failed"
                        print(f"analysis #{event_id} failed: {exc}", flush=True)
                        print(batch_warning, flush=True)
                    except Exception as exc:  # noqa: BLE001 - one failed notice must not kill the batch
                        analysis = failed_analysis_payload(exc)
                        delivery_status = "analysis_failed"
                        print(f"analysis #{event_id} failed: {exc}", flush=True)
                    else:
                        print(f"analysis #{event_id}: {analysis.get('core_content', '')}")
                    if deliver:
                        if analysis.get("_analysis_failed"):
                            print(f"delivery #{event_id}: skipped_analysis_failed")
                        else:
                            delivery_status = maybe_deliver_event(event_id, analysis, db_path=DEFAULT_DB_PATH)
                            print(f"delivery #{event_id}: {delivery_status}")
                elif analyze and llm_balance_exhausted:
                    analysis_skipped_count += 1
                    analysis = failed_analysis_payload(RuntimeError("LLM 余额不足"))
                    delivery_status = "analysis_skipped_balance"
                processed_summaries.append(
                    {
                        "event_id": event_id,
                        "title": event.get("title", ""),
                        "symbols": event.get("symbols") or [],
                        "analysis": analysis,
                        "delivery_status": delivery_status,
                    }
                )
            else:
                total_existing += 1
                print(f"seen event #{event_id}: {event['title']}")
        if max_events and total_seen >= max_events:
            break

    send_batch_summary_card(
        kind=kind,
        start_date=start_date,
        end_date=end_date,
        fetched=total_seen,
        new_count=total_new,
        processed=processed_summaries,
        skipped_seen=total_existing,
        deliver=deliver,
        batch_warning=batch_warning,
        analysis_skipped_count=analysis_skipped_count,
    )
    print(f"iFinD {kind} batch finished: fetched={total_seen}, new={total_new}, range={start_date}..{end_date}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="iFinD 公告/研报批处理")
    parser.add_argument("--kind", choices=["notice", "report"], required=True)
    parser.add_argument("--days", type=int, default=2)
    parser.add_argument("--no-analyze", action="store_true")
    parser.add_argument("--deliver", action="store_true", help="配置飞书后才实际推送；未配置时记录 skipped")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--parse-pdf", action="store_true", help="dry-run 时也下载并抽取公告 PDF 正文")
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()

    load_env(ROOT / ".env")
    return run_batch(
        kind=args.kind,
        days=args.days,
        analyze=not args.no_analyze,
        deliver=args.deliver,
        dry_run=args.dry_run,
        limit=args.limit,
        parse_pdf_dry_run=args.parse_pdf,
    )


if __name__ == "__main__":
    raise SystemExit(main())
