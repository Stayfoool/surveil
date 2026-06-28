#!/usr/bin/env python3
"""Generate a Markdown sample report for a date range."""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from env_utils import load_env
from event_pipeline import analyze_event
from market_db import DEFAULT_DB_PATH, init_db


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPORT_DIR = ROOT / "reports"
RELEVANT_JYGS_THEMES = {
    "PCB",
    "半导体",
    "光通信",
    "AI硬件",
    "被动元件",
    "玻璃基板封装",
    "算力",
    "AI配电",
    "电子布",
    "商业航天",
    "机器人",
}
NOTICE_PRIORITY_WORDS = [
    "回购",
    "贷款承诺函",
    "自愿性",
    "投资者关系",
    "担保",
    "质押",
    "套期",
    "限制性股票",
    "权益分派",
]
NOTICE_BOILERPLATE_WORDS = [
    "法律意见书",
    "会议决议",
    "股东会决议",
    "职工代表",
    "名单",
    "核查意见",
]


def json_list(value: str) -> list[str]:
    try:
        parsed = json.loads(value or "[]")
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item) for item in parsed if str(item).strip()]


def short(value: str, limit: int = 160) -> str:
    text = " ".join((value or "").split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def fetch_rows(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...]) -> list[sqlite3.Row]:
    return list(conn.execute(sql, params).fetchall())


def select_notice_ids(rows: list[sqlite3.Row], limit: int) -> list[int]:
    selected: list[int] = []
    for row in rows:
        title = str(row["title"] or "")
        if any(word in title for word in NOTICE_PRIORITY_WORDS) and not any(
            word in title for word in NOTICE_BOILERPLATE_WORDS
        ):
            selected.append(int(row["id"]))
        if len(selected) >= limit:
            break
    return selected


def analyze_selected_notices(ids: list[int], task: str) -> dict[int, dict[str, Any]]:
    analyses: dict[int, dict[str, Any]] = {}
    for event_id in ids:
        try:
            analyses[event_id] = analyze_event(event_id, task=task, db_path=DEFAULT_DB_PATH)
        except Exception as exc:  # noqa: BLE001 - keep report generation best-effort
            analyses[event_id] = {"_error": str(exc)}
    return analyses


