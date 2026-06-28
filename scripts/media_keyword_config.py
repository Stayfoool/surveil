"""User configurable focus keywords for RSS and overseas media filters."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Iterable

from trendforce_sources import FOCUS_KEYWORDS


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "media_keywords.json"


def normalize_keywords(values: Iterable[object]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        keyword = str(value or "").strip()
        if not keyword:
            continue
        key = keyword.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(keyword)
    return result


def load_media_keyword_config(path: Path = CONFIG_PATH) -> dict[str, list[str]]:
    if not path.exists():
        return {"base_keywords": [], "include_keywords": [], "exclude_keywords": []}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise ValueError(f"媒体关键词配置读取失败：{exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError("媒体关键词配置必须是 JSON object")
    return {
        "base_keywords": normalize_keywords(raw.get("base_keywords") or []),
        "include_keywords": normalize_keywords(raw.get("include_keywords") or []),
        "exclude_keywords": normalize_keywords(raw.get("exclude_keywords") or []),
    }


def save_media_keyword_config(
    base_keywords: Iterable[object] | None,
    include_keywords: Iterable[object],
    exclude_keywords: Iterable[object],
    path: Path = CONFIG_PATH,
) -> dict[str, list[str]]:
    payload = {
        "base_keywords": normalize_keywords(base_keywords or []),
        "include_keywords": normalize_keywords(include_keywords),
        "exclude_keywords": normalize_keywords(exclude_keywords),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
    return payload


def media_keyword_payload() -> dict[str, object]:
    user_config = load_media_keyword_config()
    effective_base = user_config["base_keywords"] or list(FOCUS_KEYWORDS)
    return {
        "code_default_keywords": list(FOCUS_KEYWORDS),
        "base_keywords": effective_base,
        "base_keywords_overridden": bool(user_config["base_keywords"]),
        "default_keywords": effective_base,
        "include_keywords": user_config["include_keywords"],
        "exclude_keywords": user_config["exclude_keywords"],
        "path": str(CONFIG_PATH),
    }


def is_media_focus_item(*parts: str) -> bool:
    text = " ".join(part for part in parts if part).casefold()
    user_config = load_media_keyword_config()
    if any(keyword.casefold() in text for keyword in user_config["exclude_keywords"]):
        return False
    base_keywords = user_config["base_keywords"] or list(FOCUS_KEYWORDS)
    keywords = [*base_keywords, *user_config["include_keywords"]]
    return any(keyword.casefold() in text for keyword in keywords)
