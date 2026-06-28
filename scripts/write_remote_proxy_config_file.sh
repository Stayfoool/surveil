#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=remote_env.sh
source "$SCRIPT_DIR/remote_env.sh"
require_remote_host

CONFIG_SOURCE="${1:-}"
if [ -z "$CONFIG_SOURCE" ]; then
  printf "请输入本地 mihomo/clash YAML 配置文件路径: "
  IFS= read -r CONFIG_SOURCE
fi

if [ -z "$CONFIG_SOURCE" ] || [ ! -f "$CONFIG_SOURCE" ]; then
  echo "配置文件不存在：$CONFIG_SOURCE" >&2
  exit 1
fi

TMP_DIR="$(mktemp -d)"
cleanup() {
  rm -rf "$TMP_DIR"
}
trap cleanup EXIT

LOCAL_CONFIG="$TMP_DIR/config.yaml"
REMOTE_CONFIG="/tmp/surveil-proxy-config-$$.yaml"

echo "将从本地配置文件生成服务器本机限定 mihomo 配置：$CONFIG_SOURCE"
echo "配置内容包含代理节点等敏感信息，不会打印明文。"
echo

python3 scripts/update_mihomo_config.py --input "$CONFIG_SOURCE" --output "$LOCAL_CONFIG" >/dev/null

if ! tail -n 20 "$LOCAL_CONFIG" | grep -q '^  - MATCH,'; then
  echo "生成后的配置缺少 MATCH 规则，请检查源配置是否为有效 mihomo/clash YAML。" >&2
  exit 1
fi

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

proxy = "http://127.0.0.1:7890"
opener = urllib.request.build_opener(
    urllib.request.ProxyHandler({"http": proxy, "https": proxy})
)
for url in ["https://xtech.nikkei.com/rss/index.rdf", "https://www.google.com/generate_204"]:
    print("\nproxy test", url)
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "surveil-proxy-test/0.1"})
        with opener.open(request, timeout=30) as response:
            print("status", response.status, "type", response.headers.get("content-type"))
            body = response.read(120).decode("utf-8", errors="replace").replace("\n", " ")
            print(body[:120])
    except Exception as exc:
        print(type(exc).__name__, exc)
PY
SH
