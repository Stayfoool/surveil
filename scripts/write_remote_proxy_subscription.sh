#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=remote_env.sh
source "$SCRIPT_DIR/remote_env.sh"
require_remote_host
LOCAL_PROXY="${LOCAL_PROXY:-}"

echo "将把 mihomo/clash 订阅链接写入远程服务器：$REMOTE_HOST:$REMOTE_PROXY_DIR/subscription.url"
echo "订阅链接是敏感信息，不会打印明文；直接回车会保留远程现有订阅。"
echo "配置生成在本地 Mac 完成，适合订阅服务拒绝华为云 IP 直接访问的情况。"
echo

CURRENT_MARKER="$(ssh -n -i "$REMOTE_SSH_KEY" -o IdentitiesOnly=yes "$REMOTE_USER@$REMOTE_HOST" \
  "REMOTE_PROXY_DIR='$REMOTE_PROXY_DIR' python3 - <<'PY'
from pathlib import Path
import os
path = Path(os.environ['REMOTE_PROXY_DIR']) / 'subscription.url'
print('configured' if path.exists() and path.read_text(encoding='utf-8').strip() else 'empty')
PY")"
echo "当前远程代理订阅：$([ "$CURRENT_MARKER" = configured ] && echo '<已配置>' || echo '<未配置>')"

printf "请输入 mihomo/clash 订阅链接（回车保留现有值）: "
IFS= read -r -s SUBSCRIPTION_URL
echo

TMP_DIR="$(mktemp -d)"
cleanup() {
  rm -rf "$TMP_DIR"
}
trap cleanup EXIT

PAYLOAD_FILE="$TMP_DIR/subscription.json"
RAW_CONFIG="$TMP_DIR/subscription.yaml"
LOCAL_CONFIG="$TMP_DIR/config.yaml"
REMOTE_PAYLOAD="/tmp/surveil-proxy-subscription-$$.json"
REMOTE_CONFIG="/tmp/surveil-proxy-config-$$.yaml"

SUBSCRIPTION_URL="$SUBSCRIPTION_URL" PAYLOAD_FILE="$PAYLOAD_FILE" python3 - <<'PY'
from pathlib import Path
import json
import os
payload = {"SUBSCRIPTION_URL": os.environ["SUBSCRIPTION_URL"]}
path = Path(os.environ["PAYLOAD_FILE"])
path.write_text(json.dumps(payload), encoding="utf-8")
path.chmod(0o600)
PY

scp -i "$REMOTE_SSH_KEY" -o IdentitiesOnly=yes "$PAYLOAD_FILE" "$REMOTE_USER@$REMOTE_HOST:$REMOTE_PAYLOAD" >/dev/null

ssh -i "$REMOTE_SSH_KEY" -o IdentitiesOnly=yes "$REMOTE_USER@$REMOTE_HOST" \
  "REMOTE_PROXY_DIR='$REMOTE_PROXY_DIR' REMOTE_PAYLOAD='$REMOTE_PAYLOAD' REMOTE_SERVICE_USER='$REMOTE_SERVICE_USER' python3 - <<'PY'
from pathlib import Path
import json
import os
import pwd
import grp

proxy_dir = Path(os.environ['REMOTE_PROXY_DIR'])
proxy_dir.mkdir(parents=True, exist_ok=True)
payload_path = Path(os.environ['REMOTE_PAYLOAD'])
payload = json.loads(payload_path.read_text(encoding='utf-8'))
subscription = str(payload.get('SUBSCRIPTION_URL') or '').strip()
subscription_path = proxy_dir / 'subscription.url'
if subscription:
    subscription_path.write_text(subscription + '\\n', encoding='utf-8')
    changed = True
else:
    changed = False
if not subscription_path.exists() or not subscription_path.read_text(encoding='utf-8').strip():
    raise SystemExit('订阅链接为空，请重新运行脚本并输入订阅链接')