def render_report(start: str, end: str, notice_analysis_limit: int) -> str:
    init_db(DEFAULT_DB_PATH).close()
    conn = sqlite3.connect(DEFAULT_DB_PATH)
    conn.row_factory = sqlite3.Row

    holdings = fetch_rows(
        conn,
        "SELECT symbol, name, full_name FROM portfolio_holdings WHERE enabled=1 ORDER BY symbol",
        (),
    )
    sina = fetch_rows(
        conn,
        """
        SELECT id, title, summary, full_text, published_at, symbols_json, raw_json
        FROM events
        WHERE source='sina_flash' AND (published_at >= ? OR first_seen_at >= ?)
        ORDER BY published_at DESC, id DESC
        """,
        (start, start),
    )
    notices = fetch_rows(
        conn,
        """
        SELECT id, title, summary, full_text, published_at, symbols_json, raw_json
        FROM events
        WHERE source='ifind_notice' AND (published_at >= ? OR first_seen_at >= ?)
        ORDER BY published_at DESC, id DESC
        """,
        (start, start),
    )
    jygs_counts = fetch_rows(
        conn,
        """
        SELECT trade_date, run_slot, count(*) AS n
        FROM jygs_events
        WHERE trade_date >= ? AND trade_date <= ?
        GROUP BY trade_date, run_slot
        ORDER BY trade_date, run_slot
        """,
        (start, end),
    )
    theme_counts = fetch_rows(
        conn,
        """
        SELECT themes, count(*) AS n
        FROM jygs_events
        WHERE trade_date >= ? AND trade_date <= ?
        GROUP BY themes
        ORDER BY n DESC
        LIMIT 25
        """,
        (start, end),
    )
    placeholders = ",".join("?" for _ in sorted(RELEVANT_JYGS_THEMES))
    jygs_samples = fetch_rows(
        conn,
        f"""
        SELECT trade_date, symbol, name, themes, reason, full_text, change_pct, board_status, limit_up_time
        FROM jygs_events
        WHERE trade_date >= ? AND trade_date <= ?
          AND themes IN ({placeholders})
        ORDER BY trade_date DESC, id ASC
        LIMIT 45
        """,
        (start, end, *sorted(RELEVANT_JYGS_THEMES)),
    )
    preds = fetch_rows(
        conn,
        """
        SELECT trade_date, symbol, name, prediction_direction, duration_bucket, confidence,
               thesis, invalidation, model, analysis_json
        FROM stock_predictions
        WHERE source='jygs' AND trade_date >= ? AND trade_date <= ?
        ORDER BY trade_date DESC, id DESC
        LIMIT 20
        """,
        (start, end),
    )

    selected_notice_ids = select_notice_ids(notices, notice_analysis_limit)
    notice_analyses = analyze_selected_notices(selected_notice_ids, "sample_range_report")

    notice_by_symbol: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in notices:
        for symbol in json_list(row["symbols_json"]):
            notice_by_symbol[symbol].append(row)
    sina_by_symbol: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in sina:
        for symbol in json_list(row["symbols_json"]):
            sina_by_symbol[symbol].append(row)

    lines: list[str] = []
    lines.append(f"# 持仓监控样张报告（{start} 至 {end}）")
    lines.append("")
    lines.append(f"生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} CST")
    lines.append("")
    lines.append("## 数据完整性说明")
    lines.append("")
    lines.append("- 新浪财经快讯：当前数据库仅包含服务上线以来捕获的持仓相关新闻；新浪快讯源未在本系统里完成上周历史全量回溯，所以不能代表上周五以来全部新闻。")
    lines.append(f"- iFinD 公告：已按持仓股从 {start} 回溯至今抓取公告，并抽取 PDF 正文；本次共覆盖 {len(notices)} 条公告。")
    lines.append("- iFinD 研报/行业报告：当前账号暂无研报权限，研报定时器停用，本报告不含研报正文。")
    lines.append("- 韭研公社：已回补 2026-06-12、06-15、06-16、06-17、06-18 的 16:00 全日异动池；2026-06-19 当前返回 0 条。")
    lines.append("- 模型：当前使用服务器配置的 DeepSeek/OpenAI-compatible 模型；本报告只对部分公告做样张分析，未对全部韭研异动逐条调用模型。")
    lines.append("")

    lines.append("## 一句话结论")
    lines.append("")
    lines.append("- 持仓公告层面：上周五以来以投资者关系记录、权益分派、股权质押/担保、限制性股票激励、回购融资支持等为主，直接高强度增量利好暂不多；京东方、国瓷材料、德福科技、沃格光电的信息量相对更高。")
    lines.append("- 行业异动层面：韭研公社异动池里 PCB、半导体、光通信、AI硬件、被动元件、玻璃基板封装持续高频出现，和当前持仓中的中际旭创、源杰科技、沃格光电、国瓷材料、京东方、盛合晶微存在较强主题关联。")
    lines.append("- 需要重点改进：日报正式版要补 iFinD 新闻历史检索、行情表现和异动/持仓交叉影响分析，否则只能看事件本身，无法判断持续性兑现。")
    lines.append("")

    lines.append("## 持仓股逐项概览")
    lines.append("")
    for holding in holdings:
        symbol = str(holding["symbol"])
        name = str(holding["name"])
        lines.append(f"### {name}（{symbol}）")
        lines.append("")
        sina_rows = sina_by_symbol.get(symbol, [])
        if sina_rows:
            lines.append("新浪快讯：")
            for row in sina_rows[:5]:
                lines.append(f"- {row['published_at']}：{row['title']}")
        else:
            lines.append("新浪快讯：当前数据库未捕获到相关快讯。")
        notice_rows = notice_by_symbol.get(symbol, [])
        if notice_rows:
            lines.append("iFinD 公告：")
            for row in notice_rows[:8]:
                lines.append(f"- {row['published_at']}：{row['title']}（正文 {len(row['full_text'] or '')} 字）")
            if len(notice_rows) > 8:
                lines.append(f"- 另有 {len(notice_rows) - 8} 条公告未展开。")
        else:
            lines.append("iFinD 公告：本区间未抓到公告。")
        lines.append("")

    lines.append("## 新浪财经持仓相关新闻/快讯")
    lines.append("")
    if not sina:
        lines.append("当前数据库未捕获到区间内持仓快讯。")
    for row in sina:
        symbols = ", ".join(json_list(row["symbols_json"]))
        lines.append(f"- {row['published_at']} | {symbols} | {row['title']}")
    lines.append("")

    lines.append("## iFinD 公告与样张解读")
    lines.append("")
    lines.append(f"本区间公告共 {len(notices)} 条；以下先列模型样张解读，再列公告清单。")
    lines.append("")
    for row in notices:
        event_id = int(row["id"])
        if event_id not in selected_notice_ids:
            continue
        analysis = notice_analyses.get(event_id, {})
        lines.append(f"### {row['title']}")
        lines.append("")
        lines.append(f"- 发布时间：{row['published_at']}")
        lines.append(f"- 正文抽取：{len(row['full_text'] or '')} 字")
        if analysis.get("_error"):
            lines.append(f"- 模型解读失败：{analysis['_error']}")
        else:
            inc = analysis.get("incremental_view") or {}
            price = analysis.get("price_impact") or {}
            lines.append(f"- 重要性：{analysis.get('importance', '')}")
            lines.append(f"- 核心内容：{analysis.get('core_content', '')}")
            lines.append(
                f"- 增量判断：{inc.get('classification', '')}；"
                f"超预期：{inc.get('surprise_level', '')}；定价：{inc.get('priced_in', '')}"
            )
            lines.append(
                f"- 股价方向：{price.get('direction', '')}；影响幅度：{price.get('magnitude', '')}；"
                f"持续时间：{price.get('duration', '')}；属性：{price.get('persistence', '')}"
            )
            reason = inc.get("reason") or price.get("reason") or ""
            if reason:
                lines.append(f"- 理由：{reason}")
        lines.append("")

    lines.append("### 公告清单")
    lines.append("")
    for row in notices:
        symbols = ", ".join(json_list(row["symbols_json"]))
        lines.append(f"- {row['published_at']} | {symbols} | {row['title']}（正文 {len(row['full_text'] or '')} 字）")
    lines.append("")

    lines.append("## 韭研公社异动池")
    lines.append("")
    lines.append("### 每日数量")
    lines.append("")
    for row in jygs_counts:
        lines.append(f"- {row['trade_date']} {row['run_slot']}：{row['n']} 条")
    lines.append("")
    lines.append("### 高频主题")
    lines.append("")
    for row in theme_counts:
        lines.append(f"- {row['themes'] or '未分类'}：{row['n']} 条")
    lines.append("")
    lines.append("### 与持仓主题相关的异动样本")
    lines.append("")
    for row in jygs_samples:
        status = "；".join(
            part
            for part in [
                row["change_pct"] or "",
                row["board_status"] or "",
                f"涨停 {row['limit_up_time']}" if row["limit_up_time"] else "",
            ]
            if part
        )
        lines.append(
            f"- {row['trade_date']} | {row['themes']} | {row['name']}（{row['symbol']}）"
            f" | {status} | {short(row['reason'] or row['full_text'] or '')}"
        )
    lines.append("")

    lines.append("### 已有模型异动预测样本")
    lines.append("")
    if not preds:
        lines.append("暂无已入库的韭研公社模型预测。")
    for row in preds:
        lines.append(
            f"- {row['trade_date']} | {row['name']}（{row['symbol']}）"
            f" | 方向：{row['prediction_direction']} | 持续：{row['duration_bucket']}"
            f" | 置信度：{row['confidence']} | {row['thesis']}"
        )
    lines.append("")

    lines.append("## 下一步建议")
    lines.append("")
    lines.append("1. 正式实现 `daily_report.py`，每天自动生成 Markdown + 飞书卡片。")
    lines.append("2. 增加 iFinD 新闻历史检索，补齐新浪快讯无法回溯的问题。")
    lines.append("3. 对韭研公社异动池先做规则预筛，再只对高相关主题/高持续性候选调用模型，避免 700+ 条逐条消耗 token。")
    lines.append("4. 接入行情表现和复盘字段，跟踪 1/3/5/10/20 日收益、连板和最大回撤。")

    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="生成指定区间的持仓/公告/韭研公社样张报告")
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--output")
    parser.add_argument("--notice-analysis-limit", type=int, default=8)
    args = parser.parse_args()

    load_env(ROOT / ".env", override=True)
    report = render_report(args.start, args.end, max(0, args.notice_analysis_limit))
    output = Path(args.output) if args.output else DEFAULT_REPORT_DIR / f"sample_{args.start}_to_{args.end}.md"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(report, encoding="utf-8")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
