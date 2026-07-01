#!/usr/bin/env python3
"""Regression checks for RSS fetch parsing and state helpers."""

from __future__ import annotations

import sqlite3
import os
from dataclasses import dataclass

import rss_monitor
from pipeline_health import record_pipeline_failure, record_pipeline_success
from source_backoff import backoff_state_after_failure, clear_backoff_state, should_skip_by_backoff
from source_health import record_source_failure, record_source_success, should_alert_failure, should_alert_recovery


@dataclass
class FakeResponse:
    status_code: int
    url: str
    headers: dict[str, str]
    content: bytes


def test_feedparser_parses_rss_atom_and_rdf() -> None:
    rss_xml = b"""
    <rss version="2.0">
      <channel>
        <item>
          <title><![CDATA[RSS title]]></title>
          <link>https://example.com/rss</link>
          <guid>rss-1</guid>
          <description><![CDATA[<p>RSS summary</p>]]></description>
          <pubDate>Mon, 29 Jun 2026 06:00:00 GMT</pubDate>
          <category>HBM</category>
        </item>
      </channel>
    </rss>
    """
    atom_xml = b"""
    <feed xmlns="http://www.w3.org/2005/Atom">
      <entry>
        <title>Atom title</title>
        <id>atom-1</id>
        <link href="https://example.com/atom" rel="alternate" />
        <summary>Atom summary</summary>
        <updated>2026-06-29T06:01:00Z</updated>
        <category term="CPO" />
      </entry>
    </feed>
    """
    rdf_xml = b"""
    <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
             xmlns="http://purl.org/rss/1.0/"
             xmlns:dc="http://purl.org/dc/elements/1.1/">
      <item rdf:about="https://example.com/rdf">
        <title>RDF title</title>
        <link>https://example.com/rdf</link>
        <description>RDF summary</description>
        <dc:date>2026-06-29T06:02:00Z</dc:date>
      </item>
    </rdf:RDF>
    """
    rss_items = rss_monitor.parsed_feed_items(rss_monitor.feedparser.parse(rss_xml))
    atom_items = rss_monitor.parsed_feed_items(rss_monitor.feedparser.parse(atom_xml))
    rdf_items = rss_monitor.parsed_feed_items(rss_monitor.feedparser.parse(rdf_xml))
    assert rss_items[0]["id"] == "rss-1"
    assert rss_items[0]["categories"] == ["HBM"]
    assert atom_items[0]["url"] == "https://example.com/atom"
    assert atom_items[0]["categories"] == ["CPO"]
    assert rdf_items[0]["id"] == "https://example.com/rdf"


def test_feed_state_roundtrip() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE source_state (source TEXT PRIMARY KEY, state_json TEXT, updated_at TEXT NOT NULL)")
    rss_monitor.save_source_state(conn, "demo", {"etag": '"abc"', "modified": "Mon, 29 Jun 2026 06:00:00 GMT"})
    state = rss_monitor.load_source_state(conn, "demo")
    assert state["etag"] == '"abc"'
    assert state["modified"].startswith("Mon")


def test_fetch_feed_uses_conditionals_and_skips_304() -> None:
    calls: list[dict] = []
    original_http_get = rss_monitor.http_get

    def fake_http_get(url: str, *, headers: dict | None = None, timeout: float | None = None, retries: int | None = None):
        calls.append({"url": url, "headers": headers or {}, "timeout": timeout, "retries": retries})
        return FakeResponse(304, url, {}, b"")

    try:
        rss_monitor.http_get = fake_http_get
        items, state, not_modified = rss_monitor.fetch_feed(
            "demo",
            "https://example.com/feed.xml",
            {"etag": '"abc"', "modified": "Mon, 29 Jun 2026 06:00:00 GMT"},
        )
    finally:
        rss_monitor.http_get = original_http_get

    assert items == []
    assert not_modified is True
    assert calls[0]["headers"]["If-None-Match"] == '"abc"'
    assert calls[0]["headers"]["If-Modified-Since"].startswith("Mon")
    assert state["last_checked_at"]


