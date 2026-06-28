#!/usr/bin/env python3
"""Lightweight repository secret/personal-data scan for CI and pre-publish checks."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


EXCLUDED_DIRS = {
    ".git",
    ".venv",
    "__pycache__",
    ".pytest_cache",
    "data",
    "logs",
    "reports",
}
EXCLUDED_FILES = {
    ".env",
    ".DS_Store",
    ".gitignore",
    "scan_secrets.py",
}
EXCLUDED_GLOBS = (
    "shadowsocks_*.yaml",
    "*clash*.yaml",
    "*mihomo*.yaml",
)
PRIVATE_PATHS = {
    Path("docs/monitoring-plan.md"),
}
BINARY_SUFFIXES = {
    ".db",
    ".gif",
    ".gz",
    ".jpeg",
    ".jpg",
    ".pdf",
    ".png",
    ".sqlite",
    ".sqlite3",
    ".zip",
}

PATTERNS = [
    (
        "public_ipv4_literal",
        re.compile(
            r"\b(?!(?:0|10|127|169\.254|172\.(?:1[6-9]|2\d|3[01])|192\.168|224|255)\.)"
            r"(?:\d{1,3}\.){3}\d{1,3}\b"
        ),
    ),
    (
        "personal_ssh_key_path",
        re.compile(
            r"(?:^|[\s\"'`])~?/?\.ssh/(?!deploy_key\b|id_ed25519\b|id_rsa\b)"
            r"[A-Za-z0-9_.-]*(?:key|id|rsa|ed25519)[A-Za-z0-9_.-]*",
            re.I,
        ),
    ),
    ("local_user_path", re.compile(r"/Users/[A-Za-z0-9_.-]+")),
    ("proxy_config_name", re.compile(r"shadowsocks_[^/\s]+\.ya?ml", re.I)),
    ("bearer_literal", re.compile(r"Bearer\s+[A-Za-z0-9._-]{20,}")),
    ("openai_style_key", re.compile(r"\bsk-[A-Za-z0-9][A-Za-z0-9_-]{18,}\b")),
    (
        "assigned_secret",
        re.compile(
            r"^\s*(?:export\s+)?[A-Z0-9_]*(?:API_KEY|ACCESS_TOKEN|REFRESH_TOKEN|BEARER_TOKEN|COOKIE|SECRET|WEBHOOK)[A-Z0-9_]*\s*=\s*"
            r"(?!$|[\"']?\$|[\"']?<|[\"']?your[_-]?\w*|[\"']?example\b|[\"']?xxx\b|[\"']?redacted\b|[\"']?hidden\b|[\"']?placeholder\b|[\"']?如果|[\"']?你的|[\"']?已|[\"']?保留)"
            r"[^#\s][^\n]*"
        ),
    ),
]


def should_skip(path: Path, root: Path) -> bool:
    rel = path.relative_to(root)
    if rel in PRIVATE_PATHS:
        return True
    if any(part in EXCLUDED_DIRS for part in rel.parts):
        return True
    if path.name in EXCLUDED_FILES:
        return True
    if any(path.match(pattern) for pattern in EXCLUDED_GLOBS):
        return True
    if path.name.startswith(".env.") and path.name != ".env.example":
        return True
    if path.suffix.lower() in BINARY_SUFFIXES:
        return True
    return False


def scan_file(path: Path, root: Path) -> list[str]:
    text = path.read_text(encoding="utf-8", errors="replace")
    checks = [item for item in PATTERNS if path.name != ".env.example" or item[0] != "assigned_secret"]

    findings: list[str] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        if any(marker in line for marker in ("User-Agent", "Mozilla/", "AppleWebKit/", "Chrome/", "Safari/", "Edg/")):
            continue
        for label, pattern in checks:
            if pattern.search(line):
                rel = path.relative_to(root)
                findings.append(f"{rel}:{lineno}: {label}: {line[:220]}")
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description="Scan repository files for likely secrets or personal deployment data.")
    parser.add_argument("paths", nargs="*", default=["."], help="Files or directories to scan.")
    args = parser.parse_args()

    root = Path.cwd().resolve()
    findings: list[str] = []
    for raw in args.paths:
        start = Path(raw).resolve()
        if not start.exists():
            continue
        if start.is_file():
            if not should_skip(start, root):
                findings.extend(scan_file(start, root))
            continue
        for path in start.rglob("*"):
            resolved = path.resolve()
            if resolved.is_file() and not should_skip(resolved, root):
                findings.extend(scan_file(resolved, root))

    if findings:
        print("Potential secret or personal deployment data found:", file=sys.stderr)
        for item in findings:
            print(item, file=sys.stderr)
        return 1
    print("secret scan passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
