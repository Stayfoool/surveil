#!/usr/bin/env bash
set -euo pipefail

DOMAIN="gui/$(id -u)"
LABELS=(
  "io.github.surveil.x-stream"
  "io.github.surveil.rss-monitor"
  "io.github.surveil.trendforce-page-monitor"
  "io.github.surveil.official-news-daily"
  "io.github.surveil.article-daily"
  "io.github.surveil.portfolio-monitor"
  "io.github.surveil.monitor"
)

for label in "${LABELS[@]}"; do
  if launchctl print "$DOMAIN/$label" >/dev/null 2>&1; then
    launchctl bootout "$DOMAIN/$label"
  fi

  rm -f "$HOME/Library/LaunchAgents/$label.plist"
  echo "已卸载 $label"
done
