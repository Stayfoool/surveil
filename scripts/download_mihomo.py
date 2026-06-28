#!/usr/bin/env python3
"""Download mihomo from the official GitHub release and verify its checksum."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import shutil
import stat
import urllib.request
from pathlib import Path


GITHUB_API = "https://api.github.com/repos/MetaCubeX/mihomo/releases"
def fetch_release(version: str) -> dict:
    url = f"{GITHUB_API}/latest" if not version else f"{GITHUB_API}/tags/{version}"
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "surveil-mihomo-installer/0.1", "Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def select_asset(release: dict) -> dict:
    for item in release.get("assets", []):
        name = str(item.get("name") or "")
        if name.startswith("mihomo-linux-amd64-compatible-") and name.endswith(".gz"):
            return item
    raise SystemExit("未找到 mihomo linux-amd64-compatible 官方 release 资产")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download(url: str, destination: Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "surveil-mihomo-installer/0.1"})
    with urllib.request.urlopen(request, timeout=120) as response, destination.open("wb") as out:
        shutil.copyfileobj(response, out)


def main() -> int:
    parser = argparse.ArgumentParser(description="Download checksum-verified mihomo binary.")
    parser.add_argument("--version", default="", help="Release tag, for example v1.19.27. Default: latest.")
    parser.add_argument("--output", required=True, help="Output binary path.")
    args = parser.parse_args()

    release = fetch_release(args.version)
    version = str(release.get("tag_name") or args.version).strip()
    asset = select_asset(release)
    asset_name = str(asset["name"])
    official_url = str(asset["browser_download_url"])
    official_digest = str(asset.get("digest") or "").strip()
    if not official_digest.startswith("sha256:"):
        raise SystemExit("官方 GitHub release 未提供 sha256 digest，停止安装")
    expected_sha256 = official_digest.split(":", 1)[1]

    tmp_gz = Path("/tmp") / asset_name
    output = Path(args.output)
    print(f"download from official GitHub release: {official_url}", flush=True)
    download(official_url, tmp_gz)
    actual = sha256_file(tmp_gz)
    if actual != expected_sha256:
        tmp_gz.unlink(missing_ok=True)
        raise SystemExit(f"sha256 mismatch: expected={expected_sha256} actual={actual}")
    print(f"sha256 verified: {actual}", flush=True)
    with gzip.open(tmp_gz, "rb") as src, output.open("wb") as dst:
        shutil.copyfileobj(src, dst)
    output.chmod(output.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    tmp_gz.unlink(missing_ok=True)
    print(f"wrote {output}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
