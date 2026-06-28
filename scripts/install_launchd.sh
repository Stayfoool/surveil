#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-$(command -v python3)}"
DOMAIN="gui/$(id -u)"
LABELS=(
  "io.github.surveil.x-stream"
  "io.github.surveil.rss-monitor"
  "io.github.surveil.trendforce-page-monitor"
  "io.github.surveil.official-news-daily"
  "io.github.surveil.article-daily"
)
OLD_LABELS=(
  "io.github.surveil.monitor"
)

mkdir -p "$HOME/Library/LaunchAgents" "$ROOT/logs"

for label in "${OLD_LABELS[@]}"; do
  if launchctl print "$DOMAIN/$label" >/dev/null 2>&1; then
    launchctl bootout "$DOMAIN/$label"
  fi
done

for label in "${LABELS[@]}"; do
  plist_src="$ROOT/launchd/$label.plist"
  plist_dst="$HOME/Library/LaunchAgents/$label.plist"

  if launchctl print "$DOMAIN/$label" >/dev/null 2>&1; then
    launchctl bootout "$DOMAIN/$label"
  fi

  cp "$plist_src" "$plist_dst"
  python3 - "$plist_dst" "$ROOT" "$PYTHON_BIN" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
root = sys.argv[2]
python_bin = sys.argv[3]
text = path.read_text(encoding="utf-8")
text = text.replace("__SURVEIL_ROOT__", root).replace("__PYTHON_BIN__", python_bin)
path.write_text(text, encoding="utf-8")
PY
  chmod 644 "$plist_dst"

  launchctl bootstrap "$DOMAIN" "$plist_dst"
  launchctl enable "$DOMAIN/$label"
  launchctl kickstart -k "$DOMAIN/$label"
  echo "已安装并启动 $label"
done

echo "查看 X 状态：launchctl print $DOMAIN/io.github.surveil.x-stream"
echo "查看 RSS 状态：launchctl print $DOMAIN/io.github.surveil.rss-monitor"
echo "查看 TrendForce 页面监控状态：launchctl print $DOMAIN/io.github.surveil.trendforce-page-monitor"
echo "查看官网新闻日报状态：launchctl print $DOMAIN/io.github.surveil.official-news-daily"
echo "查看 RSS/TrendForce 文章日报状态：launchctl print $DOMAIN/io.github.surveil.article-daily"
echo "持仓监控将改为远程服务器 systemd 部署，本地旧 portfolio-monitor 草稿不会由此脚本启用。"
echo "查看日志：tail -f $ROOT/logs/x_stream.out.log $ROOT/logs/x_stream.err.log $ROOT/logs/rss_monitor.out.log $ROOT/logs/rss_monitor.err.log $ROOT/logs/trendforce_page_monitor.out.log $ROOT/logs/trendforce_page_monitor.err.log"
