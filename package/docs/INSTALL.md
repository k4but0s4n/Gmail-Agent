# Install — blank OpenClaw host

Get **openclaw-gmail-triage** running on a fresh machine: env → OAuth → MCP → agent → cron → smoke.

## Prerequisites

- OpenClaw gateway + CLI (`openclaw` on `PATH`, often `$HOME/.npm-global/bin`)
- Python **3.10+** preferred (3.9 works if modules use `from __future__ import annotations`; package does)
- Gmail OAuth via `@gongrzhe/server-gmail-autoauth-mcp` (or equivalent)
- Chroma + OpenAI-compatible embedder + retrieve API (v1 requires RAG; URLs may be LAN or localhost)
- Slack bot token in `$OPENCLAW_HOME/secrets.json` → `providers.slack.botToken`
- LLM with reliable tool calling (Gemma: keep pages ≤25 and compact finalize)

## 1. Copy package files

```bash
export OPENCLAW_HOME="${OPENCLAW_HOME:-$HOME/.openclaw}"
mkdir -p "$OPENCLAW_HOME/bin" "$OPENCLAW_HOME/gmail" "$OPENCLAW_HOME/logs" "$OPENCLAW_HOME/run"

# from this repo
cp package/mcp/*.py "$OPENCLAW_HOME/bin/"
cp package/scripts/gmail_triage_2h.sh package/scripts/gmail_nightly.sh \
  package/scripts/gmail_e2e_200.sh "$OPENCLAW_HOME/bin/"
chmod +x "$OPENCLAW_HOME/bin/"*.sh
chmod 700 "$OPENCLAW_HOME/gmail" "$OPENCLAW_HOME/run"
```

`_config.py` must sit next to the other Python modules in `$OPENCLAW_HOME/bin/`.

## 2. Env file

```bash
cp package/.env.example "$OPENCLAW_HOME/gmail.env"
# edit: CHROMA_URL, GMAIL_EMBED_URL, GMAIL_RETRIEVE_URL, Slack channels, agent id
set -a; source "$OPENCLAW_HOME/gmail.env"; set +a
```

**Required for triage scripts:** `CHROMA_URL`, `GMAIL_EMBED_URL`, `GMAIL_RETRIEVE_URL`, `GMAIL_SLACK_CHANNEL`.

Scripts auto-source `$OPENCLAW_HOME/gmail.env` (or `GMAIL_ENV_FILE`) when present. Also export the same vars for the OpenClaw **MCP process** environment (gateway env or wrapper) — otherwise `email_query` / sync fail at runtime.

Operator command inventory (every CLI / runner / MCP type): [`COMMANDS.md`](./COMMANDS.md).

Locks live under `$OPENCLAW_HOME/run/` (not `/tmp`).

## 3. Gmail OAuth

Install/configure the Gmail MCP so credentials land under `$GMAIL_CREDS_DIR` (default `~/.gmail-mcp`):

- `credentials.json`
- `gcp-oauth.keys.json`

Confirm refresh works:

```bash
python3 "$OPENCLAW_HOME/bin/gmail_oauth_refresh.py"
```

## 4. Register MCP servers

```bash
openclaw mcp add email-query -- python3 "$OPENCLAW_HOME/bin/email_query_mcp.py"
openclaw mcp add gmail-triage-ops -- python3 "$OPENCLAW_HOME/bin/gmail_triage_ops_mcp.py"
openclaw mcp add list-unsubscribe -- python3 "$OPENCLAW_HOME/bin/list_unsubscribe_mcp.py"
```

Also register the Gmail autoauth MCP.

### Triage agent tool allowlist (deny-by-default)

Include:

- `email_query__list_recent`, `email_query__email_query`
- `gmail_triage_ops__finalize_triage`
- `list_unsubscribe__propose_unsubscribe`
- `list_unsubscribe__list_pending_unsubscribes`
- `list_unsubscribe__list_suppressed_senders`
- `list_unsubscribe__list_post_unsub_watch` (optional)
- `gmail__search_emails`, `gmail__read_email`, `gmail__draft_email` (draft only when asked)

**Do not** allowlist on the triage agent:

- `list_unsubscribe__approve_unsubscribe`
- `list_unsubscribe__reject_unsubscribe` (optional; prefer CLI)
- `list_unsubscribe__unsuppress_sender`
- `list_unsubscribe__clear_post_unsub_watch`

Approve/reject via CLI (human gate):

```bash
python3 "$OPENCLAW_HOME/bin/list_unsubscribe_mcp.py" --pending
python3 "$OPENCLAW_HOME/bin/list_unsubscribe_mcp.py" --approve <pending_id>
python3 "$OPENCLAW_HOME/bin/list_unsubscribe_mcp.py" --reject <pending_id>
python3 "$OPENCLAW_HOME/bin/list_unsubscribe_mcp.py" --watch
```

Successful approve adds the sender to a **post-unsub watch**. After `GMAIL_POST_UNSUB_GRACE_DAYS` (default 3), `finalize_triage` forces matching mail to `SPAM` (mark-read) and skips re-queueing unsub.

```bash
# Offline regression (no Gmail)
python3 package/scripts/test_post_unsub_watch.py

# Inspect / manage watch list on the host
python3 "$OPENCLAW_HOME/bin/list_unsubscribe_mcp.py" --watch
python3 "$OPENCLAW_HOME/bin/list_unsubscribe_mcp.py" --unwatch <email-or-domain>
```

## 5. Agent instructions

1. Copy [`../openclaw/AGENTS.template.md`](../openclaw/AGENTS.template.md) (or [`AGENTS.md`](../openclaw/AGENTS.md)) into the agent workspace.
2. Set agent id to match `GMAIL_AGENT_ID` (default: `gmail-triage`).
3. Prefer a model that reliably emits `tool_call` with **`id` set**. Page size ≤25 for smaller models.

## 6. Sync once, then cron

```bash
set -a; source "$OPENCLAW_HOME/gmail.env"; set +a
python3 "$OPENCLAW_HOME/bin/gmail_sync.py"
```

Sync stores Gmail label **IDs** and resolved **names** (`label_names`) so `skip_labeled` can match `GMAIL_LABEL_PREFIX/*`. Sync exits non-zero if Chroma/embed largely fails.

| Name | Schedule | Command |
|---|---|---|
| `gmail-triage-2h` | every 2h | `gmail_triage_2h.sh` |
| `gmail-nightly-triage` | 30 6 * * * ET | `gmail_nightly.sh` |
| `gmail-oauth-refresh` | 0 6 * * * ET | `python3 …/gmail_oauth_refresh.py` |

Example cron payload: [`../openclaw/cron.example.json`](../openclaw/cron.example.json).

**Pagination:** with `skip_seen=true`, runners keep `offset=0` every page and advance a processed counter — do not bump `offset` while seen shrinks the eligible set.

## 7. Smoke test (≤25)

```bash
set -a; source "$OPENCLAW_HOME/gmail.env"; set +a
GMAIL_TRIAGE_TOTAL=25 GMAIL_TRIAGE_CHUNK=25 "$OPENCLAW_HOME/bin/gmail_triage_2h.sh"
```

**Pass when:**

1. Sync completes against your Chroma collection (exit 0).
2. Agent lists ≤25 unread (or reports none).
3. If mail existed: `finalize_triage` applied labels (`GMAIL_LABEL_PREFIX/<CAT>`, default `OC/`).
4. Slack digest posted (ACTION/URGENT + NEWSLETTER bullets only).
5. Verify script exits 0 (recovers orphan finalize if the model dropped `tool_call` `id`).

## Unsubscribe security notes

- One-click unsub requires **https**, refuses private/link-local hosts, and **does not follow redirects**.
- Approve re-checks live `List-Unsubscribe` headers against the pending target.
- Approve takes **pending proposal ids only** (no auto-propose + execute).
- Post-unsub watch → SPAM is a **label** policy after human approve; it does not execute another unsub.

## Pitfalls

- **Missing `tool_call` `id`** → validation fails; rely on `gmail_e2e_verify_batch.py` after each page.
- **Missing URL env** → scripts/MCP raise required-env errors.
- **Wrong collection** → set `GMAIL_CHROMA_COLLECTION`.
- **MCP without env** → gateway must inherit `gmail.env` vars.
- **Never** allowlist `approve_unsubscribe` on the triage agent or expand to 200 one-shot triage on weak tool-callers.