def test_source_health_failure_and_recovery() -> None:
    conn = sqlite3.connect(":memory:")
    record_source_failure(conn, "test", "source_a", RuntimeError("boom"))
    record_source_failure(conn, "test", "source_a", RuntimeError("boom again"))
    row = conn.execute(
        "SELECT consecutive_failures, last_error FROM source_health WHERE monitor = ? AND source = ?",
        ("test", "source_a"),
    ).fetchone()
    assert row[0] == 2
    assert "boom again" in row[1]
    record_source_success(conn, "test", "source_a")
    row = conn.execute(
        "SELECT consecutive_failures, last_success_at FROM source_health WHERE monitor = ? AND source = ?",
        ("test", "source_a"),
    ).fetchone()
    assert row[0] == 0
    assert row[1]


def test_source_health_alert_threshold() -> None:
    original = os.environ.pop("SOURCE_HEALTH_ALERT_FAILURES", None)
    try:
        assert should_alert_failure(1, None) is False
        assert should_alert_failure(3, None) is True
    finally:
        if original is not None:
            os.environ["SOURCE_HEALTH_ALERT_FAILURES"] = original


def test_source_health_recovery_respects_alert_cooldown() -> None:
    original_threshold = os.environ.get("SOURCE_HEALTH_ALERT_FAILURES")
    original_cooldown = os.environ.get("SOURCE_HEALTH_ALERT_COOLDOWN_MINUTES")
    try:
        os.environ["SOURCE_HEALTH_ALERT_FAILURES"] = "3"
        os.environ["SOURCE_HEALTH_ALERT_COOLDOWN_MINUTES"] = "60"
        assert should_alert_recovery(3, None) is False
        assert should_alert_recovery(3, "2026-06-29T00:00:00+00:00") is True
    finally:
        if original_threshold is None:
            os.environ.pop("SOURCE_HEALTH_ALERT_FAILURES", None)
        else:
            os.environ["SOURCE_HEALTH_ALERT_FAILURES"] = original_threshold
        if original_cooldown is None:
            os.environ.pop("SOURCE_HEALTH_ALERT_COOLDOWN_MINUTES", None)
        else:
            os.environ["SOURCE_HEALTH_ALERT_COOLDOWN_MINUTES"] = original_cooldown


def test_source_backoff_state_roundtrip() -> None:
    original = os.environ.get("SOURCE_BACKOFF_JIN10_SECONDS")
    try:
        os.environ["SOURCE_BACKOFF_JIN10_SECONDS"] = "600"
        state = backoff_state_after_failure("jin10_rsshub_important", {}, now=rss_monitor.datetime.fromisoformat("2026-06-29T00:00:00+00:00"))
        skip, until = should_skip_by_backoff(state, now=rss_monitor.datetime.fromisoformat("2026-06-29T00:05:00+00:00"))
        assert skip is True
        assert until == "2026-06-29T00:10:00+00:00"
        cleared = clear_backoff_state(state)
        assert "skip_until" not in cleared
    finally:
        if original is None:
            os.environ.pop("SOURCE_BACKOFF_JIN10_SECONDS", None)
        else:
            os.environ["SOURCE_BACKOFF_JIN10_SECONDS"] = original


def test_pipeline_health_helpers() -> None:
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "surveil.sqlite3"
        record_pipeline_failure("signals_extract", RuntimeError("boom"), db_path=db_path)
        conn = sqlite3.connect(db_path)
        row = conn.execute(
            """
            SELECT consecutive_failures, last_error
            FROM source_health
            WHERE monitor = 'signal_pipeline' AND source = 'signals_extract'
            """
        ).fetchone()
        assert row[0] == 1
        assert "boom" in row[1]
        conn.close()
        record_pipeline_success("signals_extract", db_path=db_path)
        conn = sqlite3.connect(db_path)
        row = conn.execute(
            """
            SELECT consecutive_failures, last_success_at
            FROM source_health
            WHERE monitor = 'signal_pipeline' AND source = 'signals_extract'
            """
        ).fetchone()
        assert row[0] == 0
        assert row[1]
        conn.close()


def main() -> int:
    test_feedparser_parses_rss_atom_and_rdf()
    test_feed_state_roundtrip()
    test_fetch_feed_uses_conditionals_and_skips_304()
    test_source_health_failure_and_recovery()
    test_source_health_alert_threshold()
    test_source_health_recovery_respects_alert_cooldown()
    test_source_backoff_state_roundtrip()
    test_pipeline_health_helpers()
    print("rss monitor fetch/state checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
