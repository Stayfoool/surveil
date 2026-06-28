#!/usr/bin/env python3
"""Local-only web UI for portfolio holdings management."""

from __future__ import annotations

import argparse
import html
import json
import os
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

from db_utils import connect_sqlite
from env_utils import load_env
from holdings_store import (
    HoldingsError,
    holdings_diff,
    normalized_holdings,
    normalize_holdings_for_save,
    save_holdings,
    validate_holdings,
)
from market_db import DEFAULT_DB_PATH
from media_keyword_config import media_keyword_payload, save_media_keyword_config
from settings_store import save_settings, settings_payload


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787
BJ = ZoneInfo("Asia/Shanghai")

SERVICE_UNITS = [
    "surveil-x-stream.service",
    "surveil-rss-monitor.service",
    "surveil-trendforce-page-monitor.service",
    "surveil-sina-flash.service",
    "surveil-holdings-web.service",
    "surveil-proxy.service",
]

TIMER_UNITS = [
    "surveil-sina-stock-news.timer",
    "surveil-overseas-media.timer",
    "surveil-article-daily.timer",
    "surveil-ifind-notice.timer",
    "surveil-ifind-report.timer",
    "surveil-jygs-actions.timer",
]

LOG_FILES = [
    "x-stream.err.log",
    "rss-monitor.err.log",
    "trendforce-page-monitor.err.log",
    "overseas-media.err.log",
    "sina-flash.err.log",
    "sina-stock-news.err.log",
    "ifind-notice.err.log",
    "jygs-actions.err.log",
    "holdings-web.err.log",
]


def env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    return raw.lower() in {"1", "true", "yes", "y", "on", "是"}


def utc_window_for_day(day: str = "") -> tuple[str, str, str]:
    if day:
        start_local = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=BJ)
    else:
        start_local = datetime.now(BJ).replace(hour=0, minute=0, second=0, microsecond=0)
    end_local = start_local + timedelta(days=1)
    return (
        start_local.astimezone(timezone.utc).isoformat(),
        end_local.astimezone(timezone.utc).isoformat(),
        start_local.strftime("%Y-%m-%d"),
    )


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1", (table,)).fetchone()
    return row is not None


def normalize_time(value: str) -> str:
    return str(value or "")


def count_rows(conn: sqlite3.Connection, table: str, where: str = "", params: tuple[Any, ...] = ()) -> int:
    if not table_exists(conn, table):
        return 0
    query = f"SELECT COUNT(*) FROM {table}"
    if where:
        query += f" WHERE {where}"
    return int(conn.execute(query, params).fetchone()[0])


