#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=remote_env.sh
source "$SCRIPT_DIR/remote_env.sh"
require_remote_host

LOCAL_ENV="${LOCAL_ENV:-.env}"

if [ ! -f "$LOCAL_ENV" ]; then
  echo "本地 .env 不存在：$LOCAL_ENV" >&2
  exit 1
fi

TMP_DIR="$(mktemp -d)"
cleanup() {
  rm -rf "$TMP_DIR"
}
trap cleanup EXIT

PAYLOAD_FILE="$TMP_DIR/x-credentials.json"
REMOTE_PAYLOAD="/tmp/surveil-x-credentials-$$.json"

LOCAL_ENV="$LOCAL_ENV" PAYLOAD_FILE="$PAYLOAD_FILE" python3 - <<'PY'
from pathlib import Path
import json
import os

env_path = Path(os.environ["LOCAL_ENV"])
payload_path = Path(os.environ["PAYLOAD_FILE"])
wanted = {
    "X_USERNAME",
    "X_BEARER_TOKEN",
    "X_CLIENT_ID",
    "X_CLIENT_SECRET",
    "X_REDIRECT_URI",
}
values: dict[str, str] = {}
for raw_line in env_path.read_text(encoding="utf-8").splitlines():
    line = raw_line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    key, value = line.split("=", 1)
    key = key.strip()
    if key not in wanted:
        continue
    value = value.strip().strip('"').strip("'")
    if value:
        values[key] = value

missing = [key for key in ("X_USERNAME", "X_BEARER_TOKEN") if not values.get(key)]
if missing:
    raise SystemExit("本地 .env 缺少：" + ", ".join(missing))

payload_path.write_text(json.dumps(values, ensure_ascii=False), encoding="utf-8")
payload_path.chmod(0o600)
print("将同步到服务器的 X 配置键：" + ", ".join(sorted(values)))
print("不会同步 X_ACCESS_TOKEN / X_REFRESH_TOKEN；服务器将优先使用 X_BEARER_TOKEN。")
PY

scp -i "$REMOTE_SSH_KEY" -o IdentitiesOnly=yes "$PAYLOAD_FILE" "$REMOTE_USER@$REMOTE_HOST:$REMOTE_PAYLOAD" >/dev/null

ssh -i "$REMOTE_SSH_KEY" -o IdentitiesOnly=yes "$REMOTE_USER@$REMOTE_HOST" \
  "REMOTE_DIR='$REMOTE_DIR' REMOTE_PAYLOAD='$REMOTE_PAYLOAD' REMOTE_SERVICE_USER='$REMOTE_SERVICE_USER' python3 - <<'PY'
from pathlib import Path
import json
import os
import pwd
import grp

remote_dir = Path(os.environ['REMOTE_DIR'])
env_path = remote_dir / '.env'
payload_path = Path(os.environ['REMOTE_PAYLOAD'])
payload = json.loads(payload_path.read_text(encoding='utf-8'))
payload_path.unlink(missing_ok=True)

remote_dir.mkdir(parents=True, exist_ok=True)
lines = env_path.read_text(encoding='utf-8').splitlines() if env_path.exists() else []
out: list[str] = []
seen: set[str] = set()
managed_keys = set(payload) | {'X_ACCESS_TOKEN', 'X_REFRESH_TOKEN'}

for line in lines:
    stripped = line.strip()
    if '=' not in stripped or stripped.startswith('#'):
        out.append(line)
        continue
    key = stripped.split('=', 1)[0].strip()
    if key in {'X_ACCESS_TOKEN', 'X_REFRESH_TOKEN'}:
        continue
    if key in payload:
        out.append(f'{key}={payload[key]}')
        seen.add(key)
    else:
        out.append(line)

if payload and out and out[-1].strip():
    out.append('')
out.append('# X / Serenity monitor credentials')
for key in sorted(payload):
    if key not in seen:
        out.append(f'{key}={payload[key]}')

env_path.write_text('\\n'.join(out).rstrip() + '\\n', encoding='utf-8')
env_path.chmod(0o600)
service_user = os.environ.get('REMOTE_SERVICE_USER') or 'surveil'
try:
    uid = pwd.getpwnam(service_user).pw_uid
    gid = grp.getgrnam(service_user).gr_gid
    os.chown(env_path, uid, gid)
except KeyError:
    pass

print('已更新 /opt/surveil/.env: X_USERNAME, X_BEARER_TOKEN=<redacted>')
PY"
