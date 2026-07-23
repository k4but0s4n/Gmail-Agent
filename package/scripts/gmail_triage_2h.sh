#!/usr/bin/env bash
# Recurring triage: sync recent mail, then process unread from today in pages of ≤25.
# With skip_seen=true, keep offset=0 every page (seen shrinks the eligible set).
# Agent runs without --deliver; runner posts Slack only after verify succeeds.
set -euo pipefail

OPENCLAW_HOME="${OPENCLAW_HOME:-$HOME/.openclaw}"
# Optional: source env file if present (cron-friendly)
if [[ -f "${GMAIL_ENV_FILE:-$OPENCLAW_HOME/gmail.env}" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "${GMAIL_ENV_FILE:-$OPENCLAW_HOME/gmail.env}"
  set +a
fi

export PATH="${HOME}/.npm-global/bin:/usr/bin:/bin:${PATH}"

TOTAL="${GMAIL_TRIAGE_TOTAL:-50}"
CHUNK="${GMAIL_TRIAGE_CHUNK:-25}"
: "${GMAIL_SLACK_CHANNEL:?GMAIL_SLACK_CHANNEL is required}"
: "${CHROMA_URL:?CHROMA_URL is required}"
: "${GMAIL_EMBED_URL:?GMAIL_EMBED_URL is required}"
: "${GMAIL_RETRIEVE_URL:?GMAIL_RETRIEVE_URL is required}"
GMAIL_CHANNEL="$GMAIL_SLACK_CHANNEL"
ALERT_CHANNEL="${GMAIL_ALERT_SLACK_CHANNEL:-}"
AGENT_ID="${GMAIL_AGENT_ID:-gmail-triage}"
OPENCLAW="${OPENCLAW_BIN_CMD:-$(command -v openclaw || echo "$HOME/.npm-global/bin/openclaw")}"
BIN_DIR="${OPENCLAW_BIN:-$OPENCLAW_HOME/bin}"
VERIFY="${GMAIL_VERIFY_SCRIPT:-$BIN_DIR/gmail_e2e_verify_batch.py}"
SYNC="${GMAIL_SYNC_SCRIPT:-$BIN_DIR/gmail_sync.py}"
SLACK_POST="${GMAIL_SLACK_POST_SCRIPT:-$BIN_DIR/gmail_slack_post.py}"
LOG_DIR="$OPENCLAW_HOME/logs"
LOCK_FILE="${GMAIL_TRIAGE_LOCK:-$OPENCLAW_HOME/run/gmail_triage_2h.lock}"
LOG_FILE="$LOG_DIR/gmail_triage_2h.log"
SECRETS="${OPENCLAW_SECRETS:-$OPENCLAW_HOME/secrets.json}"

export GMAIL_SYNC_LOOKBACK_DAYS="${GMAIL_SYNC_LOOKBACK_DAYS:-2}"
export GMAIL_SYNC_MAX_EMAILS="${GMAIL_SYNC_MAX_EMAILS:-80}"

mkdir -p "$LOG_DIR" "$(dirname "$LOCK_FILE")"
chmod 700 "$(dirname "$LOCK_FILE")" 2>/dev/null || true

slack_post() {
  # Prints parent message ts on success (for thread replies). Optional $3 = thread_ts.
  local channel="$1"
  local text="$2"
  local thread_ts="${3:-}"
  [[ -n "$channel" ]] || return 0
  OPENCLAW_SECRETS="$SECRETS" /usr/bin/python3 "$SLACK_POST" \
    --channel "$channel" --text "$text" ${thread_ts:+--thread-ts "$thread_ts"} \
    2>>"$LOG_FILE" || true
}

slack_post_digest() {
  # Digest + optional Approve button + CLI draft thread. Prints parent ts.
  local channel="$1"
  local text="$2"
  local unsub_draft="$3"
  local unsub_ids="$4"
  local session_key="$5"
  [[ -n "$channel" ]] || return 0
  OPENCLAW_SECRETS="$SECRETS" /usr/bin/python3 "$SLACK_POST" \
    --digest --channel "$channel" --text "$text" \
    --unsub-draft "$unsub_draft" --unsub-ids "$unsub_ids" \
    --session-key "$session_key" \
    2>>"$LOG_FILE" || true
}

notify_fail() {
  local detail="$1"
  local msg="gmail triage-2h FAILED: ${detail}. See ${LOG_FILE}"
  echo "[$(date --iso-8601=seconds)] FAIL $detail" >>"$LOG_FILE"
  slack_post "$GMAIL_CHANNEL" "$msg"
  slack_post "$ALERT_CHANNEL" "$msg"
}

# Separate lock-busy from work failure (flock exits 1 when lock held).
if ! /usr/bin/flock -n "$LOCK_FILE" true 2>/dev/null; then
  echo "[$(date --iso-8601=seconds)] skip — lock busy" | tee -a "$LOG_FILE"
  exit 0
fi

INNER_RC=0
/usr/bin/flock -n "$LOCK_FILE" /bin/bash -s <<EOF || INNER_RC=$?
set -euo pipefail
RUN_ID="triage2h-\$(date +%Y%m%d-%H%M%S)"
TOTAL="$TOTAL"
CHUNK="$CHUNK"
GMAIL_CHANNEL="$GMAIL_CHANNEL"
AGENT_ID="$AGENT_ID"
OPENCLAW="$OPENCLAW"
VERIFY="$VERIFY"
SYNC="$SYNC"
SLACK_POST="$SLACK_POST"
LOG_DIR="$LOG_DIR"
LOG_FILE="$LOG_FILE"
SECRETS="$SECRETS"

slack_post() {
  local channel="\$1"
  local text="\$2"
  local thread_ts="\${3:-}"
  [[ -n "\$channel" ]] || return 0
  OPENCLAW_SECRETS="\$SECRETS" /usr/bin/python3 "\$SLACK_POST" \\
    --channel "\$channel" --text "\$text" \${thread_ts:+--thread-ts "\$thread_ts"} \\
    2>>"\$LOG_FILE" || true
}

slack_post_digest() {
  local channel="\$1"
  local text="\$2"
  local unsub_draft="\$3"
  local unsub_ids="\$4"
  local session_key="\$5"
  [[ -n "\$channel" ]] || return 0
  OPENCLAW_SECRETS="\$SECRETS" /usr/bin/python3 "\$SLACK_POST" \\
    --digest --channel "\$channel" --text "\$text" \\
    --unsub-draft "\$unsub_draft" --unsub-ids "\$unsub_ids" \\
    --session-key "\$session_key" \\
    2>>"\$LOG_FILE" || true
}

run_batch() {
  local SESSION_KEY="\$1"
  local LIMIT="\$2"
  local OFFSET="\$3"
  local BATCH="\$4"
  local PROCESSED="\$5"
  local LOG="\$LOG_DIR/\${SESSION_KEY}.log"
  local MSG RC VERIFY_OUT VRC
  MSG="Recurring triage batch \${BATCH}: unread since today (America/New_York), page offset=\${OFFSET} limit=\${LIMIT} (cap \${TOTAL}, processed≈\${PROCESSED}).
SESSION_KEY=\${SESSION_KEY}
1) Call tool_call id=email_query__list_recent ONCE with args limit=\${LIMIT} offset=\${OFFSET} unread_only=true skip_labeled=true skip_seen=true since_today=true. NEVER echo tool_call as chat text — invoke the meta-tool only.
2) If zero hits, stop tools (runner posts Slack).
3) Categorize EVERY hit using ONLY: URGENT, ACTION-REQUIRED, FYI, SOCIAL, NEWSLETTER, SPAM (AGENTS.md defs).
4) Call tool_call id=gmail_triage_ops__finalize_triage ONCE with args.items={message_id,category} only. ALWAYS include id. If Validation failed, retry once.
5) Do NOT post to Slack yourself — the runner posts after verify. Do NOT approve unsubs. Do NOT draft. Do NOT fetch other offsets. Skip bootstrap. Never fabricate. Never browser. NEVER markdown tables."

  echo "[\$(date --iso-8601=seconds)] START batch=\$BATCH processed=\$PROCESSED limit=\$LIMIT session=\$SESSION_KEY"
  set +e
  # No --deliver: prevents leaked tool_call YAML from posting to Slack.
  timeout 900 "\$OPENCLAW" agent --agent "\$AGENT_ID" --session-key "\$SESSION_KEY" \\
    --message "\$MSG" \\
    --timeout 900 \\
    2>&1 | tee "\$LOG"
  RC=\${PIPESTATUS[0]}
  set -e
  echo "[\$(date --iso-8601=seconds)] END batch=\$BATCH rc=\$RC session=\$SESSION_KEY"

  set +e
  VERIFY_OUT=\$(python3 "\$VERIFY" --session-key "\$SESSION_KEY" 2>&1)
  VRC=\$?
  set -e
  echo "\$VERIFY_OUT"
  # Export for caller via globals
  LAST_RC=\$RC
  LAST_VRC=\$VRC
  LAST_VERIFY_OUT=\$VERIFY_OUT
  return 0
}

