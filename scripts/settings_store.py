"""Safe Web settings access for the Surveil runtime .env file."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / ".env"


@dataclass(frozen=True)
class SettingField:
    key: str
    label: str
    group: str
    sensitive: bool = False
    help: str = ""
    placeholder: str = ""


SETTING_GROUPS: list[dict[str, Any]] = [
    {
        "id": "llm",
        "title": "大模型",
        "restart_hint": "保存后建议重启 X/RSS/TrendForce/海外媒体等常驻分析服务，使新模型配置立即生效。",
        "fields": [
            SettingField("LLM_PROVIDER", "供应商类型", "llm", placeholder="openai_compatible"),
            SettingField("LLM_BASE_URL", "Base URL", "llm", placeholder="https://api.deepseek.com"),
            SettingField("LLM_MODEL", "模型名称", "llm", placeholder="deepseek-chat"),
            SettingField("LLM_API_KEY", "API Key", "llm", sensitive=True, help="留空表示保留现有密钥。"),
            SettingField("LLM_TIMEOUT_SECONDS", "超时秒数", "llm", placeholder="90"),
            SettingField("LLM_RETRY_COUNT", "重试次数", "llm", placeholder="2"),
            SettingField("LLM_THINKING_TYPE", "默认 thinking", "llm", placeholder="disabled"),
            SettingField("LLM_GATE_THINKING_TYPE", "门控 thinking", "llm", placeholder="enabled"),
        ],
    },
    {
        "id": "ifind",
        "title": "iFinD",
        "restart_hint": "iFinD 定时任务下一次运行会读取新配置；如要立即验证，可手动运行 iFinD smoke test。",
        "fields": [
            SettingField("IFIND_API_BASE_URL", "API Base URL", "ifind", placeholder="https://quantapi.51ifind.com/api/v1"),
            SettingField("IFIND_REFRESH_TOKEN", "Refresh Token", "ifind", sensitive=True, help="账号详情页 Refresh Token，不是 API 密钥。"),
            SettingField("IFIND_ACCESS_TOKEN", "Access Token", "ifind", sensitive=True, help="可选；通常只需要 Refresh Token。"),
            SettingField("IFIND_TIMEOUT_SECONDS", "超时秒数", "ifind", placeholder="20"),
        ],
    },
    {
        "id": "x",
        "title": "X / Serenity",
        "restart_hint": "保存后建议重启 surveil-x-stream.service，使 X stream 立即使用新 token。",
        "fields": [
            SettingField("X_USERNAME", "监控账号", "x", placeholder="example_user"),
            SettingField("X_BEARER_TOKEN", "Bearer Token", "x", sensitive=True, help="服务器优先使用 Bearer Token。"),
            SettingField("X_CLIENT_ID", "Client ID", "x"),
            SettingField("X_CLIENT_SECRET", "Client Secret", "x", sensitive=True),
            SettingField("X_REDIRECT_URI", "Redirect URI", "x", placeholder="http://127.0.0.1:8765/callback"),
            SettingField("X_LINK_ENRICHMENT_ENABLED", "外链解析", "x", placeholder="1"),
        ],
    },
    {
        "id": "feishu",
        "title": "飞书",
        "restart_hint": "飞书配置通常下一条推送即可生效；常驻服务如已缓存环境变量，重启后立即生效。",
        "fields": [
            SettingField("FEISHU_WEBHOOK", "机器人 Webhook", "feishu", sensitive=True),
            SettingField("FEISHU_SECRET", "签名 Secret", "feishu", sensitive=True, help="机器人未开启签名校验时可留空。"),
            SettingField("FEISHU_APP_ID", "App ID", "feishu"),
            SettingField("FEISHU_APP_SECRET", "App Secret", "feishu", sensitive=True),
            SettingField("FEISHU_RETRY_COUNT", "重试次数", "feishu", placeholder="2"),
        ],
    },
    {
        "id": "network",
        "title": "网络 / RSS 抓取",
        "restart_hint": "保存后建议重启 RSS/海外媒体/TrendForce 等抓取服务，使代理、超时、并发和健康告警配置立即生效。",
        "fields": [
            SettingField("SURVEIL_HTTP_PROXY", "HTTP 代理", "network", placeholder="http://127.0.0.1:7890"),
            SettingField("SURVEIL_USER_AGENT", "User-Agent", "network", placeholder="Mozilla/5.0 ..."),
            SettingField("SURVEIL_HTTP_TIMEOUT_SECONDS", "默认超时秒数", "network", placeholder="20"),
            SettingField("SURVEIL_HTTP_RETRY_COUNT", "默认重试次数", "network", placeholder="2"),
            SettingField("SURVEIL_HTTP_RETRY_BACKOFF_SECONDS", "重试退避秒数", "network", placeholder="2"),
            SettingField("RSS_FETCH_MAX_WORKERS", "RSS 并发数", "network", placeholder="8"),
            SettingField("RSS_FETCH_TIMEOUT_SECONDS", "RSS 超时秒数", "network", placeholder="15"),
            SettingField("RSS_FETCH_RETRY_COUNT", "RSS 重试次数", "network", placeholder="1"),
            SettingField("SOURCE_HEALTH_ALERT_FAILURES", "连续失败告警阈值", "network", placeholder="3"),
            SettingField("SOURCE_HEALTH_ALERT_COOLDOWN_MINUTES", "告警冷却分钟", "network", placeholder="60"),
            SettingField("SOURCE_HEALTH_ALERT_RECOVERY", "恢复告警", "network", placeholder="1"),
        ],
    },
    {
        "id": "sina",
        "title": "新浪智研 / 新浪新闻",
        "restart_hint": "保存后建议重启新浪快讯常驻服务；个股资讯 timer 下一次运行会读取新配置。",
        "fields": [
            SettingField("SINA_NEWS_PROVIDER", "新闻源", "sina", placeholder="legacy"),
            SettingField("SINA_ZY_API_BASE_URL", "智研 API Base URL", "sina"),
            SettingField("SINA_ZY_API_KEY", "智研 API Key", "sina", sensitive=True),
            SettingField("SINA_ZY_TIMEOUT_SECONDS", "智研超时秒数", "sina", placeholder="20"),
            SettingField("SINA_FLASH_POLL_SECONDS", "快讯轮询秒数", "sina", placeholder="10"),
            SettingField("SINA_FLASH_TAGS", "快讯 tags", "sina", placeholder="10"),
        ],
    },
    {
        "id": "jygs",
        "title": "韭研公社",
        "restart_hint": "韭研公社 timer 下一次运行会读取新 Cookie；Cookie 过期时在这里覆盖写入即可。",
        "fields": [
            SettingField("JYGS_COOKIE", "完整 Cookie", "jygs", sensitive=True),
            SettingField("JYGS_SESSION", "SESSION", "jygs", sensitive=True, help="只填 SESSION 时脚本会拼成 Cookie。"),
            SettingField("JYGS_RUN_TIMES", "运行时间", "jygs", placeholder="12:30,16:00"),
            SettingField("JYGS_PAGE_LIMIT", "单页数量", "jygs", placeholder="30"),
            SettingField("JYGS_MAX_FETCH_ITEMS", "最大抓取条数", "jygs", placeholder="300"),
        ],
    },
    {
        "id": "web",
        "title": "Web 工作台",
        "restart_hint": "HOLDINGS_WEB_TOKEN 变更后需要重启 surveil-holdings-web.service 才会用于鉴权。",
        "fields": [
            SettingField("HOLDINGS_WEB_TOKEN", "访问 Token", "web", sensitive=True, help="留空表示保留现有 token。"),
        ],
    },
]

FIELDS_BY_KEY = {field.key: field for group in SETTING_GROUPS for field in group["fields"]}

MIRROR_KEYS = {
    "LLM_API_KEY": ("OPENAI_API_KEY",),
    "LLM_BASE_URL": ("OPENAI_BASE_URL",),
    "LLM_MODEL": ("OPENAI_MODEL",),
}


def parse_env_file(path: Path = ENV_PATH) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def mask_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}...{value[-4:]}"


def settings_payload(path: Path = ENV_PATH) -> dict[str, Any]:
    values = parse_env_file(path)
    groups = []
    for group in SETTING_GROUPS:
        fields = []
        for field in group["fields"]:
            value = values.get(field.key, "")
            item = {
                "key": field.key,
                "label": field.label,
                "sensitive": field.sensitive,
                "configured": bool(value),
                "masked": mask_secret(value) if field.sensitive else "",
                "value": "" if field.sensitive else value,
                "help": field.help,
                "placeholder": field.placeholder,
            }
            fields.append(item)
        groups.append(
            {
                "id": group["id"],
                "title": group["title"],
                "restart_hint": group["restart_hint"],
                "fields": fields,
            }
        )
    return {"groups": groups, "path": str(path)}


def build_updates(raw_values: dict[str, Any], current: dict[str, str]) -> tuple[dict[str, str], list[dict[str, str]]]:
    updates: dict[str, str] = {}
    changes: list[dict[str, str]] = []
    for key, raw_value in raw_values.items():
        if key not in FIELDS_BY_KEY:
            raise ValueError(f"不允许修改未知配置项：{key}")
        field = FIELDS_BY_KEY[key]
        value = str(raw_value or "").strip()
        old_value = current.get(key, "")
        if field.sensitive and not value:
            continue
        if value == old_value:
            continue
        updates[key] = value
        changes.append(
            {
                "key": key,
                "sensitive": "1" if field.sensitive else "0",
                "old": "<redacted>" if field.sensitive and old_value else old_value,
                "new": "<redacted>" if field.sensitive and value else value,
            }
        )
        for mirror_key in MIRROR_KEYS.get(key, ()):
            updates[mirror_key] = value
    return updates, changes


def write_env_updates(updates: dict[str, str], path: Path = ENV_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        key = stripped.split("=", 1)[0].strip() if "=" in stripped and not stripped.startswith("#") else ""
        if key in updates:
            out.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            out.append(line)
    if updates and out and out[-1].strip():
        out.append("")
    for key, value in updates.items():
        if key not in seen:
            out.append(f"{key}={value}")
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("\n".join(out).rstrip() + "\n")
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
        os.chmod(path, 0o600)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def save_settings(raw_values: dict[str, Any], path: Path = ENV_PATH) -> dict[str, Any]:
    current = parse_env_file(path)
    updates, changes = build_updates(raw_values, current)
    if updates:
        write_env_updates(updates, path)
    return {"changed": changes, "changed_count": len(changes), "path": str(path)}
