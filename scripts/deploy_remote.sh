#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=remote_env.sh
source "$SCRIPT_DIR/remote_env.sh"
require_remote_host

SSH=(ssh -i "$REMOTE_SSH_KEY" -o IdentitiesOnly=yes "$REMOTE_USER@$REMOTE_HOST")
RSYNC_RSH="ssh -i $REMOTE_SSH_KEY -o IdentitiesOnly=yes"

echo "==> remote preflight: $REMOTE_USER@$REMOTE_HOST"
"${SSH[@]}" "set -euo pipefail
if [ -e '$REMOTE_DIR' ] && [ ! -d '$REMOTE_DIR' ]; then
  echo '$REMOTE_DIR exists but is not a directory' >&2
  exit 1
fi
id '$REMOTE_SERVICE_USER' >/dev/null 2>&1 || useradd --system --home '$REMOTE_DIR' --shell /usr/sbin/nologin '$REMOTE_SERVICE_USER'
mkdir -p '$REMOTE_DIR' '$REMOTE_DIR/logs' '$REMOTE_DIR/data'
chown -R '$REMOTE_SERVICE_USER:$REMOTE_SERVICE_USER' '$REMOTE_DIR'
python3 --version
"

echo "==> sync code"
rsync -az --delete \
  --include '.env.example' \
  --exclude '.env' \
  --exclude '.env.*' \
  --exclude 'proxy.env' \
  --exclude 'config/portfolio.json' \
  --exclude 'config/media_keywords.json' \
  --exclude '.venv' \
  --exclude '__pycache__' \
  --exclude '*.pyc' \
  --exclude 'data/' \
  --exclude 'logs/' \
  --exclude 'docs/monitoring-plan.md' \
  --exclude '.DS_Store' \
  -e "$RSYNC_RSH" \
  ./ "$REMOTE_USER@$REMOTE_HOST:$REMOTE_DIR/"

echo "==> remote venv and schema"
"${SSH[@]}" "set -euo pipefail
cd '$REMOTE_DIR'
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
if [ -f requirements.txt ]; then
  .venv/bin/python -m pip install -r requirements.txt
fi
.venv/bin/python scripts/market_db.py
chown -R '$REMOTE_SERVICE_USER:$REMOTE_SERVICE_USER' '$REMOTE_DIR'
chmod 700 '$REMOTE_DIR'
if [ -f '$REMOTE_DIR/.env' ]; then chmod 600 '$REMOTE_DIR/.env'; fi
"

echo "部署完成。下一步写入 $REMOTE_ENV 后再安装 systemd services/timers。"
