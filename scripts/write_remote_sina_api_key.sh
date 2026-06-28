#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=remote_env.sh
source "$SCRIPT_DIR/remote_env.sh"
require_remote_host

echo "将写入远程服务器 $REMOTE_HOST:$REMOTE_ENV"
echo "直接回车 = 保留远程现有新浪智研 API Key；输入新值 = 覆盖。"
echo "API Key 来自新浪财经智研平台「我的服务 / API Key」。"
echo

ssh -n -i "$REMOTE_SSH_KEY" -o IdentitiesOnly=yes "$REMOTE_USER@$REMOTE_HOST" \
  "REMOTE_ENV='$REMOTE_ENV' python3 - <<'PY'
from pathlib import Path
import os

env_path = Path(os.environ['REMOTE_ENV'])
values = {}
if env_path.exists():
    for raw_line in env_path.read_text(encoding='utf-8').splitlines():
        line = raw_line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        values[key.strip()] = value.strip().strip('\"').strip(\"'\")

print('当前远程新浪配置（敏感值不显示）：')
print(f'- SINA_NEWS_PROVIDER: {values.get(\"SINA_NEWS_PROVIDER\", \"<未配置>\") or \"<未配置>\"}')
print(f'- SINA_ZY_API_BASE_URL: {values.get(\"SINA_ZY_API_BASE_URL\", \"<未配置>\") or \"<未配置>\"}')
print(f'- SINA_ZY_MCP_URL: {values.get(\"SINA_ZY_MCP_URL\", \"<未配置>\") or \"<未配置>\"}')
print('- SINA_ZY_API_KEY: ' + ('<已配置>' if values.get('SINA_ZY_API_KEY') else '<未配置>'))
PY"
echo

printf "请输入新浪财经智研 API Key（回车保留现有值）: "
IFS= read -r -s SINA_ZY_API_KEY
echo
printf "请输入新浪智研 OpenAPI Base URL（回车保留现有值；需从接口详情页确认）: "
IFS= read -r SINA_ZY_API_BASE_URL

PAYLOAD_FILE="$(mktemp)"
REMOTE_PAYLOAD="/tmp/surveil-sina-zy-$$.json"
cleanup() {
  rm -f "$PAYLOAD_FILE"
}
trap cleanup EXIT

PAYLOAD_FILE="$PAYLOAD_FILE" \
SINA_ZY_API_KEY="$SINA_ZY_API_KEY" \
SINA_ZY_API_BASE_URL="$SINA_ZY_API_BASE_URL" \
python3 - <<'PY'
from pathlib import Path
import json
import os

payload = {
    "SINA_ZY_API_KEY": os.environ["SINA_ZY_API_KEY"],
    "SINA_ZY_API_BASE_URL": os.environ["SINA_ZY_API_BASE_URL"],
}
path = Path(os.environ["PAYLOAD_FILE"])
path.write_text(json.dumps(payload), encoding="utf-8")
path.chmod(0o600)
PY

scp -i "$REMOTE_SSH_KEY" -o IdentitiesOnly=yes "$PAYLOAD_FILE" "$REMOTE_USER@$REMOTE_HOST:$REMOTE_PAYLOAD" >/dev/null

ssh -n -i "$REMOTE_SSH_KEY" -o IdentitiesOnly=yes "$REMOTE_USER@$REMOTE_HOST" \
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
    'SINA_NEWS_PROVIDER': 'zy_api',
}

api_key = str(payload.get('SINA_ZY_API_KEY') or '').strip()
api_base_url = str(payload.get('SINA_ZY_API_BASE_URL') or '').strip()
if api_key:
    updates['SINA_ZY_API_KEY'] = api_key
if api_base_url:
    updates['SINA_ZY_API_BASE_URL'] = api_base_url

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
changed = ['SINA_NEWS_PROVIDER=zy_api']
if api_base_url:
    changed.append('SINA_ZY_API_BASE_URL=' + api_base_url)
else:
    changed.append('SINA_ZY_API_BASE_URL=<保留现有值>')
if api_key:
    changed.append('SINA_ZY_API_KEY=<redacted>')
else:
    changed.append('SINA_ZY_API_KEY=<保留现有值>')
print(f'已更新 {env_path}: ' + ', '.join(changed))
PY"

unset SINA_ZY_API_KEY SINA_ZY_API_BASE_URL
