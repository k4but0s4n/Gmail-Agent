# Packaging

**openclaw-gmail-triage** ships as:

1. **MCP + scripts** (this `package/mcp/` and `package/scripts/`) — register with `openclaw mcp add`
2. **Agent instructions** — [`openclaw/AGENTS.template.md`](../openclaw/AGENTS.template.md) or [`openclaw/SKILL.md`](../openclaw/SKILL.md)
3. **Install guide** — [`INSTALL.md`](./INSTALL.md)

Optional: Cursor maintainer aid in [`../skill/SKILL.md`](../skill/SKILL.md) (not required for end users).

## Install surfaces

| Surface | How |
|---|---|
| GitHub (this repo) | Clone → copy bin files → env → `mcp add` → agent rules → cron |
| Local skill | Copy `openclaw/SKILL.md` into a workspace `skills/gmail-triage/` folder |
| ClawHub (optional later) | `clawhub skill publish` of a folder whose root is `SKILL.md` |

MCP Python servers are **not** ClawHub plugins by themselves; they are registered via OpenClaw’s MCP client registry.

## Non-goals

- Auto-unsubscribe without human approve
- Hosted SaaS
- Guaranteeing 200-message single-shot triage
- Bundling finance / other agents
