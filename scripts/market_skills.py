#!/usr/bin/env python3
"""Import and match reusable market reasoning skills."""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from db_utils import connect_sqlite
from market_db import DEFAULT_DB_PATH, init_db
from signal_store import json_dumps, json_loads, normalize_direction


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PRIVATE_SKILL_DIR = ROOT / "config" / "market_skill"
LOCAL_WORKSPACE_SKILL_DIR = ROOT.parent / "misce" / "market_skill"


FIELD_RE_TEMPLATE = r"(?m)^{key}:\s*(.*)$"
CHINESE_RE = re.compile(r"[\u4e00-\u9fff]{2,}")
ASCII_RE = re.compile(r"[A-Za-z][A-Za-z0-9.+_-]{1,}")
SPLIT_RE = re.compile(r"[\s,，;；、/|｜:：()（）\[\]【】<>《》\"'“”‘’]+")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1", (table,)).fetchone()
    return bool(row)


def default_skill_dir() -> Path:
    env_path = os.getenv("MARKET_SKILL_DIR", "").strip()
    if env_path:
        return Path(env_path).expanduser()
    if DEFAULT_PRIVATE_SKILL_DIR.exists():
        return DEFAULT_PRIVATE_SKILL_DIR
    return LOCAL_WORKSPACE_SKILL_DIR


def split_frontmatter(text: str) -> tuple[str, str]:
    if not text.startswith("---"):
        return "", text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return "", text
    return parts[1].strip("\n"), parts[2].strip("\n")


def line_field(frontmatter: str, key: str) -> str:
    match = re.search(FIELD_RE_TEMPLATE.format(key=re.escape(key)), frontmatter)
    if not match:
        return ""
    return match.group(1).strip().strip('"').strip("'")


def block_field(frontmatter: str, key: str) -> str:
    match = re.search(rf"(?m)^{re.escape(key)}:\s*\|\s*\n((?:[ \t]+.*(?:\n|$))+)", frontmatter)
    if not match:
        return line_field(frontmatter, key)
    lines = []
    for line in match.group(1).splitlines():
        lines.append(re.sub(r"^[ \t]{2,}", "", line).rstrip())
    return "\n".join(lines).strip()


def list_field(frontmatter: str, key: str) -> list[str]:
    inline = line_field(frontmatter, key)
    if inline.startswith("[") and inline.endswith("]"):
        return [item.strip().strip('"').strip("'") for item in inline.strip("[]").split(",") if item.strip()]
    match = re.search(rf"(?m)^{re.escape(key)}:\s*\n((?:[ \t]+-[^\n]*(?:\n|$))+)", frontmatter)
    if not match:
        return []
    return [re.sub(r"^[ \t]+-\s*", "", line).strip() for line in match.group(1).splitlines() if line.strip()]


def relevance_block(frontmatter: str) -> str:
    start = re.search(r"(?m)^relevance_maps:\s*$", frontmatter)
    if not start:
        return ""
    tail = frontmatter[start.end() :]
    end = re.search(r"(?m)^(key_insight|constraints|hard_evidence|staleness|side_topics):", tail)
    return tail[: end.start()] if end else tail


def map_field(body: str, key: str) -> str:
    block = re.search(rf"(?m)^    {re.escape(key)}:\s*\|\s*\n((?:[ \t]{{6,}}.*(?:\n|$))+)", body)
    if block:
        lines = [re.sub(r"^[ \t]{6,}", "", line).rstrip() for line in block.group(1).splitlines()]
        return "\n".join(lines).strip()
    match = re.search(rf"(?m)^    {re.escape(key)}:\s*(.*)$", body)
    return match.group(1).strip() if match else ""


