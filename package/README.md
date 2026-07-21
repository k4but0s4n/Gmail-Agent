# openclaw-gmail-triage

Safe, human-in-the-loop Gmail triage for OpenClaw: MCP servers, cron scripts, and agent instructions.

**Install:** [`docs/INSTALL.md`](./docs/INSTALL.md) · **Changes:** [`../CHANGELOG.md`](../CHANGELOG.md) · **License:** MIT

## Layout

```text
package/
├── mcp/                 # stdlib NDJSON MCP + sync/prune/oauth/verify + _config.py
├── scripts/             # every-2h triage, nightly, e2e
├── openclaw/            # AGENTS, SKILL.md, cron example, eval fixtures
├── docs/                # INSTALL, ARCHITECTURE, PACKAGING
├── skill/SKILL.md       # optional Cursor maintainer aid
├── .env.example
└── LICENSE
```

## Behavior

1. Sync Gmail → Chroma (`GMAIL_CHROMA_COLLECTION`, default `gmail_inbox`)
2. List unread / unlabeled / unseen (pages ≤25)
3. One `finalize_triage` per page → labels (`GMAIL_LABEL_PREFIX`, default `OC`), mark-read, unsub queue
4. Slack digest (ACTION/URGENT + NEWSLETTER bullets only)
5. Verify/recover missing `tool_call` `id`

**Never:** send, delete, trash, archive, or unsubscribe without explicit human approve.

## Quick start

```bash
cp package/.env.example ~/.openclaw/gmail.env   # edit placeholders
# then follow docs/INSTALL.md
GMAIL_TRIAGE_TOTAL=25 "$OPENCLAW_HOME/bin/gmail_triage_2h.sh"
```

Do not commit secrets. Triage agents must not allowlist `approve_unsubscribe`.
