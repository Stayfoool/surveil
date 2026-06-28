#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=remote_env.sh
source "$SCRIPT_DIR/remote_env.sh"
require_remote_host

SSH=(ssh -i "$REMOTE_SSH_KEY" -o IdentitiesOnly=yes "$REMOTE_USER@$REMOTE_HOST")

printf '请输入持仓 Web UI 访问令牌（留空则自动生成）: '
IFS= read -r token || true
if [ -z "$token" ]; then
  token="$(python3 - <<'PY'
import secrets
print(secrets.token_urlsafe(24))
PY
)"
fi

"${SSH[@]}" "set -euo pipefail
cd '$REMOTE_DIR'
touch .env
python3 - <<'PY'
from pathlib import Path
path = Path('.env')
token = '''$token'''
lines = path.read_text(encoding='utf-8').splitlines() if path.exists() else []
out = []
seen = False
for line in lines:
    if line.startswith('HOLDINGS_WEB_TOKEN='):
        out.append('HOLDINGS_WEB_TOKEN=' + token)
        seen = True
    else:
        out.append(line)
if not seen:
    out.append('HOLDINGS_WEB_TOKEN=' + token)
path.write_text('\\n'.join(out).rstrip() + '\\n', encoding='utf-8')
PY
chmod 600 .env
systemctl restart surveil-holdings-web.service 2>/dev/null || true
"

echo "已写入远程 HOLDINGS_WEB_TOKEN。访问 Web UI 时请输入：$token"
