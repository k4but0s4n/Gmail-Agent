# Commands

Imperative phrases you can use for each runnable command type in **openclaw-gmail-triage**. Fill in `[brackets]` with real values.

Paths assume install under `$OPENCLAW_HOME/bin` (default `~/.openclaw/bin`). From a git checkout you can run `package/mcp/…` and `package/scripts/…` instead.

Env when needed: `set -a; source "${GMAIL_ENV_FILE:-$OPENCLAW_HOME/gmail.env}"; set +a`

Base unsub CLI: `python3 "$OPENCLAW_HOME/bin/list_unsubscribe_mcp.py"`  
Base triage CLI: `python3 "$OPENCLAW_HOME/bin/gmail_triage_ops_mcp.py"`

---

## Unsubscribe (human CLI)

| # | Command | Invocation |
|---|---|---|
| 1 | Propose unsubscribe for this message `[message_id]` `[category]` | `--propose <message_id> [CATEGORY] [--force]` |
| 2 | List pending to unsubscribe | `--pending` |
| 3 | Approve unsubscribe `[pending_id]` | `--approve <pending_id>…` **or** digest Slack button **Approve these unsubs** (allowlisted users; see INSTALL Interactivity) |
| 4 | Reject unsubscribe `[pending_id]` | `--reject <pending_id>… [--once] [--email]` |
| 5 | List suppressed senders | `--suppressed` |
| 6 | Unsuppress this sender/domain `[sender name/email/domain]` | `--unsuppress <key>` |
| 7 | List post-unsub watch | `--watch` |
| 8 | Watch this sender after unsub `[from]` | `--watch-add FROM [--grace N] [--days-ago N] [--scope email\|domain]` |
| 9 | Unwatch this sender/domain `[email-or-domain]` | `--unwatch <email-or-domain>` |

Reject defaults: suppress **domain**. Use `--email` for exact address only, or `--once` to dismiss without suppressing.

---

## Slack operator phrases (agent; draft-only)

| # | Phrase | Agent action |
|---|---|---|
| 9a | Unsub this message `[gmail_message_id]` | `list_unsubscribe__propose_unsubscribe` (category NEWSLETTER, or SPAM if said spam); reply with tool note + pending id / already_in_queue / already_unsubscribed |
| 9b | Mark this message as SPAM `[gmail_message_id]` | `gmail_triage_ops__finalize_triage` with one `{message_id, category: "SPAM"}` item; confirm label result |
| 9c | Unsub and mark as SPAM `[gmail_message_id]` | propose (SPAM) then finalize SPAM (or finalize SPAM alone — finalize queues unsub); surface propose note + label ok |

**Approve is human-only** — never from the triage agent. Prefer the digest Slack **Approve these unsubs** button (allowlisted operators via `gmail_slack_interact.py`). CLI fallback: `--approve <pending_id>…`. Digests / thread drafts still list Gmail `message_id` and pending proposal `id`.

---

## Triage / labels

| # | Command | Invocation |
|---|---|---|
| 10 | Finalize triage from this file `[path.json]` | `--finalize-json <path.json>` |
| 11 | Preview post-unsub SPAM overrides from this file `[path.json]` | `--preview-post-unsub-json <path.json>` |

JSON shape: `{ "items": [ { "message_id", "category", "from?", "subject?" }, … ] }`

---

## Index / mailbox maintenance

| # | Command | Invocation |
|---|---|---|
| 12 | Sync Gmail to index | `python3 "$OPENCLAW_HOME/bin/gmail_sync.py"` |
| 13 | Prune index older than `[days]` | `python3 "$OPENCLAW_HOME/bin/gmail_prune.py" [--keep-days N] [--dry-run]` |
| 14 | Refresh Gmail OAuth | `python3 "$OPENCLAW_HOME/bin/gmail_oauth_refresh.py"` |

---

## Scheduled / batch runners

| # | Command | Invocation |
|---|---|---|
| 15 | Run triage now | `"$OPENCLAW_HOME/bin/gmail_triage_2h.sh"` |
| 16 | Run nightly sync and prune | `"$OPENCLAW_HOME/bin/gmail_nightly.sh"` |
| 17 | Run chunked e2e triage | `"$OPENCLAW_HOME/bin/gmail_e2e_200.sh"` |
| 18 | Verify triage session `[session_key]` | `python3 "$OPENCLAW_HOME/bin/gmail_e2e_verify_batch.py" --session-key <key>` |
| 18a | Run Slack interactivity endpoint (Approve button) | `python3 "$OPENCLAW_HOME/bin/gmail_slack_interact.py"` |

---

## Feature tests

| # | Command | Invocation |
|---|---|---|
| 19 | Test post-unsub watch offline | `python3 package/scripts/test_post_unsub_watch.py` |
| 20 | Test post-unsub watch live | `cd "$OPENCLAW_HOME/bin" && python3 e2e_post_unsub_live.py` |
| 20a | Test Slack interact / batch / list_pending limit | `python3 package/scripts/test_slack_interact.py` · `python3 package/scripts/test_list_pending_limit.py` |

---

## MCP servers (via OpenClaw; agent uses these)

| # | Command | Server module |
|---|---|---|
| 21 | List recent mail | `email_query_mcp.py` |
| 22 | Finalize triage (agent) | `gmail_triage_ops_mcp.py` |
| 23 | Propose unsubscribe (agent) | `list_unsubscribe_mcp.py` |

See [INSTALL.md](./INSTALL.md) for allowlists (never put `approve_unsubscribe` on the triage agent) and [ARCHITECTURE.md](./ARCHITECTURE.md) for the data plane.
