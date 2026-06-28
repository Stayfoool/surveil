#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=remote_env.sh
source "$SCRIPT_DIR/remote_env.sh"
require_remote_host

echo "请输入浏览器里韭研公社的登录 cookie。最少通常需要 SESSION=...；完整 Cookie 也可以。"
echo "不要把 cookie 发到聊天里；本脚本会隐藏输入并写入远程 ${REMOTE_ENV}。"
echo "直接回车会保留远程现有值。"
read -r -s -p "请输入 JYGS_COOKIE: " JYGS_COOKIE
echo
echo "JYGS_SIGN_SECRET 是韭研接口签名参数；仅在你确认有权使用该接口时写入私有 .env。"
read -r -s -p "请输入 JYGS_SIGN_SECRET: " JYGS_SIGN_SECRET
echo

PAYLOAD_FILE="$(mktemp)"
REMOTE_PAYLOAD="/tmp/surveil-jygs-cookie-$$.json"
cleanup() {
  rm -f "$PAYLOAD_FILE"
}
trap cleanup EXIT

PAYLOAD_FILE="$PAYLOAD_FILE" JYGS_COOKIE="$JYGS_COOKIE" JYGS_SIGN_SECRET="$JYGS_SIGN_SECRET" python3 - <<'PY'
from pathlib import Path
import json
import os

payload = {
    "JYGS_COOKIE": os.environ["JYGS_COOKIE"],
    "JYGS_SIGN_SECRET": os.environ["JYGS_SIGN_SECRET"],
}
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

updates = {key: str(value) for key, value in payload.items() if str(value).strip()}
if not updates:
    print(f'未更新 {env_path}: JYGS_COOKIE/JYGS_SIGN_SECRET 均保留现有值')
    payload_path.unlink(missing_ok=True)
    raise SystemExit(0)

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
changed = ', '.join(f'{key}=<redacted>' for key in updates)
print(f'已更新 {env_path}: {changed}')
PY"

unset JYGS_COOKIE
unset JYGS_SIGN_SECRET
