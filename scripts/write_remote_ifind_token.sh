#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=remote_env.sh
source "$SCRIPT_DIR/remote_env.sh"
require_remote_host

echo "请输入账号详情页里的 Refresh Token，不是“密钥 / 个人令牌 / API 密钥”。"
read -r -s -p "请输入 iFinD Refresh Token: " IFIND_REFRESH_TOKEN
echo

PAYLOAD_FILE="$(mktemp)"
REMOTE_PAYLOAD="/tmp/surveil-ifind-token-$$.json"
cleanup() {
  rm -f "$PAYLOAD_FILE"
}
trap cleanup EXIT

PAYLOAD_FILE="$PAYLOAD_FILE" IFIND_REFRESH_TOKEN="$IFIND_REFRESH_TOKEN" python3 - <<'PY'
from pathlib import Path
import json
import os

payload = {"IFIND_REFRESH_TOKEN": os.environ["IFIND_REFRESH_TOKEN"]}
path = Path(os.environ["PAYLOAD_FILE"])
path.write_text(json.dumps(payload), encoding="utf-8")
path.chmod(0o600)
PY

scp -i "$REMOTE_SSH_KEY" -o IdentitiesOnly=yes "$PAYLOAD_FILE" "$REMOTE_USER@$REMOTE_HOST:$REMOTE_PAYLOAD" >/dev/null

ssh -i "$REMOTE_SSH_KEY" -o IdentitiesOnly=yes "$REMOTE_USER@$REMOTE_HOST" \
  "REMOTE_ENV='$REMOTE_ENV' REMOTE_PAYLOAD='$REMOTE_PAYLOAD' REMOTE_SERVICE_USER='$REMOTE_SERVICE_USER' python3 - <<'PY'
from pathlib import Path
import json
import os
import pwd
import grp

env_path = Path(os.environ['REMOTE_ENV'])
env_path.parent.mkdir(parents=True, exist_ok=True)
payload_path = Path(os.environ['REMOTE_PAYLOAD'])
payload = json.loads(payload_path.read_text(encoding='utf-8'))

updates = {
    'IFIND_API_BASE_URL': 'https://quantapi.51ifind.com/api/v1',
    'IFIND_REFRESH_TOKEN': payload['IFIND_REFRESH_TOKEN'],
}

lines = env_path.read_text(encoding='utf-8').splitlines() if env_path.exists() else []
seen = set()
out = []
for line in lines:
    stripped = line.strip()
    key = stripped.split('=', 1)[0] if '=' in stripped and not stripped.startswith('#') else ''
    if key in updates:
        out.append(f'{key}={updates[key]}')
        seen.add(key)
    else:
        out.append(line)
for key, value in updates.items():
    if key not in seen:
        out.append(f'{key}={value}')
env_path.write_text('\\n'.join(out) + '\\n', encoding='utf-8')
env_path.chmod(0o600)
service_user = os.environ.get('REMOTE_SERVICE_USER') or 'surveil'
try:
    uid = pwd.getpwnam(service_user).pw_uid
    gid = grp.getgrnam(service_user).gr_gid
    os.chown(env_path, uid, gid)
except KeyError:
    pass
payload_path.unlink(missing_ok=True)
print(f'已更新 {env_path}: IFIND_REFRESH_TOKEN=<redacted>')
PY"

unset IFIND_REFRESH_TOKEN