{
  echo "[\$(date --iso-8601=seconds)] start run=\$RUN_ID total=\$TOTAL chunk=\$CHUNK lookback=\$GMAIL_SYNC_LOOKBACK_DAYS agent=\$AGENT_ID"
  /usr/bin/python3 "\$SYNC"
  echo "[\$(date --iso-8601=seconds)] sync ok; starting triage pages (since_today, offset=0 each page)"

  FAIL=0
  PROCESSED=0
  BATCH=0
  while [[ "\$PROCESSED" -lt "\$TOTAL" ]]; do
    BATCH=\$((BATCH + 1))
    LIMIT=\$CHUNK
    REMAIN=\$((TOTAL - PROCESSED))
    if [[ "\$REMAIN" -lt "\$LIMIT" ]]; then LIMIT=\$REMAIN; fi
    OFFSET=0
    SESSION_KEY="\${RUN_ID}-b\${BATCH}-o\${PROCESSED}"
    ATTEMPT=1
    MAX_ATTEMPT=2
    while [[ "\$ATTEMPT" -le "\$MAX_ATTEMPT" ]]; do
      if [[ "\$ATTEMPT" -gt 1 ]]; then
        SESSION_KEY="\${RUN_ID}-b\${BATCH}-o\${PROCESSED}-retry\${ATTEMPT}"
        echo "[\$(date --iso-8601=seconds)] RETRY attempt=\$ATTEMPT session=\$SESSION_KEY"
      fi
      run_batch "\$SESSION_KEY" "\$LIMIT" "\$OFFSET" "\$BATCH" "\$PROCESSED"
      if [[ "\$LAST_VRC" -eq 0 ]]; then
        break
      fi
      if [[ "\$ATTEMPT" -ge "\$MAX_ATTEMPT" ]]; then
        break
      fi
      ATTEMPT=\$((ATTEMPT + 1))
    done

    if [[ "\$LAST_RC" -ne 0 || "\$LAST_VRC" -ne 0 ]]; then
      FAIL=1
    else
      # Post digest only after verify ok (never raw tool_call text)
      SLACK_TEXT=\$(printf '%s' "\$LAST_VERIFY_OUT" | /usr/bin/python3 -c 'import sys,json; d=json.load(sys.stdin); print(d.get("slack_text") or "")' 2>/dev/null || true)
      UNSUB_DRAFT=\$(printf '%s' "\$LAST_VERIFY_OUT" | /usr/bin/python3 -c 'import sys,json; d=json.load(sys.stdin); print(d.get("unsub_draft_text") or "")' 2>/dev/null || true)
      UNSUB_IDS=\$(printf '%s' "\$LAST_VERIFY_OUT" | /usr/bin/python3 -c 'import sys,json; d=json.load(sys.stdin); print(",".join(d.get("unsub_pending_ids") or []))' 2>/dev/null || true)
      if [[ -n "\$SLACK_TEXT" ]]; then
        slack_post_digest "\$GMAIL_CHANNEL" "\$SLACK_TEXT" "\$UNSUB_DRAFT" "\$UNSUB_IDS" "\$SESSION_KEY" >/dev/null || true
      fi
    fi

    EMPTY=\$(printf '%s' "\$LAST_VERIFY_OUT" | /usr/bin/python3 -c 'import sys,json; d=json.load(sys.stdin); print("1" if d.get("ok") and int(d.get("listed_count") or 0)==0 else "0")' 2>/dev/null || echo 0)
    if [[ "\$EMPTY" == "1" ]]; then
      echo "[\$(date --iso-8601=seconds)] STOP — no more eligible mail today"
      break
    fi
    if [[ "\$LAST_VRC" -eq 0 ]] && grep -qE "eligible_total=0|Listed 0 email|No emails matched filters" "\$LOG_DIR/\${SESSION_KEY}.log" 2>/dev/null; then
      echo "[\$(date --iso-8601=seconds)] STOP — no more eligible mail today"
      break
    fi
    PROCESSED=\$((PROCESSED + LIMIT))
  done

  echo "[\$(date --iso-8601=seconds)] done fail=\$FAIL run=\$RUN_ID"
  exit "\$FAIL"
} >>"\$LOG_FILE" 2>&1
EOF

if [[ "$INNER_RC" -ne 0 ]]; then
  notify_fail "exit $INNER_RC"
  exit "$INNER_RC"
fi
