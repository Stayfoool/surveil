"""Download and extract text from iFinD announcement PDFs."""

from __future__ import annotations

import hashlib
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from env_utils import get_env


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_NOTICE_PDF_DIR = ROOT / "data" / "ifind_notices"


class NoticePdfError(RuntimeError):
    """Raised when a notice PDF cannot be downloaded or parsed."""


def env_bool(name: str, default: bool = True) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    if raw in {"0", "false", "no", "n", "off", "禁用"}:
        return False
    if raw in {"1", "true", "yes", "y", "on", "启用"}:
        return True
    return default


def env_int(name: str, default: int, *, minimum: int = 0) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return max(minimum, int(raw))
    except ValueError:
        return default


def pdf_dir() -> Path:
    raw = get_env("IFIND_NOTICE_PDF_DIR", default="")
    return Path(raw).expanduser() if raw else DEFAULT_NOTICE_PDF_DIR


def notice_pdf_url(row: dict[str, Any]) -> str:
    return str(row.get("pdfURL") or row.get("pdfUrl") or row.get("PDFURL") or "").strip()


def parse_notice_pdf(row: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Return extracted text and safe parse metadata.

    The signed iFinD PDF URL is intentionally not included in returned metadata.
    """
    metadata: dict[str, Any] = {
        "enabled": env_bool("IFIND_NOTICE_PDF_PARSE", True),
        "status": "skipped",
        "source": "ifind_pdf",
    }
    if not metadata["enabled"]:
        metadata["reason"] = "IFIND_NOTICE_PDF_PARSE disabled"
        return "", metadata

    url = notice_pdf_url(row)
    if not url:
        metadata["reason"] = "missing pdfURL"
        return "", metadata

    target_dir = pdf_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / safe_pdf_filename(row, url)
    max_bytes = env_int("IFIND_NOTICE_PDF_MAX_BYTES", 30 * 1024 * 1024, minimum=1024 * 1024)
    max_pages = env_int("IFIND_NOTICE_PDF_MAX_PAGES", 80, minimum=1)
    max_chars = env_int("IFIND_NOTICE_TEXT_MAX_CHARS", 20000, minimum=1000)
    min_chars = env_int("IFIND_NOTICE_TEXT_MIN_CHARS", 200, minimum=0)

    metadata.update(
        {
            "file_name": target.name,
            "max_bytes": max_bytes,
            "max_pages": max_pages,
            "max_chars": max_chars,
            "min_chars": min_chars,
        }
    )

    try:
        if not target.exists() or target.stat().st_size == 0:
            download_pdf(url, target, max_bytes=max_bytes)
        digest, size = file_sha256(target)
        text, extract_meta = extract_pdf_text(target, max_pages=max_pages, max_chars=max_chars)
    except Exception as exc:  # noqa: BLE001 - callers should continue ingesting metadata
        metadata.update({"status": "failed", "error": str(exc)[:500]})
        return "", metadata

    text = normalize_text(text)
    status = "ok" if len(text) >= min_chars else "low_text"
    metadata.update(
        {
            "status": status,
            "file_sha256": digest,
            "file_size": size,
            "extracted_chars": len(text),
            **extract_meta,
        }
    )
    if len(text) < min_chars:
        metadata["reason"] = "extracted text shorter than threshold"
    return text[:max_chars], metadata


def safe_pdf_filename(row: dict[str, Any], url: str) -> str:
    symbol = str(row.get("thscode") or row.get("THSCODE") or row.get("code") or "notice").strip()
    seq = str(row.get("seq") or row.get("SEQ") or row.get("id") or "").strip()
    title = str(row.get("reportTitle") or row.get("title") or row.get("annTitle") or "").strip()
    seed = "\n".join(part for part in [symbol, seq, title, url] if part)
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]
    prefix = re.sub(r"[^A-Za-z0-9_.-]+", "_", symbol.upper())[:32] or "notice"
    if seq:
        seq_part = re.sub(r"[^A-Za-z0-9_.-]+", "_", seq)[:32]
        return f"{prefix}_{seq_part}_{digest}.pdf"
    return f"{prefix}_{digest}.pdf"


def download_pdf(url: str, target: Path, *, max_bytes: int) -> None:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/pdf,application/octet-stream,*/*",
            "User-Agent": "surveil-ifind-notice-pdf/0.1",
        },
        method="GET",
    )
    temp = target.with_suffix(f".{int(time.time())}.tmp")
    total = 0
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            content_length = response.headers.get("Content-Length")
            if content_length:
                try:
                    if int(content_length) > max_bytes:
                        raise NoticePdfError(f"PDF too large: {content_length} bytes")
                except ValueError:
                    pass
            with temp.open("wb") as fh:
                while True:
                    chunk = response.read(1024 * 256)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        raise NoticePdfError(f"PDF exceeded max bytes: {total}")
                    fh.write(chunk)
    except urllib.error.HTTPError as exc:
        body = exc.read(300).decode("utf-8", errors="replace")
        raise NoticePdfError(f"PDF download HTTP {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise NoticePdfError(f"PDF download failed: {exc}") from exc
    except Exception:
        temp.unlink(missing_ok=True)
        raise

    if total < 4:
        temp.unlink(missing_ok=True)
        raise NoticePdfError("PDF download returned empty file")
    with temp.open("rb") as fh:
        header = fh.read(8)
    if not header.startswith(b"%PDF"):
        temp.unlink(missing_ok=True)
        raise NoticePdfError("downloaded file is not a PDF")
    temp.replace(target)


def extract_pdf_text(path: Path, *, max_pages: int, max_chars: int) -> tuple[str, dict[str, Any]]:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise NoticePdfError("missing dependency pypdf") from exc

    reader = PdfReader(str(path))
    if getattr(reader, "is_encrypted", False):
        try:
            reader.decrypt("")
        except Exception as exc:  # noqa: BLE001
            raise NoticePdfError(f"encrypted PDF cannot be decrypted: {exc}") from exc
    pages_total = len(reader.pages)
    pages_read = min(pages_total, max_pages)
    parts: list[str] = []
    for index in range(pages_read):
        try:
            page_text = reader.pages[index].extract_text() or ""
        except Exception as exc:  # noqa: BLE001
            page_text = f"\n[第 {index + 1} 页文本抽取失败：{exc}]\n"
        if page_text.strip():
            parts.append(page_text)
        if sum(len(part) for part in parts) >= max_chars:
            break
    return "\n\n".join(parts)[:max_chars], {"pages_total": pages_total, "pages_read": pages_read}


def normalize_text(text: str) -> str:
    cleaned = text.replace("\r", "\n").replace("\x00", "")
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def file_sha256(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(1024 * 256)
            if not chunk:
                break
            size += len(chunk)
            digest.update(chunk)
    return digest.hexdigest(), size
