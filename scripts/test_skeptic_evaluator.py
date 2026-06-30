#!/usr/bin/env python3
"""Regression tests for the skeptic evaluator."""

from __future__ import annotations

import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from article_gate import gate_lines
from market_db import init_db
from official_news_gate import review_exists as official_review_exists
from official_news_gate import save_review as save_official_review
from skeptic_evaluator import apply_skeptic_review


def iso_days_ago(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def seed_seen_item(conn: sqlite3.Connection, *, title: str, days_ago: int = 3) -> None:
    seen_at = iso_days_ago(days_ago)
    conn.execute(
        """
        INSERT INTO seen_items (source, item_id, url, title, summary, published_at, first_seen_at)
        VALUES ('old_source', 'old-1', 'https://example.com/old', ?, '', ?, ?)
        """,
        (title, seen_at, seen_at),
    )
    conn.commit()


def test_skeptic_downgrades_duplicate_article() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "surveil.sqlite3"
        conn = init_db(path)
        seed_seen_item(conn, title="美光业绩超预期 存储价格继续上涨", days_ago=3)
        review = {
            "importance": "high",
            "push_now": True,
            "market_impact": "利好存储链。",
            "incremental_classification": "增量利好",
            "affected_targets": ["存储"],
            "reason": "业绩超预期。",
            "daily_summary": "美光业绩超预期。",
            "confidence": "中",
        }
        item = {
            "id": "new-1",
            "url": "https://example.com/new",
            "title": "美光业绩超预期 存储价格继续上涨",
            "published_at": datetime.now(timezone.utc).isoformat(),
            "summary": "重复报道。",
            "full_text": "重复报道。",
        }
        updated = apply_skeptic_review(conn, source="new_source", item=item, review=review, push_key="push_now")
        assert updated["push_now"] is False
        assert updated["importance"] == "medium"
        assert updated["skeptic"]["skeptic_verdict"] == "downgrade"
        assert updated["skeptic"]["old_news_risk"] == "high"
        assert "Skeptic" in "\n".join(gate_lines(updated))
        conn.close()


def test_official_review_preserves_skeptic_metadata() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "surveil.sqlite3"
        conn = init_db(path)
        seed_seen_item(conn, title="NVIDIA 发布 Rubin 100% 液冷方案", days_ago=4)
        review = {
            "importance": "high",
            "should_push_now": True,
            "reason": "官网发布技术方案。",
            "daily_summary": "NVIDIA 发布 Rubin 液冷方案。",
            "analysis": {"core_content": "NVIDIA 发布 Rubin 100% 液冷方案。"},
        }
        item = {
            "id": "nvidia-rubin",
            "url": "https://example.com/rubin",
            "title": "NVIDIA 发布 Rubin 100% 液冷方案",
            "published_at": datetime.now(timezone.utc).isoformat(),
            "full_text": "官网发布 Rubin 液冷方案。",
        }
        updated = apply_skeptic_review(conn, source="nvidia_blog", item=item, review=review, push_key="should_push_now")
        save_official_review(conn, "nvidia_blog", item, updated)
        loaded = official_review_exists(conn, "nvidia_blog", "nvidia-rubin")
        assert loaded is not None
        assert loaded["should_push_now"] is False
        assert loaded["skeptic"]["skeptic_verdict"] == "downgrade"
        assert loaded["analysis"]["_skeptic"]["old_news_risk"] == "high"
        conn.close()


def main() -> int:
    test_skeptic_downgrades_duplicate_article()
    test_official_review_preserves_skeptic_metadata()
    print("skeptic evaluator tests OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
