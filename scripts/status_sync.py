#!/usr/bin/env python3
"""Check GitHub/local/server revision alignment."""

from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def run(cmd: list[str], *, cwd: Path = ROOT, check: bool = False) -> tuple[int, str]:
    proc = subprocess.run(cmd, cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
    output = proc.stdout.strip()
    if check and proc.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} failed:\n{output}")
    return proc.returncode, output


def git_value(*args: str) -> str:
    code, output = run(["git", *args])
    return output if code == 0 else ""


def local_status(fetch: bool) -> dict[str, str]:
    if fetch:
        run(["git", "fetch", "origin", "--prune"])
    branch = git_value("branch", "--show-current") or "unknown"
    commit = git_value("rev-parse", "HEAD") or "unknown"
    origin_commit = git_value("rev-parse", f"origin/{branch}") or "unknown"
    status = git_value("status", "--porcelain")
    return {
        "branch": branch,
        "commit": commit,
        "origin_commit": origin_commit,
        "dirty": "1" if status else "0",
        "dirty_summary": status,
    }


def remote_revision() -> dict[str, str]:
    host = os.getenv("REMOTE_HOST", "").strip()
    user = os.getenv("REMOTE_USER", "root").strip()
    key = os.path.expanduser(os.getenv("REMOTE_SSH_KEY", "~/.ssh/id_ed25519").strip())
    remote_dir = os.getenv("REMOTE_DIR", "/opt/surveil").strip()
    if not host:
        return {"available": "0", "reason": "REMOTE_HOST 未配置"}
    cmd = [
        "ssh",
        "-i",
        key,
        "-o",
        "IdentitiesOnly=yes",
        f"{user}@{host}",
        f"test -f {remote_dir}/REVISION && cat {remote_dir}/REVISION || true",
    ]
    code, output = run(cmd)
    if code != 0:
        return {"available": "0", "reason": output or "ssh failed"}
    parsed: dict[str, str] = {"available": "1"}
    for line in output.splitlines():
        if "=" not in line:
            continue
        key_name, value = line.split("=", 1)
        parsed[key_name.strip()] = value.strip()
    if "commit" not in parsed:
        parsed["available"] = "0"
        parsed["reason"] = f"{remote_dir}/REVISION 不存在或为空"
    return parsed


def short(value: str) -> str:
    return value[:12] if value and value != "unknown" else value or "unknown"


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare local, origin, and deployed server revisions.")
    parser.add_argument("--no-fetch", action="store_true", help="Do not git fetch origin before comparing.")
    parser.add_argument("--strict", action="store_true", help="Exit non-zero when revisions differ or local tree is dirty.")
    args = parser.parse_args()

    local = local_status(fetch=not args.no_fetch)
    remote = remote_revision()

    print("Sync status")
    print(f"- local branch: {local['branch']}")
    print(f"- local HEAD:   {short(local['commit'])}")
    print(f"- origin HEAD:  {short(local['origin_commit'])}")
    print(f"- local dirty:  {'yes' if local['dirty'] == '1' else 'no'}")
    if remote.get("available") == "1":
        print(f"- server HEAD:  {short(remote.get('commit', ''))}")
        print(f"- server dirty: {'yes' if remote.get('dirty') == '1' else 'no'}")
        print(f"- deployed at:  {remote.get('deployed_at', 'unknown')}")
    else:
        print(f"- server HEAD:  unavailable ({remote.get('reason', 'unknown')})")

    problems: list[str] = []
    if local["dirty"] == "1":
        problems.append("local worktree has uncommitted changes")
    if local["commit"] != local["origin_commit"]:
        problems.append("local HEAD differs from origin HEAD")
    if remote.get("available") == "1" and remote.get("commit") != local["origin_commit"]:
        problems.append("server deployed commit differs from origin HEAD")
    if remote.get("available") == "1" and remote.get("dirty") == "1":
        problems.append("server was deployed from a dirty local worktree")

    if problems:
        print("\nNot fully synchronized:")
        for problem in problems:
            print(f"- {problem}")
        if local["dirty_summary"]:
            print("\nLocal changes:")
            print(local["dirty_summary"])
        return 1 if args.strict else 0

    print("\nAll checked revisions are synchronized.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
