# Manifest — openclaw-gmail-triage package

| Path | Role | Notes |
|---|---|---|
| `mcp/_config.py` | Shared env/path helpers | No LAN IPs in source |
| `mcp/email_query_mcp.py` | MCP: `email_query`, `list_recent` | v0.4.0 · requires embed/retrieve/chroma URLs |
| `mcp/gmail_triage_ops_mcp.py` | MCP: `finalize_triage` | v0.3.1 · exclusive OC labels + SPAM mark-read |
| `mcp/list_unsubscribe_mcp.py` | MCP: unsub propose/approve | v0.5.0 · watch list after approve |
| `mcp/gmail_sync.py` | Gmail → Chroma sync | Env: sync lookback/max + URLs |
| `mcp/gmail_prune.py` | Prune old index docs | `CHROMA_URL` |
| `mcp/gmail_oauth_refresh.py` | Token refresh | Cron daily |
| `mcp/gmail_e2e_verify_batch.py` | Post-batch verify + orphan finalize recover | `GMAIL_AGENT_ID` |
| `scripts/gmail_triage_2h.sh` | Production runner (≤50 today, 25/page) | Cron 7am · 5pm · 10pm · 2am ET |
| `scripts/gmail_nightly.sh` | Sync + prune only | |
| `scripts/gmail_e2e_200.sh` | Chunked e2e harness | Default TOTAL 25 |
| `scripts/test_post_unsub_watch.py` | Offline e2e for post-unsub → SPAM | No Gmail/network |
| `scripts/e2e_post_unsub_live.py` | Live host e2e (backfill watch + finalize) | Mutates Gmail labels |
| `openclaw/AGENTS.md` | Agent hard rules (live reference) | |
| `openclaw/AGENTS.template.md` | Blank-host agent template | |
| `openclaw/cron.example.json` | Example cron registrations | |
| `openclaw/gmail-eval-set.json` | Category fixtures | No personal message ids |
| `docs/INSTALL.md` | Blank-host install | |
| `docs/COMMANDS.md` | Imperative command phrases + CLI invocations | |
| `docs/ARCHITECTURE.md` | Data/control plane | |
| `.env.example` | Env template | |
| `LICENSE` | MIT | |

## Live cron (reference deployment)

| Name | Schedule | Command |
|---|---|---|
| `gmail-triage-digest` | 7am · 5pm · 10pm · 2am ET | `gmail_triage_2h.sh` |
| `gmail-nightly-triage` | 30 6 * * * ET | `gmail_nightly.sh` (sync+prune) |
| `gmail-oauth-refresh` | 0 6 * * * ET | `gmail_oauth_refresh.py` |

## Env knobs

```
# Required for RAG path + triage script
CHROMA_URL=
GMAIL_EMBED_URL=
GMAIL_RETRIEVE_URL=

# Paths (defaults under $HOME)
OPENCLAW_HOME=
GMAIL_CREDS_DIR=
GMAIL_ENV_FILE=           # default $OPENCLAW_HOME/gmail.env
GMAIL_AGENT_ID=gmail-triage
GMAIL_CHROMA_COLLECTION=gmail_inbox
GMAIL_LABEL_PREFIX=OC

GMAIL_TRIAGE_TOTAL=50
GMAIL_TRIAGE_CHUNK=25
GMAIL_TRIAGE_MAX_ITEMS=25
GMAIL_TRIAGE_LABEL_MODE=batch|sequential
GMAIL_MARK_NEWSLETTER_READ=1
GMAIL_MARK_SOCIAL_READ=1
GMAIL_POST_UNSUB_WATCH=1
GMAIL_POST_UNSUB_GRACE_DAYS=3
GMAIL_POST_UNSUB_SCOPE=email
GMAIL_POST_UNSUB_DOMAIN_AFTER_HITS=2
GMAIL_MARK_POST_UNSUB_SPAM_READ=1
GMAIL_LIST_RECENT_MAX=50
GMAIL_TRIAGE_TZ=America/New_York
GMAIL_SYNC_LOOKBACK_DAYS=2
GMAIL_SYNC_MAX_EMAILS=80
GMAIL_SLACK_CHANNEL=          # required for triage/e2e
GMAIL_ALERT_SLACK_CHANNEL=
```

Scripts source `gmail.env` automatically. MCP servers need the same vars in the gateway environment. See [`docs/INSTALL.md`](docs/INSTALL.md) for allowlist (no `approve_unsubscribe` on triage agent) and CLI approve / watch flow.
