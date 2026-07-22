---
name: openclaw-gmail-triage-packaging
description: >-
  Package and extend the OpenClaw Gmail triage stack (MCP + cron + agent rules)
  for community release. Use when working in Gmail-Agent/package, publishing
  openclaw-gmail-triage, or converting live gmail-triage scripts into a skill/MCP.
---

# OpenClaw Gmail triage — packaging skill

## When to use

- Turning the live `gmail-triage` deployment into a distributable skill/MCP
- Editing files under `package/` in the Gmail-Agent repo
- Writing install docs, env config, or OpenClaw cron recipes for Gmail triage

## Source of truth

1. `package/openclaw/AGENTS.md` / `AGENTS.template.md` — agent behavior
2. `package/mcp/` — MCP servers + `_config.py` + sync/prune/oauth/verify
3. `package/scripts/gmail_triage_2h.sh` — production recurring runner
4. `package/docs/INSTALL.md` — blank-host install
5. `package/.env.example` — required URLs and knobs
6. `docs/HISTORY.md` — why the design is this way

## Invariants (do not weaken)

- Never send / delete / trash / archive in triage flows
- Unsubscribe is propose → human approve only
- Post-unsub recidivists are forced to SPAM by finalize (past grace); agents categorize normally
- Finalize is the sole batch mutator during triage
- Slack bullets: ACTION-REQUIRED (+ URGENT) and NEWSLETTER only
- Page size ≤25 for small tool-calling models; verify after each page
- SOCIAL is labeled + marked read; not auto-queued for unsub
- No LAN IPs or absolute home paths in package source (use `_config` / env)

## Packaging workflow

1. Read `package/docs/PACKAGING.md` and `INSTALL.md`
2. Keep URLs/paths in env via `mcp/_config.py` (never re-hardcode host IPs)
3. Keep MCP stdlib/NDJSON unless there is a strong reason to add deps
4. Copy `_config.py` alongside MCP scripts when installing to `$OPENCLAW_HOME/bin`
5. Document the `tool_call` + `id` requirement and the verify/recover helper
6. Keep eval fixtures free of personal Gmail message ids

## Smoke test definition of done

1. Sync indexes recent mail (exit 0)  
2. `list_recent` with `since_today=true`, limit 25, `offset=0`  
3. `finalize_triage` returns `ok: true` with `labels_applied > 0` (or 0 if empty)  
4. Gmail shows `{GMAIL_LABEL_PREFIX}/*` (default `OC/`); NEWSLETTER/SOCIAL lose UNREAD  
5. Slack digest matches finalize counts (not invented)

Approve unsubs only via CLI — never from the triage agent allowlist.

## Out of scope

Finance agent, browser automation, auto-unsub execution, one-shot 200 triage.
