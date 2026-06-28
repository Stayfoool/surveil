#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=remote_env.sh
source "$SCRIPT_DIR/remote_env.sh"
require_remote_host

echo "请粘贴 iFinD Super Command 生成的 THS_RPT(...)，或包含 formData:{...} 的 HTTP 命令。"
echo "粘贴完成后按 Ctrl-D 结束输入。"
RAW_INPUT="$(cat)"

PAYLOAD_FILE="$(mktemp)"
REMOTE_PAYLOAD="/tmp/surveil-ifind-research-$$.json"
cleanup() {
  rm -f "$PAYLOAD_FILE"
}
trap cleanup EXIT

PAYLOAD_FILE="$PAYLOAD_FILE" RAW_INPUT="$RAW_INPUT" python3 - <<'PY'
from pathlib import Path
import json
import os
import re

raw = os.environ["RAW_INPUT"].strip()
if not raw:
    raise SystemExit("未读取到输入")

formula = ""
reportname = ""
functionpara = {}
outputpara = ""

match = re.search(r"formData:\s*(\{.*\})\s*$", raw, flags=re.S)
if match:
    payload = json.loads(match.group(1))
    formula = str(payload.get("formula") or "").strip()
    reportname = str(payload.get("reportname") or "").strip()
    functionpara = payload.get("functionpara") if isinstance(payload.get("functionpara"), dict) else {}
    outputpara = str(payload.get("outputpara") or "").strip()
else:
    formula = raw

if formula and "THS_RPT" not in formula and "THS_DR" not in formula:
    raise SystemExit("输入里没有识别到 THS_RPT 或 THS_DR，请确认复制的是专题报表/机构研究命令")
if not formula and not reportname:
    raise SystemExit("没有识别到 formula 或 reportname/functionpara")

updates = {}
if formula:
    updates["IFIND_RESEARCH_FORMULA"] = formula
    updates["IFIND_RESEARCH_REPORTNAME"] = ""
    updates["IFIND_RESEARCH_FUNCTIONPARA"] = ""
else:
    updates["IFIND_RESEARCH_REPORTNAME"] = reportname
    updates["IFIND_RESEARCH_FUNCTIONPARA"] = json.dumps(functionpara, ensure_ascii=False, separators=(",", ":"))
    updates["IFIND_RESEARCH_FORMULA"] = ""
if outputpara:
    updates["IFIND_RESEARCH_OUTPUTPARA"] = outputpara

path = Path(os.environ["PAYLOAD_FILE"])
path.write_text(json.dumps(updates, ensure_ascii=False), encoding="utf-8")
path.chmod(0o600)
PY

scp -i "$REMOTE_SSH_KEY" -o IdentitiesOnly=yes "$PAYLOAD_FILE" "$REMOTE_USER@$REMOTE_HOST:$REMOTE_PAYLOAD" >/dev/null

ssh -i "$REMOTE_SSH_KEY" -o IdentitiesOnly=yes "$REMOTE_USER@$REMOTE_HOST" \
  "REMOTE_ENV='$REMOTE_ENV' REMOTE_PAYLOAD='$REMOTE_PAYLOAD' REMOTE_SERVICE_USER='$REMOTE_SERVICE_USER' python3 - <<'PY'
from pathlib import Path
import json
import os
import pwd
import grp

env_path = Path(os.environ['REMOTE_ENV'])
env_path.parent.mkdir(parents=True, exist_ok=True)
payload_path = Path(os.environ['REMOTE_PAYLOAD'])
updates = json.loads(payload_path.read_text(encoding='utf-8'))

lines = env_path.read_text(encoding='utf-8').splitlines() if env_path.exists() else []
seen = set()
out = []
for line in lines:
    stripped = line.strip()
    key = stripped.split('=', 1)[0] if '=' in stripped and not stripped.startswith('#') else ''
    if key in updates:
        out.append(f'{key}={updates[key]}')
        seen.add(key)
    else:
        out.append(line)
for key, value in updates.items():
    if key not in seen:
        out.append(f'{key}={value}')
env_path.write_text('\\n'.join(out) + '\\n', encoding='utf-8')
env_path.chmod(0o600)
service_user = os.environ.get('REMOTE_SERVICE_USER') or 'surveil'
try:
    uid = pwd.getpwnam(service_user).pw_uid
    gid = grp.getgrnam(service_user).gr_gid
    os.chown(env_path, uid, gid)
except KeyError:
    pass
payload_path.unlink(missing_ok=True)
written = ', '.join(k for k, v in updates.items() if v)
print(f'已更新 {env_path}: {written}')
PY"
