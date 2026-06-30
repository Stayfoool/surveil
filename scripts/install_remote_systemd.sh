#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=remote_env.sh
source "$SCRIPT_DIR/remote_env.sh"
require_remote_host

SSH=(ssh -i "$REMOTE_SSH_KEY" -o IdentitiesOnly=yes "$REMOTE_USER@$REMOTE_HOST")
RSYNC_RSH="ssh -i $REMOTE_SSH_KEY -o IdentitiesOnly=yes"

TMP_DIR="$(mktemp -d)"
cleanup() {
  rm -rf "$TMP_DIR"
}
trap cleanup EXIT

escape_sed_replacement() {
  printf '%s' "$1" | sed -e 's/[\/&]/\\&/g'
}

echo "==> render and sync systemd units"
RENDERED_SYSTEMD="$TMP_DIR/systemd"
mkdir -p "$RENDERED_SYSTEMD"
REMOTE_DIR_ESCAPED="$(escape_sed_replacement "$REMOTE_DIR")"
REMOTE_PROXY_DIR_ESCAPED="$(escape_sed_replacement "$REMOTE_PROXY_DIR")"
REMOTE_SERVICE_USER_ESCAPED="$(escape_sed_replacement "$REMOTE_SERVICE_USER")"
for unit in ./systemd/*.service ./systemd/*.timer; do
  sed \
    -e "s/User=surveil/User=$REMOTE_SERVICE_USER_ESCAPED/g" \
    -e "s/\/opt\/surveil-proxy/$REMOTE_PROXY_DIR_ESCAPED/g" \
    -e "s/\/opt\/surveil/$REMOTE_DIR_ESCAPED/g" \
    "$unit" > "$RENDERED_SYSTEMD/$(basename "$unit")"
done
rsync -az -e "$RSYNC_RSH" "$RENDERED_SYSTEMD/" "$REMOTE_USER@$REMOTE_HOST:/tmp/surveil-systemd/"

echo "==> install units"
"${SSH[@]}" "set -euo pipefail
cp /tmp/surveil-systemd/*.service /etc/systemd/system/
cp /tmp/surveil-systemd/*.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable surveil-db-init.service
systemctl start surveil-db-init.service
systemctl is-enabled surveil-db-init.service
journalctl -u surveil-db-init.service -n 20 --no-pager
systemctl enable --now surveil-ifind-notice.timer
systemctl enable --now surveil-sina-stock-news.timer
systemctl enable --now surveil-overseas-media.timer
systemctl enable --now surveil-china-media.timer
systemctl enable --now surveil-article-daily.timer
systemctl enable --now surveil-signals-extract.timer
systemctl enable --now surveil-signal-outcome.timer
systemctl enable --now surveil-signal-review.timer
systemctl enable --now surveil-signal-digest.timer
systemctl enable surveil-stock-relations-import.service
systemctl start surveil-stock-relations-import.service || true
systemctl enable --now surveil-rss-monitor.service
systemctl enable --now surveil-trendforce-page-monitor.service
if grep -Eq '^(IFIND_RESEARCH_FORMULA|IFIND_REPORT_FORMULA|IFIND_RESEARCH_REPORTNAME|IFIND_REPORT_REPORTNAME|IFIND_RESEARCH_REPORT_TYPE|IFIND_REPORT_REPORT_TYPE)=[^[:space:]]+' '$REMOTE_DIR/.env' 2>/dev/null; then
  systemctl enable --now surveil-ifind-report.timer
else
  systemctl disable --now surveil-ifind-report.timer >/dev/null 2>&1 || true
  echo 'iFinD 研报配置为空，保持 surveil-ifind-report.timer 停用。'
fi
if grep -Eq '^ENABLE_JYGS_TIMER=1$' '$REMOTE_DIR/.env' 2>/dev/null; then
  systemctl enable --now surveil-jygs-actions.timer
else
  systemctl disable --now surveil-jygs-actions.timer >/dev/null 2>&1 || true
  echo '韭研公社异动模块当前默认搁置；如需启用，请在 .env 设置 ENABLE_JYGS_TIMER=1。'
fi
systemctl enable --now surveil-holdings-web.service
systemctl enable surveil-sina-flash.service
systemctl restart surveil-sina-flash.service
if grep -Eq '^X_BEARER_TOKEN=[^[:space:]]+' '$REMOTE_DIR/.env' 2>/dev/null; then
  systemctl enable --now surveil-x-stream.service
else
  systemctl disable --now surveil-x-stream.service >/dev/null 2>&1 || true
  echo 'X_BEARER_TOKEN 未配置，保持 surveil-x-stream.service 停用。'
fi
systemctl list-timers --all 'surveil-*' --no-pager
systemctl --no-pager --full status surveil-sina-flash.service || true
systemctl --no-pager --full status surveil-holdings-web.service || true
systemctl --no-pager --full status surveil-rss-monitor.service || true
systemctl --no-pager --full status surveil-trendforce-page-monitor.service || true
systemctl --no-pager --full status surveil-x-stream.service || true
echo '已安装 surveil-db-init.service，启用 iFinD 公告、Sina 个股新闻、中国财经媒体、RSS/TrendForce/海外媒体、文章日报、信号抽取/outcome/复盘/复盘日报、持仓 Web UI，并启动新浪快讯常驻服务。iFinD smoke test 可用：systemctl start surveil-ifind-smoke.service'
"
