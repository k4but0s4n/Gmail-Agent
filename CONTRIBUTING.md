# Contributing

## Local layout

- `package/mcp/` — stdlib Python MCP + ops (keep dependencies at zero)
- `package/scripts/` — cron runners
- `package/openclaw/` — agent rules / skill text
- `package/docs/` — install and architecture

## Rules of the road

- No LAN IPs, home paths, tokens, or personal mailbox data in the tree
- Preserve safety invariants (see root README and `SECURITY.md`)
- Prefer env via `mcp/_config.py` over new hardcodes
- Update `CHANGELOG.md` for user-visible fixes

## Smoke

After changing MCP or scripts, on a configured host:

```bash
set -a; source "$OPENCLAW_HOME/gmail.env"; set +a
GMAIL_TRIAGE_TOTAL=25 "$OPENCLAW_HOME/bin/gmail_triage_2h.sh"
```
