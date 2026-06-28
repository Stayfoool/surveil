"""Safe read/write helpers for portfolio holdings configuration."""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import unicodedata
import urllib.parse
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from market_db import DEFAULT_DB_PATH
from portfolio_import import import_holdings


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = ROOT / "config" / "portfolio.json"
DEFAULT_BACKUP_DIR = ROOT / "config" / "backups"
LOCK_PATH = ROOT / "config" / "portfolio.lock"


@dataclass(frozen=True)
class SaveResult:
    backup_path: Path | None
    imported_count: int
    changed_count: int


class HoldingsError(ValueError):
    """Raised for invalid holdings configuration."""


class HoldingsValidationError(HoldingsError):
    """Raised when holdings fail strict validation before saving."""


@contextmanager
def config_lock() -> Any:
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open("a+", encoding="utf-8") as handle:
        try:
            import fcntl

            fcntl.flock(handle, fcntl.LOCK_EX)
        except ImportError:
            pass
        try:
            yield
        finally:
            try:
                import fcntl

                fcntl.flock(handle, fcntl.LOCK_UN)
            except ImportError:
                pass


def infer_symbol(value: str) -> str:
    raw = normalize_text(value).upper()
    if not raw:
        return ""
    if re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", raw):
        return raw
    match = re.fullmatch(r"HK(\d{1,5})", raw)
    if match:
        return f"HK{match.group(1).zfill(4)}"
    match = re.fullmatch(r"0?(\d{4,5})\.HK", raw)
    if match:
        return f"HK{match.group(1).zfill(4)}"
    if re.fullmatch(r"\d{6}", raw):
        if raw.startswith(("6", "9")):
            return f"{raw}.SH"
        if raw.startswith(("0", "1", "2", "3")):
            return f"{raw}.SZ"
        if raw.startswith(("4", "8")):
            return f"{raw}.BJ"
    return raw


def is_a_share_symbol(symbol: str) -> bool:
    return bool(re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", symbol.strip().upper()))


def is_hk_symbol(symbol: str) -> bool:
    return bool(re.fullmatch(r"HK\d{4,5}", symbol.strip().upper()))


def sina_quote_symbol(symbol: str) -> str:
    raw = infer_symbol(symbol)
    if raw.endswith(".SH"):
        return f"sh{raw.split('.')[0]}"
    if raw.endswith(".SZ"):
        return f"sz{raw.split('.')[0]}"
    if raw.endswith(".BJ"):
        return f"bj{raw.split('.')[0]}"
    return ""


def standard_symbol_from_sina(raw: str) -> str:
    symbol = raw.strip().lower()
    match = re.fullmatch(r"(sh|sz|bj)(\d{6})", symbol)
    if not match:
        return ""
    market = match.group(1).upper()
    suffix = {"SH": "SH", "SZ": "SZ", "BJ": "BJ"}.get(market)
    return f"{match.group(2)}.{suffix}" if suffix else ""


def string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        parts = re.split(r"[,，;；\n]+", value)
    elif isinstance(value, list):
        parts = value
    else:
        parts = []
    result: list[str] = []
    for item in parts:
        text = str(item).strip()
        if text and text not in result:
            result.append(text)
    return result


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value or "")).strip()


def normalize_holding(item: dict[str, Any], *, existing: dict[str, Any] | None = None) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise HoldingsError("持仓条目必须是对象")
    merged = dict(existing or {})
    symbol = infer_symbol(str(item.get("symbol") or merged.get("symbol") or ""))
    name = str(item.get("name") or merged.get("name") or "").strip()
    if not symbol and not name:
        raise HoldingsError("每条持仓至少需要股票代码或名称")
    merged.update(
        {
            "symbol": symbol,
            "name": name or symbol,
            "enabled": bool(item.get("enabled", merged.get("enabled", True))),
        }
    )
    for key in ("full_name", "business_summary"):
        if key in item or key not in merged:
            merged[key] = str(item.get(key) or "").strip()
    for key in ("aliases", "news_keywords", "news_exclude_keywords"):
        if key in item or key not in merged:
            merged[key] = string_list(item.get(key))
    return {key: value for key, value in merged.items() if value not in ("", [], None)}


