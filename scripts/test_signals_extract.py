#!/usr/bin/env python3
"""Regression tests for signal extraction and outcome metric helpers."""

from __future__ import annotations

import json
import sqlite3
import tempfile
from datetime import date
from pathlib import Path

from article_gate import ensure_article_reviews_table
from market_db import init_db
from official_news_gate import ensure_official_news_table
from signal_outcome_update import compute_metrics, quote_rows_from_response, target_rows
from signal_review import classify_review, review_signals
from stock_relations import (
    accept_relation_suggestion,
    create_relation_suggestion,
    diff_relations,
    export_relations,
    import_relations,
    save_relation,
)
from signals_extract import extract_signals, target_from_text, x_targets


NOW = "2026-06-29T02:00:00+00:00"


def dumps(value) -> str:  # noqa: ANN001 - tiny test helper
    return json.dumps(value, ensure_ascii=False)


def seed_db(path: Path) -> None:
    conn = init_db(path)
    ensure_article_reviews_table(conn)
    ensure_official_news_table(conn)
    conn.execute(
        """
        INSERT INTO portfolio_holdings (symbol, name, full_name, aliases_json, enabled, raw_json, updated_at)
        VALUES (?, ?, ?, ?, 1, '{}', ?)
        """,
        ("000725.SZ", "京东方A", "京东方科技集团股份有限公司", dumps(["京东方"]), NOW),
    )
    conn.execute(
        """
        INSERT INTO events (
            source, source_event_id, event_type, title, summary, full_text, url,
            published_at, first_seen_at, symbols_json, themes_json, raw_json,
            content_hash, baseline_only
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
        """,
        (
            "ifind_notice",
            "notice-1",
            "announcement",
            "京东方A 先进封装试验线通线",
            "京东方A 公告先进封装试验线进展。",
            "板级玻璃基封装载板试验线实现设备通线。",
            "https://example.com/notice",
            NOW,
            NOW,
            dumps(["000725.SZ"]),
            dumps(["先进封装"]),
            "{}",
            "hash-notice-1",
        ),
    )
    event_id = conn.execute("SELECT id FROM events WHERE source_event_id='notice-1'").fetchone()[0]
    analysis = {
        "importance": "high",
        "core_content": "京东方A先进封装产线进展。",
        "incremental_view": {"classification": "增量利好"},
        "price_impact": {"direction": "上涨", "duration": "数日"},
        "related_holdings": [
            {
                "name": "京东方A",
                "code": "000725.SZ",
                "relation": "直接相关",
                "impact_direction": "positive",
                "reason": "产线进度直接相关。",
            }
        ],
        "tracking_points": ["后续验证产线良率和客户认证。"],
        "risks": ["量产进度低于预期。"],
    }
    conn.execute(
        """
        INSERT INTO event_analyses (
            event_id, task, model, importance, classification, direction,
            impact_duration, should_push, analysis_json, created_at
        ) VALUES (?, 'portfolio_event', 'fake-model', 'high', '增量利好', '上涨', '数日', 1, ?, ?)
        """,
        (event_id, dumps(analysis), NOW),
    )
    conn.execute(
        """
        INSERT INTO deliveries (event_id, channel, status, sent_at, error, payload_json)
        VALUES (?, 'feishu', 'sent', ?, '', '{}')
        """,
        (event_id, NOW),
    )

    conn.execute(
        """
        INSERT INTO events (
            source, source_event_id, event_type, title, summary, full_text, url,
            published_at, first_seen_at, symbols_json, themes_json, raw_json,
            content_hash, baseline_only
        ) VALUES ('jygs', 'jygs-1', 'action', '韭研异动样本', '', '', '', ?, ?, '[]', '[]', '{}', 'hash-jygs', 0)
        """,
        (NOW, NOW),
    )
    jygs_event_id = conn.execute("SELECT id FROM events WHERE source_event_id='jygs-1'").fetchone()[0]
    conn.execute(
        """
        INSERT INTO event_analyses (
            event_id, task, model, importance, classification, direction,
            impact_duration, should_push, analysis_json, created_at
        ) VALUES (?, 'portfolio_event', 'fake-model', 'high', '增量利好', '上涨', '数日', 1, ?, ?)
        """,
        (jygs_event_id, dumps({"importance": "high"}), NOW),
    )

    article_review = {
        "importance": "high",
        "push_now": True,
        "affected_targets": ["京东方A（000725.SZ）", "玻璃基板封装"],
        "market_impact": "利好先进封装链。",
        "model": "fake-gate",
    }
    conn.execute(
        """
        INSERT INTO article_reviews (
            source, item_id, url, title, source_module, published_at,
            importance, push_now, market_impact, incremental_classification,
            affected_targets_json, reason, daily_summary, confidence, gate_json,
            pushed_at, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "digitimes_tw_semiconductors",
            "article-1",
            "https://example.com/article",
            "玻璃基板封装需求升温",
            "DIGITIMES Taiwan / 半导体与零组件",
            NOW,
            "high",
            1,
            "利好先进封装链。",
            "增量利好",
            dumps(article_review["affected_targets"]),
            "出现硬变量。",
            "玻璃基板封装需求升温。",
            "高",
            dumps(article_review),
            NOW,
            NOW,
        ),
    )

    official_analysis = {
        "importance": "high",
        "core_content": "NVIDIA 发布新平台，带动上游供应链。",
        "incremental_view": {"classification": "增量利好"},
        "a_share": {
            "positive": [
                {
                    "name": "京东方A",
                    "code": "000725.SZ",
                    "reason": "先进封装载板映射。",
                    "duration": "数周到数月",
                    "confidence": "中",
                }
            ],
            "negative": [],
        },
        "global_equity": {"positive": [{"name": "NVDA", "code": "NVDA"}], "negative": []},
    }
    conn.execute(
        """
        INSERT INTO official_news_reviews (
            source, item_id, url, title, published_at, importance, should_push_now,
            reason, daily_summary, analysis_json, pushed_at, created_at
        ) VALUES (?, ?, ?, ?, ?, 'high', 1, ?, ?, ?, ?, ?)
        """,
        (
            "nvidia_blog",
            "official-1",
            "https://example.com/nvidia",
            "NVIDIA 发布 AI 基础设施平台",
            NOW,
            "产业链传导明确。",
            "NVIDIA 平台更新。",
            dumps(official_analysis),
            NOW,
            NOW,
        ),
    )

    conn.execute(
        """
        CREATE TABLE seen_posts (
            source TEXT NOT NULL,
            post_id TEXT NOT NULL,
            url TEXT NOT NULL,
            text TEXT NOT NULL,
            published_at TEXT,
            first_seen_at TEXT NOT NULL,
            delivery_status TEXT NOT NULL DEFAULT 'sent',
            delivered_at TEXT,
            delivery_error TEXT,
            delivery_attempts INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (source, post_id)
        )
        """
    )
    conn.execute(
        """
        INSERT INTO seen_posts (
            source, post_id, url, text, published_at, first_seen_at,
            delivery_status, delivered_at
        ) VALUES (?, ?, ?, ?, ?, ?, 'sent', ?)
        """,
        (
            "x:serenity",
            "x-1",
            "https://x.com/serenity/status/x-1",
            "$NVDA 光互联趋势继续发酵，也会映射到京东方A。",
            NOW,
            NOW,
            NOW,
        ),
    )
    conn.commit()
    conn.close()


def test_extract_signals_from_existing_sources() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "surveil.sqlite3"
        seed_db(path)
        counts = extract_signals(db_path=path, days=10, dry_run=False)
        assert counts["events"] == 1
        assert counts["article_reviews"] == 1
        assert counts["official_news_reviews"] == 1
        assert counts["seen_posts"] == 1
        assert counts["signals"] == 4
        conn = init_db(path)
        signals = conn.execute("SELECT source_table, source, title FROM signals ORDER BY id").fetchall()
        assert len(signals) == 4
        assert all(row[1] != "jygs" for row in signals)
        targets = conn.execute(
            "SELECT symbol, name, target_role FROM signal_targets ORDER BY symbol, target_role"
        ).fetchall()
        assert ("000725.SZ", "京东方A", "holding") in targets
        assert ("NVDA", "NVDA", "global_mapping") in targets
        extract_signals(db_path=path, days=10, dry_run=False)
        assert conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0] == 4
        conn.close()


def test_outcome_metrics_from_ifind_like_response() -> None:
    response = {
        "tables": [
            {
                "thscode": "000725.SZ",
                "time": ["2026-06-22", "2026-06-23", "2026-06-24", "2026-06-25"],
                "table": {
                    "close": [10, 11, 9, 12],
                    "amount": [100, 200, 150, 300],
                },
            }
        ]
    }
    quotes = quote_rows_from_response(response)
    assert [item["date"] for item in quotes] == [
        date(2026, 6, 22),
        date(2026, 6, 23),
        date(2026, 6, 24),
        date(2026, 6, 25),
    ]
    metrics, outcome_json, status = compute_metrics(quotes, "positive")
    assert metrics["return_1d"] == 10.0
    assert metrics["return_3d"] == 20.0
    assert metrics["max_drawdown"] == -10.0
    assert metrics["max_runup"] == 20.0
    assert metrics["volume_change"] == 200.0
    assert metrics["matched_direction"] == "matched"
    assert status == "partial_3d"
    assert outcome_json["quote_count"] == 4


def test_bare_foreign_numeric_codes_do_not_become_a_share_symbols() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "surveil.sqlite3"
        seed_db(path)
        conn = init_db(path)
        assert target_from_text(conn, "京东方A（000725.SZ）")["symbol"] == "000725.SZ"
        assert target_from_text(conn, "京东方A")["symbol"] == "000725.SZ"
        foreign = target_from_text(conn, "Samsung Electronics 005930 / SK hynix 000660")
        assert foreign is not None
        assert foreign.get("symbol") in (None, "")
        assert all(target.get("symbol") != "005930.SZ" for target in x_targets(conn, "005930 and 000660"))
        conn.close()


def test_outcome_target_rows_only_select_a_share_symbols() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "surveil.sqlite3"
        seed_db(path)
        extract_signals(db_path=path, days=10, dry_run=False)
        conn = init_db(path)
        conn.row_factory = sqlite3.Row
        rows = target_rows(conn, days=10, limit=None)
        assert rows
        assert all(str(row["symbol"]).endswith((".SZ", ".SH", ".BJ")) for row in rows)
        conn.close()


def test_relation_import_expands_signal_targets() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "surveil.sqlite3"
        config_path = Path(tmpdir) / "relations.json"
        seed_db(path)
        config_path.write_text(
            dumps(
                {
                    "relations": [
                        {
                            "symbol": "000725.SZ",
                            "symbol_name": "京东方A",
                            "related_symbol": "300567.SZ",
                            "related_name": "精测电子",
                            "relation_type": "设备供应链",
                            "impact_direction": "positive",
                            "reason": "面板/封装产线资本开支映射到检测设备。",
                            "confidence": "中",
                            "enabled": True,
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        counts = import_relations(db_path=path, config_path=config_path)
        assert counts["imported"] == 1
        extract_signals(db_path=path, days=10, dry_run=False)
        conn = init_db(path)
        targets = conn.execute(
            "SELECT symbol, name, target_role, relation_type FROM signal_targets ORDER BY symbol"
        ).fetchall()
        assert ("300567.SZ", "精测电子", "related_stock", "设备供应链") in targets
        conn.close()


def test_relation_json_roundtrip_and_suggestion_accept() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "surveil.sqlite3"
        config_path = Path(tmpdir) / "relations.json"
        init_db(path).close()
        relation = save_relation(
            {
                "symbol": "HBM",
                "symbol_name": "HBM",
                "related_symbol": "300308.SZ",
                "related_name": "中际旭创",
                "relation_type": "AI memory read-through",
                "impact_direction": "positive",
                "theme": "AI 算力",
                "reason": "HBM 供给紧张强化 AI 服务器链景气度。",
                "confidence": "中",
                "relation_strength": "3",
                "source": "test",
                "enabled": True,
            },
            db_path=path,
        )
        assert relation["id"] > 0
        exported = export_relations(db_path=path, config_path=config_path)
        assert exported["count"] == 1
        diff = diff_relations(db_path=path, config_path=config_path)
        assert diff["only_in_db"] == []
        assert diff["only_in_json"] == []
        suggestion = create_relation_suggestion(
            {
                "symbol": "人造钻石散热",
                "related_symbol": "300179.SZ",
                "related_name": "四方达",
                "relation_type": "thermal material theme",
                "impact_direction": "positive",
                "reason": "芯片散热材料主题映射。",
                "confidence": "中",
                "source": "test-suggestion",
            },
            db_path=path,
        )
        accepted = accept_relation_suggestion(suggestion_id=suggestion["id"], db_path=path)
        assert accepted["symbol"] == "人造钻石散热"
        assert accepted["related_symbol"] == "300179.SZ"


def test_theme_context_expands_signal_targets() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "surveil.sqlite3"
        init_db(path).close()
        save_relation(
            {
                "symbol": "人造钻石散热",
                "symbol_name": "人造钻石散热",
                "related_symbol": "300179.SZ",
                "related_name": "四方达",
                "relation_type": "thermal material theme",
                "impact_direction": "positive",
                "theme": "芯片散热",
                "reason": "金刚石散热主题映射到相关材料股。",
                "confidence": "中",
                "enabled": True,
            },
            db_path=path,
        )
        with sqlite3.connect(path) as conn:
            ensure_article_reviews_table(conn)
            conn.execute(
                """
                INSERT INTO article_reviews (
                    source, item_id, url, title, source_module, published_at,
                    importance, push_now, market_impact, incremental_classification,
                    affected_targets_json, reason, daily_summary, confidence,
                    gate_json, pushed_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'high', 1, ?, '增量利好', '[]', ?, ?, '中', '{}', ?, ?)
                """,
                (
                    "rss",
                    "diamond-cooling-1",
                    "https://example.com/diamond",
                    "人造钻石给芯片降温：量产元年已至",
                    "test",
                    NOW,
                    "人造钻石散热进入量产阶段，可能影响芯片散热材料链。",
                    "标题命中人造钻石散热主题。",
                    "人造钻石散热主题强化。",
                    NOW,
                    NOW,
                ),
            )
            conn.commit()
        extract_signals(db_path=path, days=10, dry_run=False)
        with sqlite3.connect(path) as conn:
            row = conn.execute(
                "SELECT symbol, name, target_role, relation_type FROM signal_targets WHERE symbol = '300179.SZ'"
            ).fetchone()
        assert row == ("300179.SZ", "四方达", "related_stock", "thermal material theme")


def test_signal_review_classification_and_insert() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "surveil.sqlite3"
        seed_db(path)
        extract_signals(db_path=path, days=10, dry_run=False)
        conn = init_db(path)
        signal_id, target_id = conn.execute(
            """
            SELECT s.id, t.id
            FROM signals s JOIN signal_targets t ON t.signal_id = s.id
            WHERE t.symbol = '000725.SZ'
            ORDER BY s.id LIMIT 1
            """
        ).fetchone()
        conn.execute(
            """
            INSERT INTO signal_outcomes (
                signal_id, target_id, symbol, as_of_date, return_1d, return_3d,
                return_5d, max_drawdown, max_runup, volume_change,
                matched_direction, outcome_status, outcome_json, updated_at
            ) VALUES (?, ?, '000725.SZ', '2026-06-30', 2.0, 4.5, 5.0, -1.0, 5.5, 80.0,
                      'matched', 'partial_5d', '{}', ?)
            """,
            (signal_id, target_id, NOW),
        )
        conn.commit()
        conn.close()
        conn = init_db(path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT s.id AS signal_id, s.direction AS signal_direction,
                   t.id AS target_id, t.symbol, t.name, t.expected_direction,
                   o.as_of_date, o.return_1d, o.return_3d, o.return_5d, o.return_10d,
                   o.return_20d, o.max_drawdown, o.max_runup, o.volume_change,
                   o.outcome_status
            FROM signal_outcomes o
            JOIN signals s ON s.id = o.signal_id
            JOIN signal_targets t ON t.id = o.target_id
            WHERE o.signal_id = ? AND o.symbol = '000725.SZ'
            """,
            (signal_id,),
        ).fetchone()
        review = classify_review(row)
        assert review["verdict"] == "hit"
        conn.close()
        counts = review_signals(db_path=path, days=10, dry_run=False)
        assert counts["reviewed"] >= 1
        conn = init_db(path)
        stored = conn.execute(
            "SELECT symbol, verdict, error_type FROM signal_reviews WHERE symbol='000725.SZ'"
        ).fetchone()
        assert stored == ("000725.SZ", "hit", "none")
        score = conn.execute("SELECT signal_count, hit_rate FROM source_scores WHERE window_days=30").fetchone()
        assert score is not None
        conn.close()


def main() -> int:
    test_extract_signals_from_existing_sources()
    test_outcome_metrics_from_ifind_like_response()
    test_bare_foreign_numeric_codes_do_not_become_a_share_symbols()
    test_outcome_target_rows_only_select_a_share_symbols()
    test_relation_import_expands_signal_targets()
    test_relation_json_roundtrip_and_suggestion_accept()
    test_theme_context_expands_signal_targets()
    test_signal_review_classification_and_insert()
    print("signal extraction/outcome tests OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
