#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=remote_env.sh
source "$SCRIPT_DIR/remote_env.sh"
require_remote_host
MIHOMO_VERSION="${MIHOMO_VERSION:-}"

SSH=(ssh -i "$REMOTE_SSH_KEY" -o IdentitiesOnly=yes "$REMOTE_USER@$REMOTE_HOST")
RSYNC_RSH="ssh -i $REMOTE_SSH_KEY -o IdentitiesOnly=yes"

TMP_DIR="$(mktemp -d)"
cleanup() {
  rm -rf "$TMP_DIR"
}
trap cleanup EXIT

echo "==> download mihomo on local Mac from official MetaCubeX GitHub release"
python3 scripts/download_mihomo.py --version "$MIHOMO_VERSION" --output "$TMP_DIR/mihomo"

echo "==> sync proxy code and systemd units"
rsync -az -e "$RSYNC_RSH" \
  ./scripts/update_mihomo_config.py \
  "$REMOTE_USER@$REMOTE_HOST:$REMOTE_DIR/scripts/update_mihomo_config.py"

escape_sed_replacement() {
  printf '%s' "$1" | sed -e 's/[\/&]/\\&/g'
}

RENDERED_SYSTEMD="$TMP_DIR/systemd"
mkdir -p "$RENDERED_SYSTEMD"
REMOTE_DIR_ESCAPED="$(escape_sed_replacement "$REMOTE_DIR")"
REMOTE_PROXY_DIR_ESCAPED="$(escape_sed_replacement "$REMOTE_PROXY_DIR")"
REMOTE_SERVICE_USER_ESCAPED="$(escape_sed_replacement "$REMOTE_SERVICE_USER")"
for unit in ./systemd/surveil-proxy.service ./systemd/surveil-overseas-media.service; do
  sed \
    -e "s/User=surveil/User=$REMOTE_SERVICE_USER_ESCAPED/g" \
    -e "s/\/opt\/surveil-proxy/$REMOTE_PROXY_DIR_ESCAPED/g" \
    -e "s/\/opt\/surveil/$REMOTE_DIR_ESCAPED/g" \
    "$unit" > "$RENDERED_SYSTEMD/$(basename "$unit")"
done
rsync -az -e "$RSYNC_RSH" "$RENDERED_SYSTEMD/" "$REMOTE_USER@$REMOTE_HOST:/tmp/surveil-proxy-systemd/"

echo "==> upload verified mihomo binary and install"
scp -i "$REMOTE_SSH_KEY" -o IdentitiesOnly=yes "$TMP_DIR/mihomo" "$REMOTE_USER@$REMOTE_HOST:/tmp/mihomo" >/dev/null
"${SSH[@]}" "set -euo pipefail
REMOTE_DIR='$REMOTE_DIR'
REMOTE_PROXY_DIR='$REMOTE_PROXY_DIR'
REMOTE_SERVICE_USER='$REMOTE_SERVICE_USER'
id \"\$REMOTE_SERVICE_USER\" >/dev/null 2>&1 || useradd --system --home \"\$REMOTE_DIR\" --shell /usr/sbin/nologin \"\$REMOTE_SERVICE_USER\"
mkdir -p \"\$REMOTE_PROXY_DIR\" \"\$REMOTE_DIR/scripts\"
chown -R \"\$REMOTE_SERVICE_USER:\$REMOTE_SERVICE_USER\" \"\$REMOTE_PROXY_DIR\"
chmod 700 \"\$REMOTE_PROXY_DIR\"
install -m 0755 /tmp/mihomo /usr/local/bin/mihomo
rm -f /tmp/mihomo
cat > \"\$REMOTE_DIR/proxy.env\" <<'EOF'
HTTP_PROXY=http://127.0.0.1:7890
HTTPS_PROXY=http://127.0.0.1:7890
ALL_PROXY=socks5://127.0.0.1:7890
NO_PROXY=127.0.0.1,localhost,$REMOTE_HOST,quantapi.51ifind.com,open.feishu.cn,api.deepseek.com,token-plan.cn-beijing.maas.aliyuncs.com,api.z.ai
EOF
chown \"\$REMOTE_SERVICE_USER:\$REMOTE_SERVICE_USER\" \"\$REMOTE_DIR/proxy.env\"
chmod 600 \"\$REMOTE_DIR/proxy.env\"
cp /tmp/surveil-proxy-systemd/*.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable surveil-proxy.service
systemctl restart surveil-overseas-media.timer
/usr/local/bin/mihomo -v
systemctl status --no-pager surveil-overseas-media.timer
"

echo "代理框架已安装。下一步运行 ./scripts/write_remote_proxy_subscription.sh 写入订阅链接并启动 surveil-proxy.service。"
