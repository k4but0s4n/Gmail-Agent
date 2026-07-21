#!/usr/bin/env bash
# Chunked E2E over unread, not-yet-labeled, not-yet-seen mail.
# With skip_seen=true, keep offset=0 every page.
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
RUN_ID="e2e-${TOTAL}c${CHUNK}-$(date +%Y%m%d-%H%M%S)"
LOG_DIR="$OPENCLAW_HOME/logs"
mkdir -p "$LOG_DIR"
MASTER_LOG="$LOG_DIR/${RUN_ID}.log"

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
  SESSION_KEY="${RUN_ID}-b${BATCH}-o${PROCESSED}"
  LOG="$LOG_DIR/${SESSION_KEY}.log"
  MSG="Chunked triage batch ${BATCH}: page offset=${OFFSET} limit=${LIMIT} (target up to ${TOTAL} eligible; processed≈${PROCESSED}).
SESSION_KEY=${SESSION_KEY}
1) Call tool_call id=email_query__list_recent ONCE with args limit=${LIMIT} offset=${OFFSET} unread_only=true skip_labeled=true skip_seen=true.
2) If zero hits, Slack one line including session:\`${SESSION_KEY}\` and stop tools.
3) Categorize EVERY hit using ONLY: URGENT, ACTION-REQUIRED, FYI, SOCIAL, NEWSLETTER, SPAM (see AGENTS.md defs).
4) Call tool_call with id=gmail_triage_ops__finalize_triage ONCE. args.items = compact list of {message_id, category} ONLY (omit from/subject). ALWAYS include id.
5) If tool returns Validation failed, retry finalize once with id set. NEVER claim success without finalize ok:true.
6) Slack MUST follow AGENTS.md layout (KEEP SHORT) using finalize counts:
   - First line scope + counts for ALL categories
   - Next line exactly: session: \`${SESSION_KEY}\`
   - Digest bullets ONLY for ACTION-REQUIRED (and URGENT if any) + NEWSLETTER
   - Do NOT list FYI/SOCIAL/SPAM in Slack (still finalize/label them; SOCIAL marked read)
   - Every shown bullet includes \`message_id\`
   - Mention marked_read / unsub_queued from finalize
Do NOT call per-message label/propose tools. Do NOT approve unsubs. Do NOT draft. Do NOT fetch other offsets.
Skip bootstrap. Never fabricate. Never browser. NEVER markdown tables."

  echo "[$(date --iso-8601=seconds)] START batch=$BATCH processed=$PROCESSED limit=$LIMIT session=$SESSION_KEY" | tee -a "$MASTER_LOG"
  set +e
  timeout 900 "$OPENCLAW" agent --agent "$AGENT_ID" --session-key "$SESSION_KEY" \
    --message "$MSG" \
    --timeout 900 \
    --deliver --channel slack --reply-channel slack --reply-to "$GMAIL_CHANNEL" \
    2>&1 | tee "$LOG" | tee -a "$MASTER_LOG"
  RC=${PIPESTATUS[0]}
  set -e
  echo "[$(date --iso-8601=seconds)] END batch=$BATCH rc=$RC" | tee -a "$MASTER_LOG"
  if [[ "$RC" -ne 0 ]]; then FAIL=1; fi

  set +e
  VERIFY_OUT=$(python3 "$VERIFY" --session-key "$SESSION_KEY" 2>&1)
  VRC=$?
  set -e
  echo "$VERIFY_OUT" | tee -a "$MASTER_LOG"
  if [[ "$VRC" -ne 0 ]]; then
    echo "[$(date --iso-8601=seconds)] VERIFY FAIL batch=$BATCH" | tee -a "$MASTER_LOG"
    FAIL=1
  else
    echo "[$(date --iso-8601=seconds)] VERIFY OK batch=$BATCH" | tee -a "$MASTER_LOG"
  fi

  if echo "$VERIFY_OUT" | grep -q '"listed_count": 0'; then
    echo "[$(date --iso-8601=seconds)] STOP early — no more eligible mail" | tee -a "$MASTER_LOG"
    break
  fi
  if grep -qE "eligible_total=0|No emails matched filters|page is empty|Listed 0 email" "$LOG" 2>/dev/null; then
    echo "[$(date --iso-8601=seconds)] STOP early — no more eligible mail" | tee -a "$MASTER_LOG"
    break
  fi
  PROCESSED=$((PROCESSED + LIMIT))
done

echo "=== RESULT fail=$FAIL elapsed=$(( $(date +%s) - START_ALL ))s log=$MASTER_LOG ===" | tee -a "$MASTER_LOG"
exit "$FAIL"
