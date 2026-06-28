#!/usr/bin/env bash

# Shared remote deployment defaults for helper scripts.
# Source this file from scripts that need SSH access.

REMOTE_HOST="${REMOTE_HOST:-}"
REMOTE_USER="${REMOTE_USER:-root}"
REMOTE_SSH_KEY="${REMOTE_SSH_KEY:-$HOME/.ssh/id_ed25519}"
REMOTE_DIR="${REMOTE_DIR:-/opt/surveil}"
REMOTE_ENV="${REMOTE_ENV:-$REMOTE_DIR/.env}"
REMOTE_PROXY_DIR="${REMOTE_PROXY_DIR:-/opt/surveil-proxy}"
REMOTE_SERVICE_USER="${REMOTE_SERVICE_USER:-surveil}"

require_remote_host() {
  if [ -z "${REMOTE_HOST:-}" ]; then
    echo "REMOTE_HOST is required. Example: REMOTE_HOST=your.server.example.com $0" >&2
    exit 1
  fi
}
