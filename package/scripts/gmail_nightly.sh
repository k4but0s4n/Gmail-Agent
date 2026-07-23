#!/usr/bin/env bash
# Nightly Gmail sync + prune. Triage is handled by gmail_triage_2h.sh (scheduled digests).
set -euo pipefail

OPENCLAW_HOME="${OPENCLAW_HOME:-$HOME/.openclaw}"
if [[ -f "${GMAIL_ENV_FILE:-$OPENCLAW_HOME/gmail.env}" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "${GMAIL_ENV_FILE:-$OPENCLAW_HOME/gmail.env}"
  set +a
fi

export PATH="${HOME}/.npm-global/bin:/usr/bin:/bin:${PATH}"

LOG_DIR="$OPENCLAW_HOME/logs"
LOG_FILE="$LOG_DIR/gmail_nightly.log"
LOCK_FILE="${GMAIL_NIGHTLY_LOCK:-$OPENCLAW_HOME/run/gmail_nightly.lock}"
BIN_DIR="${OPENCLAW_BIN:-$OPENCLAW_HOME/bin}"
SYNC="${GMAIL_SYNC_SCRIPT:-$BIN_DIR/gmail_sync.py}"
PRUNE="${GMAIL_PRUNE_SCRIPT:-$BIN_DIR/gmail_prune.py}"
export GMAIL_PRUNE_KEEP_DAYS="${GMAIL_PRUNE_KEEP_DAYS:-90}"
GMAIL_CHANNEL="${GMAIL_SLACK_CHANNEL:-}"
ALERT_CHANNEL="${GMAIL_ALERT_SLACK_CHANNEL:-}"
SECRETS="${OPENCLAW_SECRETS:-$OPENCLAW_HOME/secrets.json}"

export GMAIL_SYNC_LOOKBACK_DAYS="${GMAIL_SYNC_LOOKBACK_DAYS:-30}"
export GMAIL_SYNC_MAX_EMAILS="${GMAIL_SYNC_MAX_EMAILS:-50}"

mkdir -p "$LOG_DIR" "$(dirname "$LOCK_FILE")"
chmod 700 "$(dirname "$LOCK_FILE")" 2>/dev/null || true

slack_post() {
  local channel="$1"
  local text="$2"
  [[ -n "$channel" ]] || return 0
  OPENCLAW_SECRETS="$SECRETS" /usr/bin/python3 - "$channel" "$text" <<'PY' >>"$LOG_FILE" 2>&1 || true
import json, os, sys, urllib.request
from pathlib import Path
channel, text = sys.argv[1], sys.argv[2]
secrets = Path(os.environ["OPENCLAW_SECRETS"])
token = json.loads(secrets.read_text())["providers"]["slack"]["botToken"]
req = urllib.request.Request(
    "https://slack.com/api/chat.postMessage",
    data=json.dumps({"channel": channel, "text": text}).encode(),
    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"},
    method="POST",
)
with urllib.request.urlopen(req, timeout=20) as r:
    data = json.loads(r.read().decode())
print("slack_ok" if data.get("ok") else f"slack_err {data}")
PY
}

notify_fail() {
  local stage="$1"
  local detail="$2"
  local msg="gmail nightly FAILED at ${stage}: ${detail}. See ${LOG_FILE}"
  echo "[$(date --iso-8601=seconds)] FAIL stage=$stage detail=$detail" >>"$LOG_FILE"
  slack_post "$GMAIL_CHANNEL" "$msg"
  slack_post "$ALERT_CHANNEL" "$msg"
}

if ! /usr/bin/flock -n "$LOCK_FILE" true 2>/dev/null; then
  echo "[$(date --iso-8601=seconds)] skip — lock busy" | tee -a "$LOG_FILE"
  exit 0
fi

INNER_RC=0
/usr/bin/flock -n "$LOCK_FILE" /bin/bash -s <<EOF || INNER_RC=$?
set -euo pipefail
{
  echo "[\$(date --iso-8601=seconds)] starting gmail nightly sync+prune lookback=$GMAIL_SYNC_LOOKBACK_DAYS max=$GMAIL_SYNC_MAX_EMAILS"
  /usr/bin/python3 "$SYNC"
  echo "[\$(date --iso-8601=seconds)] sync ok; pruning older than ${GMAIL_PRUNE_KEEP_DAYS:-90}d"
  /usr/bin/python3 "$PRUNE" --keep-days "${GMAIL_PRUNE_KEEP_DAYS:-90}"
  echo "[\$(date --iso-8601=seconds)] gmail nightly sync+prune completed (triage via gmail_triage_2h)"
} >>"$LOG_FILE" 2>&1
EOF

if [[ "$INNER_RC" -ne 0 ]]; then
  notify_fail run "exit $INNER_RC"
  exit "$INNER_RC"
fi