def fetch_a_share_suggestions(keyword: str, timeout: int = 5) -> list[dict[str, str]]:
    keyword = keyword.strip()
    if not keyword:
        return []
    encoded = urllib.parse.quote(keyword)
    url = f"https://suggest3.sinajs.cn/suggest/type=11&key={encoded}&name=suggestdata"
    request = urllib.request.Request(
        url,
        headers={
            "Referer": "https://finance.sina.com.cn",
            "User-Agent": "surveil-holdings-validate/0.1",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read()
    text = body.decode("gb18030", errors="replace")
    match = re.search(r'="([^"]*)"', text)
    if not match:
        return []
    results: list[dict[str, str]] = []
    for record in match.group(1).split(";"):
        fields = record.split(",")
        if len(fields) < 4:
            continue
        name = fields[0].strip()
        code = fields[2].strip()
        sina_symbol = fields[3].strip()
        symbol = standard_symbol_from_sina(sina_symbol)
        if name and symbol and code:
            results.append({"name": name, "symbol": symbol, "sina_symbol": sina_symbol})
    return results


def enrich_missing_symbols(items: list[dict[str, Any]], *, verify_remote: bool = True) -> list[dict[str, Any]]:
    if not verify_remote:
        return items
    enriched: list[dict[str, Any]] = []
    for index, item in enumerate(items, start=1):
        symbol = str(item.get("symbol") or "").strip()
        name = str(item.get("name") or "").strip()
        if symbol or not name:
            enriched.append(item)
            continue
        try:
            suggestions = fetch_a_share_suggestions(name)
        except Exception as exc:  # noqa: BLE001
            raise HoldingsValidationError(f"第 {index} 行“{name}”缺少股票代码，且无法联网查询候选：{exc}") from exc
        exact_matches = [candidate for candidate in suggestions if similar_name(name, candidate.get("name", ""))]
        if len(exact_matches) == 1:
            updated = dict(item)
            updated["symbol"] = exact_matches[0]["symbol"]
            updated["name"] = exact_matches[0]["name"]
            enriched.append(updated)
            continue
        if not suggestions:
            raise HoldingsValidationError(f"第 {index} 行“{name}”缺少股票代码，且未查到匹配的 A 股候选。")
        preview = "；".join(f"{candidate['name']} {candidate['symbol']}" for candidate in suggestions[:5])
        raise HoldingsValidationError(f"第 {index} 行“{name}”缺少股票代码，候选不唯一或不精确：{preview}。请填写准确代码。")
    return enriched


def fetch_a_share_name(symbol: str, timeout: int = 5) -> str:
    sina_symbol = sina_quote_symbol(symbol)
    if not sina_symbol:
        return ""
    url = f"https://hq.sinajs.cn/list={sina_symbol}"
    request = urllib.request.Request(
        url,
        headers={
            "Referer": "https://finance.sina.com.cn",
            "User-Agent": "surveil-holdings-validate/0.1",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read()
    text = body.decode("gb18030", errors="replace")
    match = re.search(r'="([^"]*)"', text)
    if not match:
        return ""
    fields = match.group(1).split(",")
    return fields[0].strip() if fields else ""


def fetch_hk_name(symbol: str, timeout: int = 5) -> str:
    raw = infer_symbol(symbol)
    match = re.fullmatch(r"HK(\d{4,5})", raw)
    if not match:
        return ""
    sina_symbol = f"hk{match.group(1).zfill(5)}"
    url = f"https://hq.sinajs.cn/list={sina_symbol}"
    request = urllib.request.Request(
        url,
        headers={
            "Referer": "https://finance.sina.com.cn",
            "User-Agent": "surveil-holdings-validate/0.1",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read()
    text = body.decode("gb18030", errors="replace")
    match_data = re.search(r'="([^"]*)"', text)
    if not match_data:
        return ""
    fields = match_data.group(1).split(",")
    if len(fields) >= 2 and fields[1].strip():
        return fields[1].strip()
    if fields and fields[0].strip():
        return fields[0].strip()
    return ""


def similar_name(expected: str, actual: str, aliases: list[str] | None = None) -> bool:
    expected_norm = normalize_text(expected).upper()
    actual_norm = normalize_text(actual).upper()
    alias_norms = {normalize_text(alias).upper() for alias in aliases or []}
    if not expected_norm or not actual_norm:
        return True
    if expected_norm == actual_norm or expected_norm in actual_norm or actual_norm in expected_norm:
        return True
    return actual_norm in alias_norms or expected_norm in alias_norms


def validate_holdings(items: list[dict[str, Any]], *, verify_remote: bool = True) -> list[dict[str, str]]:
    """Return warnings and raise for blocking validation errors."""
    warnings: list[dict[str, str]] = []
    errors: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(items, start=1):
        symbol = str(item.get("symbol") or "").strip().upper()
        name = str(item.get("name") or "").strip()
        if not symbol:
            warnings.append(
                {
                    "level": "warning",
                    "field": "symbol",
                    "message": f"第 {index} 行 {name or '<未命名>'} 缺少股票代码；部分 iFinD/Sina 监控无法准确覆盖。",
                }
            )
            continue
        if symbol in seen:
            errors.append(f"重复股票代码：{symbol}")
        seen.add(symbol)
        if not re.fullmatch(r"(\d{6}\.(SH|SZ|BJ)|HK\d{4,5}|[A-Z.]{1,10})", symbol):
            errors.append(f"第 {index} 行股票代码格式可疑：{symbol}")
            continue
        if is_hk_symbol(symbol):
            if verify_remote:
                try:
                    actual_name = fetch_hk_name(symbol)
                except Exception as exc:  # noqa: BLE001
                    warnings.append(
                        {
                            "level": "warning",
                            "field": "symbol",
                            "message": f"{symbol} 港股名称联网校验失败：{exc}",
                        }
                    )
                    continue
                if not actual_name:
                    warnings.append(
                        {
                            "level": "warning",
                            "field": "symbol",
                            "message": f"{symbol} 未查到港股行情名称，仅完成代码格式校验。",
                        }
                    )
                    continue
                aliases = string_list(item.get("aliases"))
                if name and not similar_name(name, actual_name, aliases):
                    errors.append(f"{symbol} 名称可能填错：当前为“{name}”，行情源显示为“{actual_name}”。")
                elif not name:
                    warnings.append(
                        {
                            "level": "warning",
                            "field": "name",
                            "message": f"{symbol} 未填写简称；行情源显示为“{actual_name}”。",
                        }
                    )
            continue
        if is_a_share_symbol(symbol) and verify_remote:
            try:
                actual_name = fetch_a_share_name(symbol)
            except Exception as exc:  # noqa: BLE001 - keep save possible with explicit warning
                warnings.append(
                    {
                        "level": "warning",
                        "field": "symbol",
                        "message": f"{symbol} 无法联网校验名称：{exc}",
                    }
                )
                continue
            if not actual_name:
                errors.append(f"{symbol} 未查到有效 A 股名称，请确认代码是否正确。")
                continue
            aliases = string_list(item.get("aliases"))
            if name and not similar_name(name, actual_name, aliases):
                errors.append(f"{symbol} 名称可能填错：当前为“{name}”，行情源显示为“{actual_name}”。")
            elif not name:
                warnings.append(
                    {
                        "level": "warning",
                        "field": "name",
                        "message": f"{symbol} 未填写简称；行情源显示为“{actual_name}”。",
                    }
                )
    if errors:
        raise HoldingsValidationError("\n".join(errors))
    return warnings


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> dict[str, Any]:
    if not path.exists():
        return {"holdings": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise HoldingsError(f"持仓配置 JSON 格式错误：{exc}") from exc
    if not isinstance(data, dict):
        raise HoldingsError("持仓配置根节点必须是对象")
    holdings = data.get("holdings", [])
    if not isinstance(holdings, list):
        raise HoldingsError("持仓配置缺少 holdings 数组")
    return data


def normalized_holdings(path: Path = DEFAULT_CONFIG_PATH) -> list[dict[str, Any]]:
    data = load_config(path)
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in data.get("holdings", []):
        item = normalize_holding(raw)
        symbol = str(item.get("symbol") or "")
        if symbol and symbol in seen:
            raise HoldingsError(f"重复股票代码：{symbol}")
        if symbol:
            seen.add(symbol)
        result.append(item)
    return result


def normalize_holdings_for_save(items: list[dict[str, Any]], current: list[dict[str, Any]]) -> list[dict[str, Any]]:
    existing_by_symbol = {str(item.get("symbol") or ""): item for item in current if item.get("symbol")}
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in enrich_missing_symbols(items):
        symbol_hint = infer_symbol(str(raw.get("symbol") or ""))
        existing = existing_by_symbol.get(symbol_hint) if symbol_hint else None
        item = normalize_holding(raw, existing=existing)
        symbol = str(item.get("symbol") or "")
        if symbol and symbol in seen:
            raise HoldingsError(f"重复股票代码：{symbol}")
        if symbol:
            seen.add(symbol)
        result.append(item)
    return result


def backup_config(path: Path = DEFAULT_CONFIG_PATH, backup_dir: Path = DEFAULT_BACKUP_DIR) -> Path | None:
    if not path.exists():
        return None
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = backup_dir / f"portfolio-{stamp}.json"
    shutil.copy2(path, backup_path)
    return backup_path


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        json.loads(tmp_path.read_text(encoding="utf-8"))
        tmp_path.replace(path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def save_holdings(
    items: list[dict[str, Any]],
    *,
    config_path: Path = DEFAULT_CONFIG_PATH,
    db_path: Path = DEFAULT_DB_PATH,
) -> SaveResult:
    with config_lock():
        current = normalized_holdings(config_path)
        normalized = normalize_holdings_for_save(items, current)
        validate_holdings(normalized, verify_remote=True)
        backup_path = backup_config(config_path)
        atomic_write_json(config_path, {"holdings": normalized})
        imported_count = import_holdings(config_path, db_path)
    return SaveResult(backup_path=backup_path, imported_count=imported_count, changed_count=len(normalized))


def holdings_diff(old: list[dict[str, Any]], new: list[dict[str, Any]]) -> dict[str, Any]:
    old_by_symbol = {str(item.get("symbol") or item.get("name")): item for item in old}
    new_by_symbol = {str(item.get("symbol") or item.get("name")): item for item in new}
    added = [new_by_symbol[key] for key in new_by_symbol.keys() - old_by_symbol.keys()]
    removed = [old_by_symbol[key] for key in old_by_symbol.keys() - new_by_symbol.keys()]
    changed: list[dict[str, Any]] = []
    for key in sorted(old_by_symbol.keys() & new_by_symbol.keys()):
        if old_by_symbol[key] != new_by_symbol[key]:
            changed.append({"before": old_by_symbol[key], "after": new_by_symbol[key]})
    return {"added": added, "removed": removed, "changed": changed}
