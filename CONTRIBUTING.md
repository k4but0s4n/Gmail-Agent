# Contributing

## Local layout

- `package/mcp/` — stdlib Python MCP + ops (keep dependencies at zero)
- `package/scripts/` — cron runners + e2e harnesses
- `package/openclaw/` — agent rules / skill text
- `package/docs/` — install and architecture

## Rules of the road

- No LAN IPs, home paths, tokens, or personal mailbox data in the tree
- Preserve safety invariants (see root README and `SECURITY.md`)
- Prefer env via `mcp/_config.py` over new hardcodes
- Update `CHANGELOG.md` for user-visible changes (Keep a Changelog + semver)
- Keep agent allowlists tight: never add approve / clear-watch tools to triage

## Tests

Before opening a PR that touches unsub or finalize:

```bash
python3 package/scripts/test_post_unsub_watch.py
python3 -m py_compile package/mcp/list_unsubscribe_mcp.py package/mcp/gmail_triage_ops_mcp.py
```

## Smoke

After changing MCP or scripts, on a configured host:

```bash
set -a; source "$OPENCLAW_HOME/gmail.env"; set +a
GMAIL_TRIAGE_TOTAL=25 "$OPENCLAW_HOME/bin/gmail_triage_2h.sh"
```

Optional live post-unsub harness (labels real mail — use carefully):

```bash
cp package/scripts/e2e_post_unsub_live.py "$OPENCLAW_HOME/bin/"
cd "$OPENCLAW_HOME/bin" && python3 e2e_post_unsub_live.py
```
