---
name: gmail-triage
description: >-
  Safe human-in-the-loop Gmail triage for OpenClaw. Categorize unread mail into
  six labels, post a short Slack digest, and queue newsletter/spam unsubs for
  human approval. Requires email-query, gmail-triage-ops, and Gmail OAuth MCP.
---

# Gmail triage

## When to use

Recurring or on-demand triage of unread Gmail: label, digest, queue unsubs — without sending, deleting, or auto-unsubscribing.

## Hard rules

- **Never** send / delete / archive / trash
- **Never** call `list_unsubscribe__approve_unsubscribe` (human: CLI only)
- Slack operator may call reject / suppress / unsuppress / list suppressed when asked
- One `gmail_triage_ops__finalize_triage` per page; pages **≤25**
- Every meta `tool_call` must include `id` (e.g. `gmail_triage_ops__finalize_triage`)
- Finalize may reclassify post-unsub recidivists as SPAM automatically — categorize normally
- Finalize args: `{message_id, category}` only
- Slack bullets only for ACTION-REQUIRED (+ URGENT) and NEWSLETTER

## Categories (only these)

`URGENT` | `ACTION-REQUIRED` | `FYI` | `SOCIAL` | `NEWSLETTER` | `SPAM`

- **URGENT** — time-sensitive; needs you soon
- **ACTION-REQUIRED** — needs a reply/decision
- **FYI** — informative; no reply
- **SOCIAL** — social network noise (likes, comments, DM digests); mark read; no unsub
- **NEWSLETTER** — recurring marketing/digests; mark read; queue unsub
- **SPAM** — cold junk; queue unsub

## Workflow

1. `email_query__list_recent` with `unread_only=true`, `skip_labeled=true`, `skip_seen=true`, optional `since_today=true`, `limit≤25`, `offset=0`
2. Categorize every hit
3. One `finalize_triage`
4. Slack digest only after finalize `ok: true`

## Setup

See the repository [`docs/INSTALL.md`](../docs/INSTALL.md): env file, OAuth, `openclaw mcp add`, allowlist (no approve), cron. Unsub approve:

```bash
python3 "$OPENCLAW_HOME/bin/list_unsubscribe_mcp.py" --pending
python3 "$OPENCLAW_HOME/bin/list_unsubscribe_mcp.py" --approve <pending_id>
python3 "$OPENCLAW_HOME/bin/list_unsubscribe_mcp.py" --watch
```