def grouped_counts(conn: sqlite3.Connection, table: str, field: str, where: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
    if not table_exists(conn, table):
        return []
    rows = conn.execute(
        f"""
        SELECT COALESCE({field}, '') AS key, COUNT(*) AS count
        FROM {table}
        WHERE {where}
        GROUP BY COALESCE({field}, '')
        ORDER BY count DESC, key
        """,
        params,
    ).fetchall()
    return [{"key": row["key"] or "unknown", "count": int(row["count"])} for row in rows]


def fetch_events_rows(day: str = "", source: str = "", kind: str = "", q: str = "", limit: int = 100) -> list[dict[str, Any]]:
    start_utc, end_utc, _ = utc_window_for_day(day)
    q_lower = q.strip().lower()
    source_lower = source.strip().lower()
    kind_lower = kind.strip().lower()
    rows: list[dict[str, Any]] = []
    with connect_sqlite(DEFAULT_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        if table_exists(conn, "events"):
            for row in conn.execute(
                """
                SELECT e.id, e.source, e.event_type, e.title, e.summary, e.url, e.published_at,
                       e.first_seen_at, e.baseline_only,
                       (
                         SELECT importance FROM event_analyses a
                         WHERE a.event_id = e.id
                         ORDER BY a.id DESC LIMIT 1
                       ) AS importance,
                       (
                         SELECT classification FROM event_analyses a
                         WHERE a.event_id = e.id
                         ORDER BY a.id DESC LIMIT 1
                       ) AS classification,
                       (
                         SELECT should_push FROM event_analyses a
                         WHERE a.event_id = e.id
                         ORDER BY a.id DESC LIMIT 1
                       ) AS should_push,
                       (
                         SELECT status FROM deliveries d
                         WHERE d.event_id = e.id
                         ORDER BY d.id DESC LIMIT 1
                       ) AS delivery_status
                FROM events e
                WHERE e.first_seen_at >= ? AND e.first_seen_at < ?
                ORDER BY e.first_seen_at DESC
                LIMIT 300
                """,
                (start_utc, end_utc),
            ):
                rows.append(
                    {
                        "kind": row["event_type"] or "event",
                        "source": row["source"],
                        "id": row["id"],
                        "title": row["title"],
                        "summary": row["summary"] or "",
                        "url": row["url"] or "",
                        "published_at": normalize_time(row["published_at"]),
                        "seen_at": normalize_time(row["first_seen_at"]),
                        "importance": row["importance"] or "",
                        "classification": row["classification"] or "",
                        "push": bool(row["should_push"]),
                        "delivery_status": row["delivery_status"] or "",
                        "baseline_only": bool(row["baseline_only"]),
                    }
                )
        if table_exists(conn, "article_reviews"):
            for row in conn.execute(
                """
                SELECT source, item_id, url, title, source_module, published_at, importance,
                       push_now, incremental_classification, daily_summary, reason, pushed_at, created_at
                FROM article_reviews
                WHERE created_at >= ? AND created_at < ?
                ORDER BY created_at DESC
                LIMIT 300
                """,
                (start_utc, end_utc),
            ):
                rows.append(
                    {
                        "kind": "article",
                        "source": row["source_module"] or row["source"],
                        "id": row["item_id"],
                        "title": row["title"],
                        "summary": row["daily_summary"] or row["reason"] or "",
                        "url": row["url"] or "",
                        "published_at": normalize_time(row["published_at"]),
                        "seen_at": normalize_time(row["created_at"]),
                        "importance": row["importance"] or "",
                        "classification": row["incremental_classification"] or "",
                        "push": bool(row["push_now"]),
                        "delivery_status": "sent" if row["pushed_at"] else "daily",
                        "baseline_only": False,
                    }
                )
        if table_exists(conn, "official_news_reviews"):
            for row in conn.execute(
                """
                SELECT source, item_id, url, title, published_at, importance, daily_summary,
                       reason, pushed_at, created_at
                FROM official_news_reviews
                WHERE created_at >= ? AND created_at < ?
                ORDER BY created_at DESC
                LIMIT 200
                """,
                (start_utc, end_utc),
            ):
                rows.append(
                    {
                        "kind": "official_news",
                        "source": row["source"],
                        "id": row["item_id"],
                        "title": row["title"],
                        "summary": row["daily_summary"] or row["reason"] or "",
                        "url": row["url"] or "",
                        "published_at": normalize_time(row["published_at"]),
                        "seen_at": normalize_time(row["created_at"]),
                        "importance": row["importance"] or "",
                        "classification": "",
                        "push": bool(row["pushed_at"]),
                        "delivery_status": "sent" if row["pushed_at"] else "daily",
                        "baseline_only": False,
                    }
                )
        if table_exists(conn, "seen_posts"):
            for row in conn.execute(
                """
                SELECT source, post_id, url, text, published_at, first_seen_at
                FROM seen_posts
                WHERE first_seen_at >= ? AND first_seen_at < ?
                ORDER BY first_seen_at DESC
                LIMIT 100
                """,
                (start_utc, end_utc),
            ):
                text = row["text"] or ""
                rows.append(
                    {
                        "kind": "x_post",
                        "source": row["source"],
                        "id": row["post_id"],
                        "title": text.splitlines()[0][:120] if text else f"X post {row['post_id']}",
                        "summary": text[:300],
                        "url": row["url"] or "",
                        "published_at": normalize_time(row["published_at"]),
                        "seen_at": normalize_time(row["first_seen_at"]),
                        "importance": "",
                        "classification": "",
                        "push": True,
                        "delivery_status": "sent",
                        "baseline_only": False,
                    }
                )
        if table_exists(conn, "jygs_events"):
            for row in conn.execute(
                """
                SELECT id, trade_date, run_slot, symbol, name, themes, reason, url, first_seen_at
                FROM jygs_events
                WHERE (first_seen_at >= ? AND first_seen_at < ?)
                   OR trade_date = ?
                ORDER BY first_seen_at DESC
                LIMIT 100
                """,
                (start_utc, end_utc, utc_window_for_day(day)[2]),
            ):
                rows.append(
                    {
                        "kind": "jygs",
                        "source": f"jygs/{row['run_slot']}",
                        "id": row["id"],
                        "title": f"{row['name']} {row['symbol'] or ''}".strip(),
                        "summary": row["reason"] or row["themes"] or "",
                        "url": row["url"] or "",
                        "published_at": row["trade_date"] or "",
                        "seen_at": normalize_time(row["first_seen_at"]),
                        "importance": "",
                        "classification": "",
                        "push": False,
                        "delivery_status": "",
                        "baseline_only": False,
                    }
                )

    def matches(item: dict[str, Any]) -> bool:
        if source_lower and source_lower not in str(item["source"]).lower():
            return False
        if kind_lower and kind_lower != str(item["kind"]).lower():
            return False
        if q_lower:
            hay = json.dumps(item, ensure_ascii=False).lower()
            if q_lower not in hay:
                return False
        return True

    rows = [item for item in rows if matches(item)]
    rows.sort(key=lambda item: str(item.get("seen_at") or item.get("published_at") or ""), reverse=True)
    return rows[: max(1, min(limit, 300))]


def overview_payload(day: str = "") -> dict[str, Any]:
    start_utc, end_utc, display_day = utc_window_for_day(day)
    with connect_sqlite(DEFAULT_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        deliveries_failed = count_rows(conn, "deliveries", "sent_at >= ? AND sent_at < ? AND status = 'failed'", (start_utc, end_utc))
        article_failures = 0
        if table_exists(conn, "article_reviews"):
            article_failures = int(
                conn.execute(
                    """
                    SELECT COUNT(*) FROM article_reviews
                    WHERE created_at >= ? AND created_at < ?
                      AND (reason LIKE '%失败%' OR gate_json LIKE '%error%')
                    """,
                    (start_utc, end_utc),
                ).fetchone()[0]
            )
        cards = [
            {"label": "统一事件", "value": count_rows(conn, "events", "first_seen_at >= ? AND first_seen_at < ?", (start_utc, end_utc))},
            {"label": "文章门控", "value": count_rows(conn, "article_reviews", "created_at >= ? AND created_at < ?", (start_utc, end_utc))},
            {"label": "X 新帖", "value": count_rows(conn, "seen_posts", "first_seen_at >= ? AND first_seen_at < ?", (start_utc, end_utc))},
            {"label": "韭研异动", "value": count_rows(conn, "jygs_events", "first_seen_at >= ? AND first_seen_at < ?", (start_utc, end_utc))},
            {"label": "飞书失败", "value": deliveries_failed + article_failures},
        ]
        by_source = grouped_counts(conn, "events", "source", "first_seen_at >= ? AND first_seen_at < ?", (start_utc, end_utc))
        article_importance = grouped_counts(conn, "article_reviews", "importance", "created_at >= ? AND created_at < ?", (start_utc, end_utc))
        deliveries = grouped_counts(conn, "deliveries", "status", "sent_at >= ? AND sent_at < ?", (start_utc, end_utc))
    return {
        "ok": True,
        "date": display_day,
        "cards": cards,
        "by_source": by_source[:12],
        "article_importance": article_importance,
        "deliveries": deliveries,
        "latest": fetch_events_rows(day=day, limit=10),
    }


def systemctl_show(unit: str) -> dict[str, str]:
    try:
        result = subprocess.run(
            ["systemctl", "show", unit, "--no-pager"],
            check=False,
            text=True,
            capture_output=True,
            timeout=8,
        )
    except Exception as exc:  # noqa: BLE001
        return {"Id": unit, "error": str(exc)}
    values: dict[str, str] = {"Id": unit}
    for line in result.stdout.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key in {
            "ActiveState",
            "SubState",
            "Result",
            "ExecMainStatus",
            "ExecMainPID",
            "NRestarts",
            "ExecMainStartTimestamp",
            "NextElapseUSecRealtime",
            "LastTriggerUSec",
            "LoadState",
        }:
            values[key] = value
    if result.returncode != 0:
        values["error"] = result.stderr.strip() or result.stdout.strip()
    return values


def tail_file(path: Path, max_lines: int = 8) -> str:
    if not path.exists():
        return ""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception as exc:  # noqa: BLE001
        return f"读取失败：{exc}"
    return "\n".join(lines[-max_lines:])


def health_payload() -> dict[str, Any]:
    units = [systemctl_show(unit) for unit in [*SERVICE_UNITS, *TIMER_UNITS]]
    logs_dir = ROOT / "logs"
    logs = []
    for name in LOG_FILES:
        tail = tail_file(logs_dir / name)
        if tail:
            logs.append({"name": name, "tail": tail})
    return {"ok": True, "units": units, "logs": logs}


def html_page(token_required: bool) -> str:
    token_hint = "需要访问令牌" if token_required else "未配置访问令牌，仅限 SSH 隧道使用"
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Surveil 工作台</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f6f7f9;
      --panel: #ffffff;
      --text: #1f2933;
      --muted: #6b7280;
      --line: #d8dde6;
      --accent: #176b87;
      --danger: #b42318;
      --ok: #0f766e;
    }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: var(--bg); color: var(--text); }}
    header {{ height: 56px; display: flex; align-items: center; gap: 16px; padding: 0 20px; background: #102a43; color: white; }}
    header h1 {{ font-size: 18px; margin: 0; font-weight: 650; }}
    nav.tabs {{ display: flex; gap: 8px; padding: 10px 20px 0; background: var(--bg); }}
    nav.tabs button {{ background: transparent; border-color: transparent; border-radius: 6px 6px 0 0; }}
    nav.tabs button.active {{ background: white; border-color: var(--line); border-bottom-color: white; color: var(--accent); }}
    main {{ padding: 18px 20px 32px; }}
    .view {{ display: none; }}
    .view.active {{ display: block; }}
    .toolbar {{ display: flex; flex-wrap: wrap; align-items: center; gap: 10px; margin-bottom: 14px; }}
    .panel {{ background: var(--panel); border: 1px solid var(--line); border-radius: 8px; overflow: hidden; }}
    .section-title {{ display: flex; align-items: center; justify-content: space-between; gap: 10px; margin: 0 0 12px; }}
    .section-title h2 {{ margin: 0; font-size: 16px; }}
    .metric-grid {{ display: grid; grid-template-columns: repeat(5, minmax(120px, 1fr)); gap: 10px; margin-bottom: 14px; }}
    .metric {{ background: white; border: 1px solid var(--line); border-radius: 8px; padding: 12px; }}
    .metric .label {{ color: var(--muted); font-size: 12px; }}
    .metric .value {{ font-size: 24px; font-weight: 700; margin-top: 6px; }}
    .split {{ display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 12px; }}
    .list {{ padding: 10px 12px; }}
    .list-row {{ border-bottom: 1px solid var(--line); padding: 9px 0; font-size: 13px; }}
    .list-row:last-child {{ border-bottom: 0; }}
    .badge {{ display: inline-block; border: 1px solid var(--line); border-radius: 999px; padding: 1px 7px; font-size: 12px; color: var(--muted); background: #fbfcfd; }}
    .badge.high {{ color: #9f1239; border-color: #fecdd3; background: #fff1f2; }}
    .badge.medium {{ color: #92400e; border-color: #fed7aa; background: #fff7ed; }}
    .badge.low {{ color: #166534; border-color: #bbf7d0; background: #f0fdf4; }}
    .log {{ white-space: pre-wrap; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; background: #0f172a; color: #dbeafe; padding: 10px; border-radius: 6px; overflow: auto; }}
    .summary {{ color: var(--muted); font-size: 13px; margin-left: auto; }}
    button {{ border: 1px solid var(--line); background: white; color: var(--text); height: 34px; padding: 0 12px; border-radius: 6px; cursor: pointer; font-weight: 550; }}
    button.primary {{ background: var(--accent); color: white; border-color: var(--accent); }}
    button.danger {{ color: var(--danger); border-color: #f1b7b0; }}
    button:disabled {{ opacity: .5; cursor: not-allowed; }}
    input, textarea, select {{ width: 100%; border: 1px solid var(--line); border-radius: 6px; padding: 8px; font: inherit; background: white; }}
    input[type="checkbox"] {{ width: auto; }}
    table {{ width: 100%; border-collapse: collapse; table-layout: fixed; }}
    th, td {{ border-bottom: 1px solid var(--line); padding: 8px; vertical-align: top; font-size: 13px; }}
    th {{ text-align: left; background: #eef2f6; color: #334e68; position: sticky; top: 0; z-index: 1; }}
    td.symbol {{ width: 112px; }}
    td.enabled {{ width: 70px; text-align: center; }}
    td.actions {{ width: 82px; text-align: center; }}
    td.name {{ width: 110px; }}
    td.full {{ width: 170px; }}
    td textarea {{ min-height: 38px; resize: vertical; }}
    .events-table td.summary-cell {{ color: var(--muted); }}
    .events-table a {{ color: var(--accent); text-decoration: none; }}
    .table-wrap {{ max-height: calc(100vh - 190px); overflow: auto; }}
    .status {{ white-space: pre-wrap; font-size: 13px; padding: 10px 12px; border: 1px solid var(--line); border-radius: 8px; background: white; margin-bottom: 12px; display: none; }}
    .status.ok {{ display: block; border-color: #99d6cc; color: var(--ok); }}
    .status.err {{ display: block; border-color: #f1b7b0; color: var(--danger); }}
    .modal-backdrop {{ position: fixed; inset: 0; background: rgba(15, 23, 42, .35); display: none; align-items: center; justify-content: center; padding: 20px; }}
    .modal {{ width: min(760px, 100%); background: white; border-radius: 8px; border: 1px solid var(--line); box-shadow: 0 20px 60px rgba(15, 23, 42, .25); }}
    .modal h2 {{ font-size: 16px; margin: 0; padding: 14px 16px; border-bottom: 1px solid var(--line); }}
    .modal .body {{ padding: 16px; }}
    .modal .foot {{ padding: 12px 16px; border-top: 1px solid var(--line); display: flex; justify-content: flex-end; gap: 10px; }}
    .grid {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }}
    .field label {{ display: block; font-size: 12px; color: var(--muted); margin-bottom: 4px; }}
    .settings-grid {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }}
    .settings-card {{ background: white; border: 1px solid var(--line); border-radius: 8px; padding: 12px; }}
    .settings-card h3 {{ margin: 0 0 8px; font-size: 15px; }}
    .setting-field {{ margin-top: 9px; }}
    .setting-field label {{ display: flex; align-items: center; justify-content: space-between; gap: 8px; font-size: 12px; color: var(--muted); margin-bottom: 4px; }}
    .setting-field input {{ height: 34px; }}
    .setting-mask {{ font-size: 12px; color: var(--muted); white-space: nowrap; }}
    .diff {{ max-height: 360px; overflow: auto; border: 1px solid var(--line); border-radius: 6px; padding: 10px; background: #fbfcfd; font-size: 13px; white-space: pre-wrap; }}
    .hint {{ color: var(--muted); font-size: 12px; margin-top: 6px; }}
    @media (max-width: 1000px) {{
      .metric-grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      .split {{ grid-template-columns: 1fr; }}
      .settings-grid {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body>
  <header>
    <h1>Surveil 工作台</h1>
    <span class="hint">{html.escape(token_hint)}</span>
  </header>
  <nav class="tabs">
    <button id="tab-overview" onclick="showView('overview')">今日总览</button>
    <button id="tab-events" onclick="showView('events')">事件中心</button>
    <button id="tab-health" onclick="showView('health')">任务健康</button>
    <button id="tab-keywords" onclick="showView('keywords')">媒体关键词</button>
    <button id="tab-settings" onclick="showView('settings')">配置中心</button>
    <button id="tab-holdings" onclick="showView('holdings')">持仓管理</button>
  </nav>
  <main>
    <div id="status" class="status"></div>
    <section id="view-overview" class="view">
      <div class="section-title">
        <h2>今日总览</h2>
        <button onclick="loadOverview()">刷新</button>
      </div>
      <div id="overviewMetrics" class="metric-grid"></div>
      <div class="split">
        <section class="panel">
          <div class="list" id="overviewBreakdown"></div>
        </section>
        <section class="panel">
          <div class="list" id="overviewLatest"></div>
        </section>
      </div>
    </section>

    <section id="view-events" class="view">
      <div class="toolbar">
        <input id="eventDate" type="date" style="width:160px">
        <input id="eventSource" placeholder="来源过滤" style="width:180px">
        <input id="eventQuery" placeholder="搜索标题、摘要、标的" style="width:260px">
        <button class="primary" onclick="loadEvents()">查询</button>
      </div>
      <section class="panel">
        <div class="table-wrap">
          <table class="events-table">
            <thead>
              <tr>
                <th style="width:130px">时间</th>
                <th style="width:130px">来源</th>
                <th style="width:90px">类型</th>
                <th>标题/摘要</th>
                <th style="width:110px">重要性</th>
                <th style="width:130px">状态</th>
              </tr>
            </thead>
            <tbody id="eventRows"></tbody>
          </table>
        </div>
      </section>
    </section>

    <section id="view-health" class="view">
      <div class="section-title">
        <h2>任务健康</h2>
        <button onclick="loadHealth()">刷新</button>
      </div>
      <section class="panel">
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Unit</th>
                <th style="width:120px">Active</th>
                <th style="width:120px">Sub</th>
                <th style="width:120px">Result</th>
                <th style="width:110px">Restarts</th>
                <th style="width:220px">最近启动/触发</th>
              </tr>
            </thead>
            <tbody id="healthRows"></tbody>
          </table>
        </div>
      </section>
      <div id="healthLogs" style="margin-top:12px"></div>
    </section>

    <section id="view-keywords" class="view">
      <div class="section-title">
        <h2>媒体关键词</h2>
        <div>
          <button onclick="loadKeywords()">刷新</button>
          <button onclick="resetBaseKeywords()">恢复代码默认词</button>
          <button class="primary" onclick="saveKeywords()">保存</button>
        </div>
      </div>
      <div class="split">
        <section class="panel">
          <div class="list">
            <div class="list-row"><strong>基础关键词</strong></div>
            <div class="list-row">
              <textarea id="baseKeywords" style="min-height:260px" placeholder="每行一个基础关键词"></textarea>
              <div class="hint">实际粗筛使用“基础关键词 + 额外包含关键词 - 排除关键词”。基础关键词可编辑；留空保存时会回到代码默认词。</div>
            </div>
            <div class="list-row"><strong>额外包含关键词</strong></div>
            <div class="list-row">
              <textarea id="includeKeywords" style="min-height:220px" placeholder="每行一个关键词，例如：金刚石散热"></textarea>
              <div class="hint">这些词会叠加到基础关键词上；RSS、DIGITIMES、The Elec、日经 xTECH 下一轮轮询会自动生效。</div>
            </div>
            <div class="list-row"><strong>排除关键词</strong></div>
            <div class="list-row">
              <textarea id="excludeKeywords" style="min-height:120px" placeholder="每行一个排除词"></textarea>
              <div class="hint">命中排除词的条目会在进入 LLM 门控前被过滤。</div>
            </div>
          </div>
        </section>
        <section class="panel">
          <div class="list">
            <div class="list-row"><strong>代码默认关键词</strong></div>
            <div class="hint">这是代码内置默认词，用于恢复基线；当前是否覆盖：<span id="baseOverrideStatus">-</span></div>
            <div id="defaultKeywords" class="list-row" style="max-height:560px; overflow:auto"></div>
          </div>
        </section>
      </div>
    </section>

    <section id="view-settings" class="view">
      <div class="section-title">
        <h2>配置中心</h2>
        <div>
          <button onclick="loadSettings()">刷新</button>
          <button class="primary" onclick="saveSettings()">保存</button>
        </div>
      </div>
      <div class="status ok" style="display:block">
敏感配置不会回显明文。敏感输入框留空表示保留现有值；输入新值才会覆盖服务器 .env。
保存后如需立即生效，请按页面提示重启对应服务。
      </div>
      <div id="settingsGrid" class="settings-grid"></div>
    </section>

    <section id="view-holdings" class="view">
      <div class="toolbar">
        <button class="primary" onclick="addRow()">新增</button>
        <button onclick="openBatch()">批量导入</button>
        <button onclick="reloadData()">刷新</button>
        <button class="primary" onclick="previewSave()">保存</button>
        <input id="filter" placeholder="搜索代码、名称、关键词" style="width:260px" oninput="renderTable()">
        <span id="summary" class="summary"></span>
      </div>
      <section class="panel">
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th style="width:70px">启用</th>
                <th style="width:118px">代码</th>
                <th style="width:120px">简称</th>
                <th style="width:190px">全称</th>
                <th>别名</th>
                <th>业务简介</th>
                <th>新闻关键词</th>
                <th>排除关键词</th>
                <th style="width:86px">操作</th>
              </tr>
            </thead>
            <tbody id="rows"></tbody>
          </table>
        </div>
      </section>
    </section>
  </main>

  <div id="batchModal" class="modal-backdrop">
    <div class="modal">
      <h2>批量导入</h2>
      <div class="body">
        <textarea id="batchText" style="min-height:240px" placeholder="每行一只股票，例如：&#10;源杰科技,688498.SH&#10;中际旭创,300308.SZ&#10;长飞光纤"></textarea>
        <div class="hint">支持“名称,代码”或“代码,名称”；只填一个值时会自动判断是否像股票代码。</div>
      </div>
      <div class="foot">
        <button onclick="closeBatch()">取消</button>
        <button class="primary" onclick="applyBatch()">加入列表</button>
      </div>
    </div>
  </div>

  <div id="diffModal" class="modal-backdrop">
    <div class="modal">
      <h2>保存前确认</h2>
      <div class="body">
        <div id="diffText" class="diff"></div>
      </div>
      <div class="foot">
        <button onclick="closeDiff()">取消</button>
        <button class="primary" onclick="confirmSave()">确认保存</button>
      </div>
    </div>
  </div>

<script>
let token = localStorage.getItem('surveil_holdings_token') || '';
let holdings = [];
let pendingPayload = null;
let loadedHoldings = false;
let codeDefaultKeywords = [];

function headers() {{
  const h = {{'Content-Type': 'application/json'}};
  if (token) h['X-Holdings-Token'] = token;
  return h;
}}

async function api(path, options={{}}) {{
  const res = await fetch(path, {{...options, headers: {{...headers(), ...(options.headers || {{}})}}}});
  if (res.status === 401) {{
    token = prompt('请输入 HOLDINGS_WEB_TOKEN') || '';
    localStorage.setItem('surveil_holdings_token', token);
    return api(path, options);
  }}
  const data = await res.json();
  if (!res.ok || data.ok === false) throw new Error(data.error || res.statusText);
  return data;
}}

function showStatus(text, kind='ok') {{
  const el = document.getElementById('status');
  el.className = 'status ' + kind;
  el.textContent = text;
}}

function splitList(value) {{
  return String(value || '').split(/[，,;；\\n]+/).map(s => s.trim()).filter(Boolean);
}}

function joinList(value) {{
  return Array.isArray(value) ? value.join('，') : '';
}}

function escapeHtml(value) {{
  return String(value ?? '').replace(/[&<>"']/g, c => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]));
}}

function badge(value) {{
  const raw = String(value || '').trim();
  if (!raw) return '<span class="badge">-</span>';
  const lower = raw.toLowerCase();
  const cls = ['high', 'medium', 'low'].includes(lower) ? lower : '';
  return `<span class="badge ${{cls}}">${{escapeHtml(raw)}}</span>`;
}}

function shortText(value, limit=160) {{
  const text = String(value || '').replace(/\\s+/g, ' ').trim();
  if (text.length <= limit) return text;
  return text.slice(0, limit - 3) + '...';
}}

function formatTime(value) {{
  if (!value) return '-';
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return String(value).slice(0, 19);
  return d.toLocaleString('zh-CN', {{hour12: false}});
}}

function todayString() {{
  const d = new Date();
  const year = d.getFullYear();
  const month = String(d.getMonth() + 1).padStart(2, '0');
  const day = String(d.getDate()).padStart(2, '0');
  return `${{year}}-${{month}}-${{day}}`;
}}

function showView(name) {{
  document.querySelectorAll('.view').forEach(el => el.classList.remove('active'));
  document.querySelectorAll('nav.tabs button').forEach(el => el.classList.remove('active'));
  document.getElementById(`view-${{name}}`).classList.add('active');
  document.getElementById(`tab-${{name}}`).classList.add('active');
  if (name === 'overview') loadOverview();
  if (name === 'events') loadEvents();
  if (name === 'health') loadHealth();
  if (name === 'keywords') loadKeywords();
  if (name === 'settings') loadSettings();
  if (name === 'holdings' && !loadedHoldings) reloadData();
}}

async function loadOverview() {{
  try {{
    const data = await api('/api/overview');
    const metrics = document.getElementById('overviewMetrics');
    metrics.innerHTML = (data.cards || []).map(item => `
      <div class="metric">
        <div class="label">${{escapeHtml(item.label)}}</div>
        <div class="value">${{escapeHtml(item.value)}}</div>
      </div>
    `).join('');
    const breakdown = [];
    breakdown.push('<div class="list-row"><strong>来源分布</strong></div>');
    (data.by_source || []).forEach(item => breakdown.push(`<div class="list-row">${{escapeHtml(item.key)}} <span class="summary">${{item.count}}</span></div>`));
    breakdown.push('<div class="list-row"><strong>文章重要性</strong></div>');
    (data.article_importance || []).forEach(item => breakdown.push(`<div class="list-row">${{badge(item.key)}} <span class="summary">${{item.count}}</span></div>`));
    breakdown.push('<div class="list-row"><strong>飞书状态</strong></div>');
    (data.deliveries || []).forEach(item => breakdown.push(`<div class="list-row">${{escapeHtml(item.key)}} <span class="summary">${{item.count}}</span></div>`));
    document.getElementById('overviewBreakdown').innerHTML = breakdown.join('') || '<div class="list-row">暂无统计。</div>';
    document.getElementById('overviewLatest').innerHTML = ['<div class="list-row"><strong>最近事件</strong></div>', ...(data.latest || []).map(item => `
      <div class="list-row">
        <div>${{badge(item.importance)}} <strong>${{escapeHtml(shortText(item.title, 120))}}</strong></div>
        <div class="hint">${{escapeHtml(item.source)}} / ${{escapeHtml(item.kind)}} / ${{formatTime(item.seen_at)}}</div>
      </div>
    `)].join('');
  }} catch (err) {{
    showStatus(err.message, 'err');
  }}
}}

async function loadEvents() {{
  try {{
    const params = new URLSearchParams();
    const date = document.getElementById('eventDate').value;
    const source = document.getElementById('eventSource').value.trim();
    const q = document.getElementById('eventQuery').value.trim();
    if (date) params.set('date', date);
    if (source) params.set('source', source);
    if (q) params.set('q', q);
    const data = await api('/api/events?' + params.toString());
    const rows = document.getElementById('eventRows');
    rows.innerHTML = (data.events || []).map(item => `
      <tr>
        <td>${{formatTime(item.seen_at || item.published_at)}}</td>
        <td>${{escapeHtml(item.source || '')}}</td>
        <td>${{escapeHtml(item.kind || '')}}</td>
        <td class="summary-cell">
          <div><strong>${{item.url ? `<a href="${{escapeHtml(item.url)}}" target="_blank" rel="noreferrer">${{escapeHtml(item.title || '')}}</a>` : escapeHtml(item.title || '')}}</strong></div>
          <div>${{escapeHtml(shortText(item.summary || '', 220))}}</div>
        </td>
        <td>${{badge(item.importance)}}<div class="hint">${{escapeHtml(item.classification || '')}}</div></td>
        <td>${{escapeHtml(item.delivery_status || '')}}${{item.push ? '<div class="hint">push</div>' : ''}}</td>
      </tr>
    `).join('') || '<tr><td colspan="6">没有匹配事件。</td></tr>';
  }} catch (err) {{
    showStatus(err.message, 'err');
  }}
}}

async function loadHealth() {{
  try {{
    const data = await api('/api/health');
    document.getElementById('healthRows').innerHTML = (data.units || []).map(unit => `
      <tr>
        <td>${{escapeHtml(unit.Id || '')}}</td>
        <td>${{badge(unit.ActiveState || unit.LoadState || '')}}</td>
        <td>${{escapeHtml(unit.SubState || '')}}</td>
        <td>${{escapeHtml(unit.Result || unit.error || '')}}</td>
        <td>${{escapeHtml(unit.NRestarts || '')}}</td>
        <td>${{escapeHtml(unit.ExecMainStartTimestamp || unit.LastTriggerUSec || unit.NextElapseUSecRealtime || '')}}</td>
      </tr>
    `).join('');
    document.getElementById('healthLogs').innerHTML = (data.logs || []).map(log => `
      <section class="panel" style="margin-top:12px">
        <div class="list-row" style="padding:10px 12px"><strong>${{escapeHtml(log.name)}}</strong></div>
        <div class="log">${{escapeHtml(log.tail || '')}}</div>
      </section>
    `).join('');
  }} catch (err) {{
    showStatus(err.message, 'err');
  }}
}}

function keywordTextToList(value) {{
  return String(value || '').split(/[，,;；\\n]+/).map(s => s.trim()).filter(Boolean);
}}

function keywordListToText(value) {{
  return Array.isArray(value) ? value.join('\\n') : '';
}}

function sameKeywordList(a, b) {{
  const left = (a || []).map(item => String(item || '').trim()).filter(Boolean);
  const right = (b || []).map(item => String(item || '').trim()).filter(Boolean);
  if (left.length !== right.length) return false;
  return left.every((item, index) => item === right[index]);
}}

async function loadKeywords() {{
  try {{
    const data = await api('/api/media-keywords');
    codeDefaultKeywords = data.code_default_keywords || data.default_keywords || [];
    document.getElementById('baseKeywords').value = keywordListToText(data.base_keywords || data.default_keywords || []);
    document.getElementById('includeKeywords').value = keywordListToText(data.include_keywords || []);
    document.getElementById('excludeKeywords').value = keywordListToText(data.exclude_keywords || []);
    document.getElementById('baseOverrideStatus').textContent = data.base_keywords_overridden ? '已自定义' : '使用代码默认';
    document.getElementById('defaultKeywords').innerHTML = codeDefaultKeywords.map(item => `<span class="badge" style="margin:2px">${{escapeHtml(item)}}</span>`).join('');
  }} catch (err) {{
    showStatus(err.message, 'err');
  }}
}}

function resetBaseKeywords() {{
  document.getElementById('baseKeywords').value = keywordListToText(codeDefaultKeywords);
  showStatus('已把基础关键词恢复为代码默认词，点击保存后生效。');
}}

async function saveKeywords() {{
  try {{
    const baseKeywords = keywordTextToList(document.getElementById('baseKeywords').value);
    const payload = {{
      base_keywords: sameKeywordList(baseKeywords, codeDefaultKeywords) ? [] : baseKeywords,
      include_keywords: keywordTextToList(document.getElementById('includeKeywords').value),
      exclude_keywords: keywordTextToList(document.getElementById('excludeKeywords').value)
    }};
    const data = await api('/api/media-keywords', {{method: 'POST', body: JSON.stringify(payload)}});
    codeDefaultKeywords = data.code_default_keywords || data.default_keywords || codeDefaultKeywords;
    document.getElementById('baseKeywords').value = keywordListToText(data.base_keywords || data.default_keywords || []);
    document.getElementById('includeKeywords').value = keywordListToText(data.include_keywords || []);
    document.getElementById('excludeKeywords').value = keywordListToText(data.exclude_keywords || []);
    document.getElementById('baseOverrideStatus').textContent = data.base_keywords_overridden ? '已自定义' : '使用代码默认';
    showStatus(`媒体关键词已保存。基础 ${{(data.base_keywords || data.default_keywords || []).length}} 个，额外包含 ${{(data.include_keywords || []).length}} 个，排除 ${{(data.exclude_keywords || []).length}} 个。`);
  }} catch (err) {{
    showStatus(err.message, 'err');
  }}
}}

async function loadSettings() {{
  try {{
    const data = await api('/api/settings');
    const grid = document.getElementById('settingsGrid');
    grid.innerHTML = (data.groups || []).map(group => `
      <section class="settings-card">
        <h3>${{escapeHtml(group.title || group.id || '')}}</h3>
        <div class="hint">${{escapeHtml(group.restart_hint || '')}}</div>
        ${{(group.fields || []).map(field => `
          <div class="setting-field">
            <label>
              <span>${{escapeHtml(field.label || field.key || '')}}</span>
              <span class="setting-mask">${{field.sensitive ? (field.configured ? '已配置 ' + escapeHtml(field.masked || '') : '未配置') : ''}}</span>
            </label>
            <input
              data-setting-key="${{escapeHtml(field.key || '')}}"
              data-sensitive="${{field.sensitive ? '1' : '0'}}"
              value="${{field.sensitive ? '' : escapeHtml(field.value || '')}}"
              placeholder="${{escapeHtml(field.sensitive ? '留空保留现有值；输入新值覆盖' : (field.placeholder || ''))}}"
              autocomplete="off"
            >
            ${{field.help ? `<div class="hint">${{escapeHtml(field.help)}}</div>` : ''}}
          </div>
        `).join('')}}
      </section>
    `).join('');
  }} catch (err) {{
    showStatus(err.message, 'err');
  }}
}}

async function saveSettings() {{
  try {{
    const values = {{}};
    document.querySelectorAll('[data-setting-key]').forEach(input => {{
      const key = input.dataset.settingKey;
      const sensitive = input.dataset.sensitive === '1';
      const value = input.value.trim();
      if (!key) return;
      if (sensitive && !value) return;
      values[key] = value;
    }});
    const data = await api('/api/settings', {{method: 'POST', body: JSON.stringify({{values}})}});
    const changed = (data.changed || []).map(item => `${{item.key}}: ${{item.old || '<空>'}} -> ${{item.new || '<空>'}}`).join('\\n');
    await loadSettings();
    showStatus(changed ? `配置已保存：\\n${{changed}}\\n\\n如需立即生效，请重启对应服务。` : '没有配置变化。');
  }} catch (err) {{
    showStatus(err.message, 'err');
  }}
}}

function readRow(row, item={{}}) {{
  return {{
    ...item,
    enabled: row.querySelector('[data-field="enabled"]').checked,
    symbol: row.querySelector('[data-field="symbol"]').value.trim(),
    name: row.querySelector('[data-field="name"]').value.trim(),
    full_name: row.querySelector('[data-field="full_name"]').value.trim(),
    aliases: splitList(row.querySelector('[data-field="aliases"]').value),
    business_summary: row.querySelector('[data-field="business_summary"]').value.trim(),
    news_keywords: splitList(row.querySelector('[data-field="news_keywords"]').value),
    news_exclude_keywords: splitList(row.querySelector('[data-field="news_exclude_keywords"]').value)
  }};
}}

function syncRowsFromDom() {{
  document.querySelectorAll('#rows tr[data-index]').forEach(row => {{
    const index = Number(row.dataset.index);
    if (Number.isInteger(index) && index >= 0 && index < holdings.length) {{
      holdings[index] = readRow(row, holdings[index] || {{}});
    }}
  }});
}}

function currentRows() {{
  syncRowsFromDom();
  return holdings.map(item => ({{
    enabled: item.enabled !== false,
    symbol: String(item.symbol || '').trim(),
    name: String(item.name || '').trim(),
    full_name: String(item.full_name || '').trim(),
    aliases: splitList(Array.isArray(item.aliases) ? item.aliases.join('，') : item.aliases),
    business_summary: String(item.business_summary || '').trim(),
    news_keywords: splitList(Array.isArray(item.news_keywords) ? item.news_keywords.join('，') : item.news_keywords),
    news_exclude_keywords: splitList(Array.isArray(item.news_exclude_keywords) ? item.news_exclude_keywords.join('，') : item.news_exclude_keywords)
  }}));
}}

function renderTable(sync=true) {{
  if (sync) syncRowsFromDom();
  const q = document.getElementById('filter').value.trim().toLowerCase();
  const body = document.getElementById('rows');
  body.innerHTML = '';
  let visible = 0;
  holdings.forEach((item, index) => {{
    const hay = JSON.stringify(item).toLowerCase();
    if (q && !hay.includes(q)) return;
    visible += 1;
    const tr = document.createElement('tr');
    tr.dataset.index = index;
    tr.innerHTML = `
      <td class="enabled"><input data-field="enabled" type="checkbox" ${{item.enabled !== false ? 'checked' : ''}}></td>
      <td class="symbol"><input data-field="symbol" value="${{escapeHtml(item.symbol || '')}}"></td>
      <td class="name"><input data-field="name" value="${{escapeHtml(item.name || '')}}"></td>
      <td class="full"><textarea data-field="full_name">${{escapeHtml(item.full_name || '')}}</textarea></td>
      <td><textarea data-field="aliases">${{escapeHtml(joinList(item.aliases))}}</textarea></td>
      <td><textarea data-field="business_summary">${{escapeHtml(item.business_summary || '')}}</textarea></td>
      <td><textarea data-field="news_keywords">${{escapeHtml(joinList(item.news_keywords))}}</textarea></td>
      <td><textarea data-field="news_exclude_keywords">${{escapeHtml(joinList(item.news_exclude_keywords))}}</textarea></td>
      <td class="actions"><button class="danger" onclick="removeRow(${{index}})">删除</button></td>
    `;
    tr.addEventListener('input', () => {{
      holdings[index] = readRow(tr, holdings[index] || {{}});
    }});
    tr.addEventListener('change', () => {{
      holdings[index] = readRow(tr, holdings[index] || {{}});
    }});
    body.appendChild(tr);
  }});
  document.getElementById('summary').textContent = `共 ${{holdings.length}} 只，显示 ${{visible}} 只`;
}}

async function reloadData() {{
  try {{
    const data = await api('/api/holdings');
    holdings = data.holdings || [];
    loadedHoldings = true;
    renderTable(false);
    showStatus('已加载持仓。');
  }} catch (err) {{
    showStatus(err.message, 'err');
  }}
}}

function addRow() {{
  syncRowsFromDom();
  holdings.push({{enabled: true, symbol: '', name: '', aliases: [], news_keywords: [], news_exclude_keywords: []}});
  renderTable(false);
}}

function removeRow(index) {{
  if (!confirm('确认删除这只持仓？')) return;
  syncRowsFromDom();
  holdings.splice(index, 1);
  renderTable(false);
}}

function openBatch() {{ document.getElementById('batchModal').style.display = 'flex'; }}
function closeBatch() {{ document.getElementById('batchModal').style.display = 'none'; }}
function closeDiff() {{ document.getElementById('diffModal').style.display = 'none'; }}

function parseBatchLine(line) {{
  const parts = line.split(/[，,\\t]+/).map(s => s.trim()).filter(Boolean);
  if (!parts.length) return null;
  const codeLike = value => /^(\\d{{6}}(\\.(SH|SZ|BJ))?|HK\\d{{1,5}}|0?\\d{{4,5}}\\.HK)$/i.test(value);
  if (parts.length === 1) {{
    const only = parts[0];
    if (codeLike(only)) return {{symbol: only, name: only, enabled: true}};
    return {{symbol: '', name: only, enabled: true}};
  }}
  const [a, b] = parts;
  if (codeLike(a)) return {{symbol: a, name: b, enabled: true}};
  return {{symbol: b, name: a, enabled: true}};
}}

function applyBatch() {{
  syncRowsFromDom();
  const lines = document.getElementById('batchText').value.split(/\\n+/);
  const parsed = lines.map(parseBatchLine).filter(Boolean);
  holdings.push(...parsed);
  document.getElementById('batchText').value = '';
  closeBatch();
  renderTable(false);
}}

async function previewSave() {{
  try {{
    pendingPayload = currentRows();
    const data = await api('/api/preview', {{method: 'POST', body: JSON.stringify({{holdings: pendingPayload}})}});
    const warnings = (data.warnings || []).map(item => `! ${{item.message || item}}`).join('\\n');
    document.getElementById('diffText').textContent = [warnings ? `校验提醒：\\n${{warnings}}` : '', data.diff_text || '没有变化。'].filter(Boolean).join('\\n\\n');
    document.getElementById('diffModal').style.display = 'flex';
  }} catch (err) {{
    showStatus(err.message, 'err');
  }}
}}

async function confirmSave() {{
  try {{
    const data = await api('/api/save', {{method: 'POST', body: JSON.stringify({{holdings: pendingPayload || currentRows()}})}});
    closeDiff();
    showStatus(`保存成功。\\n备份：${{data.backup_path || '无'}}\\n已同步 SQLite：${{data.imported_count}} 只持仓。`);
    holdings = data.holdings || holdings;
    renderTable();
  }} catch (err) {{
    showStatus(err.message, 'err');
  }}
}}

document.getElementById('eventDate').value = todayString();
showView('overview');
</script>
</body>
</html>"""


def diff_text(diff: dict[str, Any]) -> str:
    lines: list[str] = []
    added = diff.get("added") or []
    removed = diff.get("removed") or []
    changed = diff.get("changed") or []
    if added:
        lines.append("新增：")
        for item in added:
            lines.append(f"+ {item.get('symbol', '')} {item.get('name', '')}")
    if removed:
        lines.append("删除：")
        for item in removed:
            lines.append(f"- {item.get('symbol', '')} {item.get('name', '')}")
    if changed:
        lines.append("修改：")
        for item in changed:
            before = item.get("before", {})
            after = item.get("after", {})
            lines.append(f"* {after.get('symbol') or before.get('symbol')} {before.get('name', '')} -> {after.get('name', '')}")
    return "\n".join(lines) or "没有变化。"


class HoldingsHandler(BaseHTTPRequestHandler):
    server_version = "SurveilHoldingsWeb/0.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"{self.address_string()} - {fmt % args}", flush=True)

    @property
    def token(self) -> str:
        return str(getattr(self.server, "token", ""))

    @property
    def restart_sina_flash(self) -> bool:
        return bool(getattr(self.server, "restart_sina_flash", False))

    def authorized(self) -> bool:
        if not self.token:
            return True
        supplied = self.headers.get("X-Holdings-Token", "")
        if supplied == self.token:
            return True
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        return (qs.get("token") or [""])[0] == self.token

    def send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, content: str) -> None:
        body = content.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or "0")
        raw = self.rfile.read(length).decode("utf-8")
        return json.loads(raw or "{}")

    def require_auth(self) -> bool:
        if self.authorized():
            return True
        self.send_json({"ok": False, "error": "未授权，请输入 HOLDINGS_WEB_TOKEN"}, HTTPStatus.UNAUTHORIZED)
        return False

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self.send_html(html_page(bool(self.token)))
            return
        if parsed.path == "/api/holdings":
            if not self.require_auth():
                return
            try:
                self.send_json({"ok": True, "holdings": normalized_holdings()})
            except Exception as exc:  # noqa: BLE001
                self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        if parsed.path == "/api/overview":
            if not self.require_auth():
                return
            try:
                qs = parse_qs(parsed.query)
                self.send_json(overview_payload((qs.get("date") or [""])[0]))
            except Exception as exc:  # noqa: BLE001
                self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        if parsed.path == "/api/events":
            if not self.require_auth():
                return
            try:
                qs = parse_qs(parsed.query)
                limit_raw = (qs.get("limit") or ["100"])[0]
                try:
                    limit = int(limit_raw)
                except ValueError:
                    limit = 100
                events = fetch_events_rows(
                    day=(qs.get("date") or [""])[0],
                    source=(qs.get("source") or [""])[0],
                    kind=(qs.get("kind") or [""])[0],
                    q=(qs.get("q") or [""])[0],
                    limit=limit,
                )
                self.send_json({"ok": True, "events": events})
            except Exception as exc:  # noqa: BLE001
                self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        if parsed.path == "/api/health":
            if not self.require_auth():
                return
            try:
                self.send_json(health_payload())
            except Exception as exc:  # noqa: BLE001
                self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        if parsed.path == "/api/media-keywords":
            if not self.require_auth():
                return
            try:
                payload = media_keyword_payload()
                payload["ok"] = True
                self.send_json(payload)
            except Exception as exc:  # noqa: BLE001
                self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        if parsed.path == "/api/settings":
            if not self.require_auth():
                return
            try:
                payload = settings_payload()
                payload["ok"] = True
                self.send_json(payload)
            except Exception as exc:  # noqa: BLE001
                self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        if not self.require_auth():
            return
        parsed = urlparse(self.path)
        try:
            payload = self.read_json()
            if parsed.path == "/api/media-keywords":
                base_keywords = payload.get("base_keywords")
                include_keywords = payload.get("include_keywords")
                exclude_keywords = payload.get("exclude_keywords")
                if not isinstance(include_keywords, list) or not isinstance(exclude_keywords, list):
                    raise HoldingsError("请求缺少 include_keywords / exclude_keywords 数组")
                if base_keywords is not None and not isinstance(base_keywords, list):
                    raise HoldingsError("base_keywords 必须是数组")
                saved = save_media_keyword_config(base_keywords, include_keywords, exclude_keywords)
                saved.update(
                    {
                        "code_default_keywords": media_keyword_payload()["code_default_keywords"],
                        "default_keywords": saved["base_keywords"] or media_keyword_payload()["code_default_keywords"],
                        "base_keywords_overridden": bool(saved["base_keywords"]),
                    }
                )
                saved["ok"] = True
                self.send_json(saved)
                return
            if parsed.path == "/api/settings":
                values = payload.get("values")
                if not isinstance(values, dict):
                    raise HoldingsError("请求缺少 values 对象")
                saved = save_settings(values)
                saved["ok"] = True
                self.send_json(saved)
                return
            items = payload.get("holdings")
            if not isinstance(items, list):
                raise HoldingsError("请求缺少 holdings 数组")
            current = normalized_holdings()
            normalized = normalize_holdings_for_save(items, current)
            if parsed.path == "/api/preview":
                warnings = validate_holdings(normalized, verify_remote=True)
                diff = holdings_diff(current, normalized)
                self.send_json(
                    {
                        "ok": True,
                        "diff": diff,
                        "diff_text": diff_text(diff),
                        "holdings": normalized,
                        "warnings": warnings,
                    }
                )
                return
            if parsed.path == "/api/save":
                result = save_holdings(normalized, db_path=DEFAULT_DB_PATH)
                if self.restart_sina_flash:
                    subprocess.run(["systemctl", "restart", "surveil-sina-flash.service"], check=False)
                self.send_json(
                    {
                        "ok": True,
                        "backup_path": str(result.backup_path) if result.backup_path else "",
                        "imported_count": result.imported_count,
                        "changed_count": result.changed_count,
                        "holdings": normalized_holdings(),
                    }
                )
                return
        except Exception as exc:  # noqa: BLE001
            self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        self.send_error(HTTPStatus.NOT_FOUND)


def main() -> int:
    parser = argparse.ArgumentParser(description="Surveil 持仓管理 Web UI")
    parser.add_argument("--host", default=os.getenv("HOLDINGS_WEB_HOST", DEFAULT_HOST))
    parser.add_argument("--port", type=int, default=int(os.getenv("HOLDINGS_WEB_PORT", str(DEFAULT_PORT))))
    args = parser.parse_args()

    load_env(ROOT / ".env")
    host = args.host
    port = args.port
    server = ThreadingHTTPServer((host, port), HoldingsHandler)
    server.token = os.getenv("HOLDINGS_WEB_TOKEN", "").strip()
    server.restart_sina_flash = env_flag("HOLDINGS_WEB_RESTART_SINA_FLASH", False)
    print(f"Surveil holdings web listening on http://{host}:{port}", flush=True)
    if not server.token:
        print("WARNING: HOLDINGS_WEB_TOKEN 未配置。请仅通过 SSH 隧道访问。", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