def extract_relevance_maps(frontmatter: str) -> list[dict[str, Any]]:
    block = relevance_block(frontmatter)
    if not block:
        return []
    parts = re.split(r"(?m)^  - trigger:\s*", block)
    maps: list[dict[str, Any]] = []
    for part in parts[1:]:
        lines = part.splitlines()
        if not lines:
            continue
        trigger = lines[0].strip()
        body = "\n".join(lines[1:])
        item = {
            "trigger": trigger,
            "chain": map_field(body, "chain"),
            "affected": map_field(body, "affected"),
            "strength": map_field(body, "strength"),
            "nature": map_field(body, "nature"),
            "verified_outcome": map_field(body, "verified_outcome"),
        }
        if item["trigger"] and (item["chain"] or item["affected"]):
            maps.append(item)
    return maps


def compact_text(value: Any, limit: int = 1200) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit]


def tokenize(value: str) -> list[str]:
    text = str(value or "")
    tokens: set[str] = set()
    for token in ASCII_RE.findall(text):
        tokens.add(token.lower())
    for token in CHINESE_RE.findall(text):
        if len(token) <= 12:
            tokens.add(token)
        for part in SPLIT_RE.split(token):
            if len(part) >= 2:
                tokens.add(part)
    for part in SPLIT_RE.split(text):
        part = part.strip()
        if len(part) >= 2 and len(part) <= 24:
            tokens.add(part.lower() if part.isascii() else part)
    return sorted(tokens)


def match_terms(*parts: Any) -> list[str]:
    terms: set[str] = set()
    for part in parts:
        if isinstance(part, list):
            terms.update(str(item).strip() for item in part if str(item).strip())
        else:
            terms.update(tokenize(str(part or "")))
    return sorted(term for term in terms if len(term) >= 2)


def parse_view_file(path: Path, *, skill_name: str = "market_skill") -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8")
    frontmatter, body = split_frontmatter(text)
    date = line_field(frontmatter, "date")
    source = line_field(frontmatter, "source")
    topic = line_field(frontmatter, "topic")
    themes = list_field(frontmatter, "themes")
    kind = line_field(frontmatter, "type") or "relevance_map"
    key_insight = block_field(frontmatter, "key_insight")
    constraints = list_field(frontmatter, "constraints")
    hard_evidence = list_field(frontmatter, "hard_evidence")
    staleness = line_field(frontmatter, "staleness")
    maps = extract_relevance_maps(frontmatter)
    records: list[dict[str, Any]] = []
    for index, item in enumerate(maps, start=1):
        trigger = compact_text(item.get("trigger"))
        chain = compact_text(item.get("chain"))
        affected = compact_text(item.get("affected"))
        terms = match_terms(topic, themes, trigger, chain, affected, item.get("nature"))
        records.append(
            {
                "skill_id": f"{skill_name}:{path.stem}:{index}",
                "skill_name": skill_name,
                "source_name": source,
                "source_path": str(path),
                "kind": kind,
                "date": date,
                "topic": topic,
                "themes": themes,
                "trigger_text": trigger,
                "chain_text": chain,
                "affected_text": affected,
                "strength": compact_text(item.get("strength"), 200),
                "nature": compact_text(item.get("nature"), 200),
                "key_insight": compact_text(key_insight, 1600),
                "constraints": constraints,
                "hard_evidence": hard_evidence,
                "staleness": compact_text(staleness, 400),
                "verified_outcome": compact_text(item.get("verified_outcome"), 1200),
                "match_terms": terms,
                "raw": {
                    "frontmatter": frontmatter,
                    "body_excerpt": body[:2000],
                    "map": item,
                },
            }
        )
    return records


def discover_view_files(skill_dir: Path) -> list[Path]:
    views_dir = skill_dir / "views"
    if not views_dir.exists():
        return []
    return sorted(path for path in views_dir.glob("*.md") if path.is_file())