subscription_path.chmod(0o600)
service_user = os.environ.get('REMOTE_SERVICE_USER') or 'surveil'
try:
    uid = pwd.getpwnam(service_user).pw_uid
    gid = grp.getgrnam(service_user).gr_gid
    os.chown(proxy_dir, uid, gid)
    os.chown(subscription_path, uid, gid)
except KeyError:
    pass
payload_path.unlink(missing_ok=True)
print(f'已更新 {subscription_path}: ' + ('<redacted>' if changed else '<保留现有值>'))
PY"

if [ -n "$SUBSCRIPTION_URL" ]; then
  FETCH_URL="$SUBSCRIPTION_URL"
else
  FETCH_URL="$(ssh -n -i "$REMOTE_SSH_KEY" -o IdentitiesOnly=yes "$REMOTE_USER@$REMOTE_HOST" \
    "python3 - <<'PY'
from pathlib import Path
path = Path('$REMOTE_PROXY_DIR') / 'subscription.url'
print(path.read_text(encoding='utf-8').strip())
PY")"
fi

echo "==> 在本地 Mac 拉取订阅内容并生成本机限定 mihomo 配置"
FETCH_URL="$FETCH_URL" RAW_CONFIG="$RAW_CONFIG" LOCAL_PROXY="$LOCAL_PROXY" python3 - <<'PY'
from pathlib import Path
import os
import urllib.request

url = os.environ["FETCH_URL"]
raw_path = Path(os.environ["RAW_CONFIG"])
local_proxy = os.environ.get("LOCAL_PROXY", "").strip()
handlers = []
if local_proxy:
    handlers.append(urllib.request.ProxyHandler({"http": local_proxy, "https": local_proxy}))
opener = urllib.request.build_opener(*handlers)
request = urllib.request.Request(
    url,
    headers={
        "User-Agent": "ClashMetaForAndroid/2.10.1 mihomo/1.19.27",
        "Accept": "text/yaml, text/plain, */*",
    },
)
with opener.open(request, timeout=60) as response:
    raw_path.write_bytes(response.read())
raw_path.chmod(0o600)
print(f"已拉取订阅内容：{raw_path.stat().st_size} bytes")
PY

python3 scripts/update_mihomo_config.py --input "$RAW_CONFIG" --output "$LOCAL_CONFIG"

scp -i "$REMOTE_SSH_KEY" -o IdentitiesOnly=yes "$LOCAL_CONFIG" "$REMOTE_USER@$REMOTE_HOST:$REMOTE_CONFIG" >/dev/null

ssh -i "$REMOTE_SSH_KEY" -o IdentitiesOnly=yes "$REMOTE_USER@$REMOTE_HOST" \
  "REMOTE_PROXY_DIR='$REMOTE_PROXY_DIR' REMOTE_CONFIG='$REMOTE_CONFIG' REMOTE_SERVICE_USER='$REMOTE_SERVICE_USER' bash -s" <<'SH'
set -euo pipefail
install -m 0600 "$REMOTE_CONFIG" "$REMOTE_PROXY_DIR/config.yaml"
rm -f "$REMOTE_CONFIG"
chown "$REMOTE_SERVICE_USER:$REMOTE_SERVICE_USER" "$REMOTE_PROXY_DIR/config.yaml"
systemctl restart surveil-proxy.service
sleep 3
systemctl status --no-pager surveil-proxy.service
python3 - <<'PY'
import urllib.request

proxy = 'http://127.0.0.1:7890'
opener = urllib.request.build_opener(
    urllib.request.ProxyHandler({'http': proxy, 'https': proxy})
)
for url in ['https://xtech.nikkei.com/rss/index.rdf', 'https://www.google.com/generate_204']:
    print('\nproxy test', url)
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'surveil-proxy-test/0.1'})
        with opener.open(req, timeout=20) as response:
            print('status', response.status, 'type', response.headers.get('content-type'))
            print(response.read(120).decode('utf-8', errors='replace').replace('\n', ' ')[:120])
    except Exception as exc:
        print(type(exc).__name__, exc)
PY
SH

unset SUBSCRIPTION_URL
