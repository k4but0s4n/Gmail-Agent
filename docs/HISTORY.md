# Design history (2026-07-20)

Notes from the first working OpenClaw Gmail triage deployment. Host-specific details are omitted; see [`package/docs/INSTALL.md`](./package/docs/INSTALL.md) for current setup.

## Goal

Gmail triage on OpenClaw: list unread mail, categorize, label, queue newsletter/spam unsubs for approval, Slack digests — safely (never send/delete/auto-unsub).

## What shipped

### Taxonomy (6 categories)

`URGENT` · `ACTION-REQUIRED` · `FYI` · `SOCIAL` · `NEWSLETTER` · `SPAM`

| Category | Side effects |
|---|---|
| URGENT / ACTION-REQUIRED | Label · Slack digest |
| NEWSLETTER | Label · mark read · unsub queue · Slack |
| SOCIAL | Label · mark read · **no** auto-unsub · omitted from Slack |
| FYI | Label only |
| SPAM | Label · unsub queue · omitted from Slack |

**Later (0.2.0):** successful unsub approve watches the sender; after grace, recidivist mail is forced to SPAM (mark-read, no re-queue).

**Later (0.2.1):** runners no longer use agent `--deliver`; verify builds `slack_text` and fails closed on leaked `tool_call` YAML (retry once). Reclassify strips sibling `PREFIX/*` labels; SPAM marked read by default.

**Later (0.3.0 / reverted button):** digests show pending unsub ids + senders + open-queue totals; approve via CLI (Slack Approve button removed).

### Slack digests

Bullets **only** for ACTION-REQUIRED (+ URGENT) and pending unsub queue (id + sender). Count line still shows all six. Posted by the runner after verify (not agent `--deliver`). Approve via CLI.

### Chunked triage (small-model safe)

- One-shot large batches fail (length / tool errors).
- Working pattern: **pages of ≤25**, one `finalize_triage` per page.
- Meta-tool `tool_call` must include `id`. Compact finalize (`message_id` + `category` only) + verify/recover helper.

### Operating cadence

| Job | Schedule | Role |
|---|---|---|
| Triage | 7am · 5pm · 10pm · 2am ET | Light sync + unread since today, pages of 25 |
| Nightly | early morning | Sync + prune only |
| OAuth refresh | early morning | Token refresh + expiry warnings |

### MCP surface

| Tool | Purpose |
|---|---|
| `email_query__list_recent` | Newest-first unread; skip labeled/seen; optional `since_today` |
| `gmail_triage_ops__finalize_triage` | Batch labels, mark-read, queue unsub |
| `list_unsubscribe__*` | Propose / approve / reject (human-in-the-loop) |
| Gmail MCP | search / read / draft (draft only when asked) |

## Hard lessons

1. Large one-shot triage is not viable on weak tool-callers; page ≤25.
2. Trust finalize results, not Slack prose — models can invent digests after tool failure.
3. Always put `id` on `tool_call`; keep finalize JSON tiny.
4. SOCIAL ≠ NEWSLETTER for unsub policy.
5. `skip_seen` + labels prevent re-triage once the backlog clears.

## Out of scope

- Auto-send, trash, archive, browser unsub without approve
- Finance / expense agents
- Guaranteeing huge single-shot triage

See [`CHANGELOG.md`](./CHANGELOG.md) for post-ship correctness and security fixes.