def import_market_skills(*, db_path: Path, skill_dir: Path) -> dict[str, int]:
    init_db(db_path).close()
    skill_dir = skill_dir.expanduser()
    if not skill_dir.exists():
        raise FileNotFoundError(f"market skill dir not found: {skill_dir}")
    counts = {"files": 0, "records": 0, "imported": 0, "skipped": 0}
    records: list[dict[str, Any]] = []
    for path in discover_view_files(skill_dir):
        counts["files"] += 1
        parsed = parse_view_file(path)
        if not parsed:
            counts["skipped"] += 1
        records.extend(parsed)
    now = utc_now()
    with connect_sqlite(db_path) as conn:
        for item in records:
            counts["records"] += 1
            conn.execute(
                """
                INSERT INTO market_skills (
                    skill_id, skill_name, source_name, source_path, kind, date, topic,
                    themes_json, trigger_text, chain_text, affected_text, strength,
                    nature, key_insight, constraints_json, hard_evidence_json,
                    staleness, verified_outcome, match_text, raw_json, enabled, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                ON CONFLICT(skill_id) DO UPDATE SET
                    skill_name = excluded.skill_name,
                    source_name = excluded.source_name,
                    source_path = excluded.source_path,
                    kind = excluded.kind,
                    date = excluded.date,
                    topic = excluded.topic,
                    themes_json = excluded.themes_json,
                    trigger_text = excluded.trigger_text,
                    chain_text = excluded.chain_text,
                    affected_text = excluded.affected_text,
                    strength = excluded.strength,
                    nature = excluded.nature,
                    key_insight = excluded.key_insight,
                    constraints_json = excluded.constraints_json,
                    hard_evidence_json = excluded.hard_evidence_json,
                    staleness = excluded.staleness,
                    verified_outcome = excluded.verified_outcome,
                    match_text = excluded.match_text,
                    raw_json = excluded.raw_json,
                    updated_at = excluded.updated_at
                """,
                (
                    item["skill_id"],
                    item["skill_name"],
                    item["source_name"],
                    item["source_path"],
                    item["kind"],
                    item["date"],
                    item["topic"],
                    json_dumps(item["themes"]),
                    item["trigger_text"],
                    item["chain_text"],
                    item["affected_text"],
                    item["strength"],
                    item["nature"],
                    item["key_insight"],
                    json_dumps(item["constraints"]),
                    json_dumps(item["hard_evidence"]),
                    item["staleness"],
                    item["verified_outcome"],
                    "\n".join(item["match_terms"]),
                    json_dumps(item["raw"]),
                    now,
                ),
            )
            counts["imported"] += 1
        conn.commit()
    return counts


def skill_row_item(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "skill_id": row["skill_id"] or "",
        "skill_name": row["skill_name"] or "",
        "source_name": row["source_name"] or "",
        "source_path": row["source_path"] or "",
        "kind": row["kind"] or "",
        "date": row["date"] or "",
        "topic": row["topic"] or "",
        "themes": json_loads(row["themes_json"], []),
        "trigger_text": row["trigger_text"] or "",
        "chain_text": row["chain_text"] or "",
        "affected_text": row["affected_text"] or "",
        "strength": row["strength"] or "",
        "nature": row["nature"] or "",
        "key_insight": row["key_insight"] or "",
        "constraints": json_loads(row["constraints_json"], []),
        "hard_evidence": json_loads(row["hard_evidence_json"], []),
        "staleness": row["staleness"] or "",
        "verified_outcome": row["verified_outcome"] or "",
        "match_terms": [term for term in str(row["match_text"] or "").splitlines() if term],
        "enabled": bool(row["enabled"]),
        "updated_at": row["updated_at"] or "",
    }


def match_score(item: dict[str, Any], context_text: str) -> tuple[int, list[str]]:
    context = str(context_text or "").lower()
    if not context:
        return 0, []
    matched: list[str] = []
    score = 0
    weighted_terms = []
    weighted_terms.extend((term, 3) for term in tokenize(item.get("trigger_text", "")))
    weighted_terms.extend((term, 2) for term in item.get("themes", []))
    weighted_terms.extend((term, 2) for term in tokenize(item.get("topic", "")))
    weighted_terms.extend((term, 1) for term in tokenize(item.get("affected_text", "")))
    weighted_terms.extend((term, 1) for term in tokenize(item.get("chain_text", "")))
    seen: set[str] = set()
    for term, weight in weighted_terms:
        probe = str(term).strip()
        if not probe or probe in seen:
            continue
        seen.add(probe)
        needle = probe.lower() if probe.isascii() else probe
        if len(needle) >= 2 and needle in context:
            score += weight
            matched.append(probe)
    return score, matched[:10]


