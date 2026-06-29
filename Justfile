set shell := ["bash", "-eu", "-o", "pipefail", "-c"]

test:
    python3 -m py_compile scripts/*.py
    python3 scripts/test_analysis.py
    python3 scripts/test_llm_analysis.py
    python3 scripts/test_llm_json_recovery.py
    python3 scripts/test_trendforce_page_monitor.py
    python3 scripts/test_link_enrichment.py
    python3 scripts/test_sina_stock_news.py
    python3 scripts/scan_secrets.py

status:
    python3 scripts/status_sync.py

status-strict:
    python3 scripts/status_sync.py --strict

deploy:
    ./scripts/deploy_remote.sh
    ./scripts/install_remote_systemd.sh

remote-timers:
    ssh -i "$REMOTE_SSH_KEY" -o IdentitiesOnly=yes "$REMOTE_USER@$REMOTE_HOST" 'systemctl list-timers --all "surveil-*" --no-pager'

remote-revision:
    ssh -i "$REMOTE_SSH_KEY" -o IdentitiesOnly=yes "$REMOTE_USER@$REMOTE_HOST" "cat '${REMOTE_DIR:-/opt/surveil}/REVISION'"
