"""Per-source fetch backoff helpers for noisy or rate-limited feeds."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def env_int(name: str, default: int, minimum: int = 0, maximum: int = 86400) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return min(maximum, max(minimum, int(raw)))
    except ValueError:
        return default


def cooldown_seconds_for(source: str, *, default: int = 0) -> int:
    normalized = source.upper().replace("-", "_").replace(".", "_")
    specific = env_int(f"SOURCE_BACKOFF_{normalized}_SECONDS", -1, minimum=-1)
    if specific >= 0:
        return specific
    if source == "semianalysis":
        return env_int("SOURCE_BACKOFF_SEMIANALYSIS_SECONDS", 1800)
    if source == "jin10_rsshub_important":
        return env_int("SOURCE_BACKOFF_JIN10_SECONDS", 600)
    return env_int("SOURCE_BACKOFF_DEFAULT_SECONDS", default)


def should_skip_by_backoff(state: dict, *, now: datetime | None = None) -> tuple[bool, str]:
    skip_until = parse_dt(str(state.get("skip_until") or ""))
    current = now or utc_now()
    if skip_until and skip_until > current:
        return True, skip_until.isoformat()
    return False, ""


def backoff_state_after_failure(source: str, state: dict | None = None, *, now: datetime | None = None) -> dict:
    next_state = dict(state or {})
    seconds = cooldown_seconds_for(source)
    if seconds <= 0:
        next_state.pop("skip_until", None)
        return next_state
    current = now or utc_now()
    next_state["skip_until"] = (current + timedelta(seconds=seconds)).isoformat()
    next_state["last_backoff_at"] = current.isoformat()
    next_state["backoff_seconds"] = seconds
    return next_state


def clear_backoff_state(state: dict | None = None) -> dict:
    next_state = dict(state or {})
    next_state.pop("skip_until", None)
    return next_state
