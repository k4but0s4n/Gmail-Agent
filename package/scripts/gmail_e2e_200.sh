#!/usr/bin/env bash
# Chunked E2E over unread, not-yet-labeled, not-yet-seen mail.
# With skip_seen=true, keep offset=0 every page.
# No --deliver; runner posts Slack only after verify succeeds.
set -euo pipefail

OPENCLAW_HOME="${OPENCLAW_HOME:-$HOME/.openclaw}"
if [[ -f "${GMAIL_ENV_FILE:-$OPENCLAW_HOME/gmail.env}" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "${GMAIL_ENV_FILE:-$OPENCLAW_HOME/gmail.env}"
  set +a
fi

export PATH="${HOME}/.npm-global/bin:/usr/bin:/bin:${PATH}"

TOTAL="${GMAIL_E2E_TOTAL:-25}"
CHUNK="${GMAIL_E2E_CHUNK:-25}"
: "${GMAIL_SLACK_CHANNEL:?GMAIL_SLACK_CHANNEL is required}"
GMAIL_CHANNEL="$GMAIL_SLACK_CHANNEL"
AGENT_ID="${GMAIL_AGENT_ID:-gmail-triage}"
OPENCLAW="${OPENCLAW_BIN_CMD:-$(command -v openclaw || echo "$HOME/.npm-global/bin/openclaw")}"
BIN_DIR="${OPENCLAW_BIN:-$OPENCLAW_HOME/bin}"
VERIFY="${GMAIL_VERIFY_SCRIPT:-$BIN_DIR/gmail_e2e_verify_batch.py}"
SECRETS="${OPENCLAW_SECRETS:-$OPENCLAW_HOME/secrets.json}"
RUN_ID="e2e-${TOTAL}c${CHUNK}-$(date +%Y%m%d-%H%M%S)"
LOG_DIR="$OPENCLAW_HOME/logs"
mkdir -p "$LOG_DIR"
MASTER_LOG="$LOG_DIR/${RUN_ID}.log"

slack_post() {
  # Prints parent message ts on success (for thread replies). Optional $3 = thread_ts.
  local channel="$1"
  local text="$2"
  local thread_ts="${3:-}"
  [[ -n "$channel" ]] || return 0
  OPENCLAW_SECRETS="$SECRETS" /usr/bin/python3 - "$channel" "$text" "$thread_ts" <<'PY' || true
import json, os, sys, urllib.request
from pathlib import Path
channel, text, thread_ts = sys.argv[1], sys.argv[2], sys.argv[3]
secrets = Path(os.environ["OPENCLAW_SECRETS"])
token = json.loads(secrets.read_text())["providers"]["slack"]["botToken"]
payload = {"channel": channel, "text": text}
if thread_ts:
    payload["thread_ts"] = thread_ts
req = urllib.request.Request(
    "https://slack.com/api/chat.postMessage",
    data=json.dumps(payload).encode(),
    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"},
    method="POST",
)
with urllib.request.urlopen(req, timeout=20) as r:
    data = json.loads(r.read().decode())
if data.get("ok"):
    print(data.get("ts") or "", end="")
    print(f"slack_ok ts={data.get('ts')}", file=sys.stderr)
else:
    print(f"slack_err {data}", file=sys.stderr)
PY
}

echo "run=$RUN_ID total=$TOTAL chunk=$CHUNK slack=$GMAIL_CHANNEL agent=$AGENT_ID unread_only+skip_labeled+skip_seen offset=0" | tee "$MASTER_LOG"
START_ALL=$(date +%s)
FAIL=0
PROCESSED=0
BATCH=0
while [[ "$PROCESSED" -lt "$TOTAL" ]]; do
  BATCH=$((BATCH + 1))
  LIMIT=$CHUNK
  REMAIN=$((TOTAL - PROCESSED))
  if [[ "$REMAIN" -lt "$LIMIT" ]]; then LIMIT=$REMAIN; fi
  OFFSET=0
  ATTEMPT=1
  MAX_ATTEMPT=2
  while [[ "$ATTEMPT" -le "$MAX_ATTEMPT" ]]; do
    if [[ "$ATTEMPT" -gt 1 ]]; then
      SESSION_KEY="${RUN_ID}-b${BATCH}-o${PROCESSED}-retry${ATTEMPT}"
      echo "[$(date --iso-8601=seconds)] RETRY attempt=$ATTEMPT session=$SESSION_KEY" | tee -a "$MASTER_LOG"
    else
      SESSION_KEY="${RUN_ID}-b${BATCH}-o${PROCESSED}"
    fi
    LOG="$LOG_DIR/${SESSION_KEY}.log"
    MSG="Chunked triage batch ${BATCH}: page offset=${OFFSET} limit=${LIMIT} (target up to ${TOTAL} eligible; processed≈${PROCESSED}).
SESSION_KEY=${SESSION_KEY}
1) Call tool_call id=email_query__list_recent ONCE with args limit=${LIMIT} offset=${OFFSET} unread_only=true skip_labeled=true skip_seen=true. NEVER echo tool_call as chat text.
2) If zero hits, stop tools (runner posts Slack).
3) Categorize EVERY hit using ONLY: URGENT, ACTION-REQUIRED, FYI, SOCIAL, NEWSLETTER, SPAM (see AGENTS.md defs).
4) Call tool_call with id=gmail_triage_ops__finalize_triage ONCE. args.items = compact list of {message_id, category} ONLY. ALWAYS include id.
5) If tool returns Validation failed, retry finalize once with id set. NEVER claim success without finalize ok:true.
6) Do NOT post to Slack — the runner posts after verify.
Do NOT call per-message label/propose tools. Do NOT approve unsubs. Do NOT draft. Do NOT fetch other offsets.
Skip bootstrap. Never fabricate. Never browser. NEVER markdown tables."

    echo "[$(date --iso-8601=seconds)] START batch=$BATCH processed=$PROCESSED limit=$LIMIT session=$SESSION_KEY" | tee -a "$MASTER_LOG"
    set +e
    timeout 900 "$OPENCLAW" agent --agent "$AGENT_ID" --session-key "$SESSION_KEY" \
      --message "$MSG" \
      --timeout 900 \
      2>&1 | tee "$LOG" | tee -a "$MASTER_LOG"
    RC=${PIPESTATUS[0]}
    set -e
    echo "[$(date --iso-8601=seconds)] END batch=$BATCH rc=$RC" | tee -a "$MASTER_LOG"

    set +e
    VERIFY_OUT=$(python3 "$VERIFY" --session-key "$SESSION_KEY" 2>&1)
    VRC=$?
    set -e
    echo "$VERIFY_OUT" | tee -a "$MASTER_LOG"
    if [[ "$VRC" -eq 0 ]]; then
      echo "[$(date --iso-8601=seconds)] VERIFY OK batch=$BATCH" | tee -a "$MASTER_LOG"
      SLACK_TEXT=$(printf '%s' "$VERIFY_OUT" | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d.get("slack_text") or "")' 2>/dev/null || true)
      UNSUB_DRAFT=$(printf '%s' "$VERIFY_OUT" | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d.get("unsub_draft_text") or "")' 2>/dev/null || true)
      if [[ -n "$SLACK_TEXT" ]]; then
        PARENT_TS=$(slack_post "$GMAIL_CHANNEL" "$SLACK_TEXT")
        if [[ -n "$PARENT_TS" && -n "$UNSUB_DRAFT" ]]; then
          slack_post "$GMAIL_CHANNEL" "$UNSUB_DRAFT" "$PARENT_TS" >/dev/null || true
        fi
      fi
      break
    fi
    echo "[$(date --iso-8601=seconds)] VERIFY FAIL batch=$BATCH attempt=$ATTEMPT" | tee -a "$MASTER_LOG"
    if [[ "$ATTEMPT" -ge "$MAX_ATTEMPT" ]]; then
      FAIL=1
      break
    fi
    ATTEMPT=$((ATTEMPT + 1))
  done

  EMPTY=$(printf '%s' "${VERIFY_OUT:-}" | python3 -c 'import sys,json; d=json.load(sys.stdin); print("1" if d.get("ok") and int(d.get("listed_count") or 0)==0 else "0")' 2>/dev/null || echo 0)
  if [[ "$EMPTY" == "1" ]]; then
    echo "[$(date --iso-8601=seconds)] STOP early — no more eligible mail" | tee -a "$MASTER_LOG"
    break
  fi
  PROCESSED=$((PROCESSED + LIMIT))
done

echo "=== RESULT fail=$FAIL elapsed=$(( $(date +%s) - START_ALL ))s log=$MASTER_LOG ===" | tee -a "$MASTER_LOG"
exit "$FAIL"