def match_market_skills(
    conn: sqlite3.Connection,
    context_text: str,
    *,
    max_matches: int = 3,
    min_score: int = 5,
) -> list[dict[str, Any]]:
    if not table_exists(conn, "market_skills"):
        return []
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT *
        FROM market_skills
        WHERE enabled = 1
        ORDER BY date DESC, updated_at DESC, id DESC
        LIMIT 1000
        """
    ).fetchall()
    scored: list[tuple[int, dict[str, Any]]] = []
    for row in rows:
        item = skill_row_item(row)
        score, matched_terms = match_score(item, context_text)
        if score < min_score:
            continue
        item["match_score"] = score
        item["matched_terms"] = matched_terms
        scored.append((score, item))
    scored.sort(key=lambda pair: (pair[0], pair[1].get("date", "")), reverse=True)
    return [item for _, item in scored[: max(1, max_matches)]]


def confidence_from_strength(strength: str) -> str:
    text = str(strength or "").lower()
    if "强" in text or "strong" in text:
        return "中高"
    if "中" in text or "medium" in text:
        return "中"
    if "弱" in text or "weak" in text:
        return "低"
    return "中"


def targets_from_matches(matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    targets: list[dict[str, Any]] = []
    for item in matches:
        affected = str(item.get("affected_text") or item.get("topic") or "").strip()
        if not affected:
            continue
        reason_parts = [
            f"Market Skill 命中：{item.get('trigger_text', '')}",
            f"传导链：{item.get('chain_text', '')}",
        ]
        constraints = item.get("constraints") or []
        if constraints:
            reason_parts.append("约束：" + "；".join(str(value) for value in constraints[:3]))
        targets.append(
            {
                "name": affected,
                "market": "行业环节",
                "target_role": "skill_inferred",
                "expected_direction": normalize_direction("uncertain"),
                "relation_type": "market_skill",
                "relation_reason": "；".join(part for part in reason_parts if part),
                "confidence": confidence_from_strength(str(item.get("strength") or "")),
                "theme": "；".join(str(value) for value in item.get("themes", [])),
                "source": item.get("skill_name") or "market_skill",
                "source_skill_id": item.get("skill_id") or "",
                "match_score": item.get("match_score", 0),
            }
        )
    return targets


def evidence_from_matches(matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    for item in matches:
        text = "\n".join(
            part
            for part in [
                f"Market Skill: {item.get('topic', '')}",
                f"trigger: {item.get('trigger_text', '')}",
                f"chain: {item.get('chain_text', '')}",
                f"affected: {item.get('affected_text', '')}",
                f"staleness: {item.get('staleness', '')}",
                f"matched_terms: {', '.join(item.get('matched_terms', []))}",
            ]
            if part.strip()
        )
        evidence.append(
            {
                "evidence_type": "market_skill",
                "text": text[:2000],
                "url": "",
                "source": item.get("skill_name") or "market_skill",
                "observed_at": item.get("date") or "",
            }
        )
    return evidence


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import market_skill relevance maps into SQLite.")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="SQLite DB path.")
    parser.add_argument("--skill-dir", default=str(default_skill_dir()), help="market_skill directory path.")
    parser.add_argument("--match", default="", help="Optional text to test skill matching after import.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    db_path = Path(args.db)
    counts = import_market_skills(db_path=db_path, skill_dir=Path(args.skill_dir))
    print(json.dumps(counts, ensure_ascii=False, sort_keys=True), flush=True)
    if args.match:
        with connect_sqlite(db_path) as conn:
            conn.row_factory = sqlite3.Row
            matches = match_market_skills(conn, args.match, max_matches=5)
        print(json.dumps(matches, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
