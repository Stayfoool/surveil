#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=remote_env.sh
source "$SCRIPT_DIR/remote_env.sh"
require_remote_host

echo "请输入飞书自定义机器人的 Webhook URL 和签名 Secret。"
echo "不要把这些值发到聊天里；本脚本会隐藏输入并写入远程 ${REMOTE_ENV}。"
read -r -s -p "请输入 FEISHU_WEBHOOK: " FEISHU_WEBHOOK
echo
read -r -s -p "请输入 FEISHU_SECRET: " FEISHU_SECRET
echo

PAYLOAD_FILE="$(mktemp)"
REMOTE_PAYLOAD="/tmp/surveil-feishu-$$.json"
cleanup() {
  rm -f "$PAYLOAD_FILE"
}
trap cleanup EXIT

PAYLOAD_FILE="$PAYLOAD_FILE" \
FEISHU_WEBHOOK="$FEISHU_WEBHOOK" \
FEISHU_SECRET="$FEISHU_SECRET" \
python3 - <<'PY'
from pathlib import Path
import json
import os

payload = {
    "FEISHU_WEBHOOK": os.environ["FEISHU_WEBHOOK"],
    "FEISHU_SECRET": os.environ["FEISHU_SECRET"],
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

updates = {
    'FEISHU_WEBHOOK': payload['FEISHU_WEBHOOK'],
    'FEISHU_SECRET': payload['FEISHU_SECRET'],
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
print(f'已更新 {env_path}: FEISHU_WEBHOOK=<redacted>, FEISHU_SECRET=<redacted>')
PY"

unset FEISHU_WEBHOOK FEISHU_SECRET
