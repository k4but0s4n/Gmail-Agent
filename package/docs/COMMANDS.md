# Commands

Natural-language inventory of what you can run in **openclaw-gmail-triage**. One entry per command type. Paths assume files are installed under `$OPENCLAW_HOME/bin` (default `~/.openclaw/bin`); from a git checkout you can also run `package/mcp/…` and `package/scripts/…` directly.

Set env first when needed: `set -a; source "${GMAIL_ENV_FILE:-$OPENCLAW_HOME/gmail.env}"; set +a`

---

## Unsubscribe (human CLI)

`python3 "$OPENCLAW_HOME/bin/list_unsubscribe_mcp.py" …`

| # | What it does | Invocation |
|---|---|---|
| 1 | **Propose** an unsubscribe for a Gmail message (queue only) | `--propose <message_id> [CATEGORY] [--force]` |
| 2 | **List pending** unsubscribe proposals waiting for you | `--pending` |
| 3 | **Approve** pending unsub(s) and execute them | `--approve <pending_id>…` |
| 4 | **Reject** pending unsub(s) (optionally suppress sender) | `--reject <pending_id>… [--once] [--email]` |
| 5 | **List suppressed** senders/domains that won’t be auto-proposed | `--suppressed` |
| 6 | **Unsuppress** a sender/domain so it can be proposed again | `--unsuppress <key>` |
| 7 | **List post-unsub watch** (senders watched after a successful approve) | `--watch` |
| 8 | **Add** someone to the post-unsub watch (ops / e2e seed) | `--watch-add FROM [--grace N] [--days-ago N] [--scope email\|domain]` |
| 9 | **Remove** someone from the post-unsub watch | `--unwatch <email-or-domain>` |

Reject defaults: suppress **domain**, unless `--email` (exact address) or `--once` (dismiss this proposal only, no suppress).

---

## Triage / labels

`python3 "$OPENCLAW_HOME/bin/gmail_triage_ops_mcp.py" …`

| # | What it does | Invocation |
|---|---|---|
| 10 | **Finalize triage** from a JSON file (labels, mark-read, unsub queue) | `--finalize-json <path.json>` |
| 11 | **Preview post-unsub overrides** (dry-run SPAM reclass; no Gmail mutate) | `--preview-post-unsub-json <path.json>` |

JSON shape: `{ "items": [ { "message_id", "category", "from?", "subject?" }, … ] }`

---

## Index / mailbox maintenance

| # | What it does | Invocation |
|---|---|---|
| 12 | **Sync** recent Gmail into the Chroma index | `python3 "$OPENCLAW_HOME/bin/gmail_sync.py"` |
| 13 | **Prune** old docs from the index | `python3 "$OPENCLAW_HOME/bin/gmail_prune.py" [--keep-days N] [--dry-run]` |
| 14 | **Refresh Gmail OAuth** tokens (warn if expiry is near) | `python3 "$OPENCLAW_HOME/bin/gmail_oauth_refresh.py"` |

---

## Scheduled / batch runners

| # | What it does | Invocation |
|---|---|---|
| 15 | **Run the every-2h triage job** (sync + agent pages + Slack + verify) | `"$OPENCLAW_HOME/bin/gmail_triage_2h.sh"` |
| 16 | **Run the nightly job** (sync + prune only) | `"$OPENCLAW_HOME/bin/gmail_nightly.sh"` |
| 17 | **Run the chunked e2e triage harness** | `"$OPENCLAW_HOME/bin/gmail_e2e_200.sh"` |
| 18 | **Verify / recover** a triage session (fix orphan finalize if needed) | `python3 "$OPENCLAW_HOME/bin/gmail_e2e_verify_batch.py" --session-key <key>` |

---

## Feature tests

| # | What it does | Invocation |
|---|---|---|
| 19 | **Offline post-unsub watch test** (no Gmail / network) | `python3 package/scripts/test_post_unsub_watch.py` |
| 20 | **Live post-unsub e2e** on an OpenClaw host (can label real mail) | `cd "$OPENCLAW_HOME/bin" && python3 e2e_post_unsub_live.py` |

---

## MCP servers (usually via OpenClaw, not typed by hand)

These run as NDJSON MCP processes registered with the gateway. The triage agent calls them; you normally do not start them yourself except for debugging.

| # | What it does | Server module |
|---|---|---|
| 21 | **Email query / list recent** — list indexed mail for the agent | `email_query_mcp.py` |
| 22 | **Triage ops** — agent calls `finalize_triage` | `gmail_triage_ops_mcp.py` |
| 23 | **List-unsubscribe** — agent proposes; humans approve via CLI above | `list_unsubscribe_mcp.py` |

See [INSTALL.md](./INSTALL.md) for allowlists (never put `approve_unsubscribe` on the triage agent) and [ARCHITECTURE.md](./ARCHITECTURE.md) for the data plane.
