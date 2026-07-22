# Architecture

## Data plane

```
┌─────────────┐     OAuth      ┌──────────────┐
│   Gmail     │◄──────────────►│ gmail MCP    │
│             │                │ (search/read │
│             │   sync.py      │  /draft)     │
└──────┬──────┘───────────────►│              │
       │                       └──────────────┘
       │ embed (GMAIL_EMBED_URL) + upsert
       ▼
┌─────────────┐   retrieve     ┌──────────────┐
│ Chroma      │◄──────────────►│ email_query  │
│ collection  │ GMAIL_RETRIEVE │ list_recent  │
│ (env name)  │     _URL       └──────┬───────┘
└─────────────┘                       │
                                      ▼
                              ┌──────────────┐
                              │ triage agent │
                              │ (GMAIL_      │
                              │  AGENT_ID)   │
                              └──────┬───────┘
                     categorize      │
                                     ▼
                              ┌──────────────┐     ┌─────────────────┐
                              │ finalize_    │────►│ list_unsubscribe│
                              │ triage       │     │ (propose only)  │
                              └──────┬───────┘     └────────┬────────┘
                                     │                      │
                                     ▼                      ▼
                              Slack digest            pending JSON
                              (short)                 (human approve)
```

URLs and paths come from env via `mcp/_config.py` (`CHROMA_URL`, `GMAIL_EMBED_URL`, `GMAIL_RETRIEVE_URL`, `OPENCLAW_HOME`, …). See [`.env.example`](../.env.example).

## Control plane

- **OpenClaw cron** runs shell wrappers (not agent payloads) so sync/lock/verify stay deterministic.
- **Fresh `--session-key` per run** avoids transcript compaction timeouts.
- **`triage_seen.json`** + `{GMAIL_LABEL_PREFIX}/*` labels = idempotency (default prefix `OC`).

## Category policy

| Cat | Label | Mark read | Unsub queue | Slack bullets |
|---|---|---|---|---|
| URGENT | PREFIX/URGENT | no | no | yes (with ACTION) |
| ACTION-REQUIRED | PREFIX/ACTION-REQUIRED | no | no | yes |
| FYI | PREFIX/FYI | no | no | no |
| SOCIAL | PREFIX/SOCIAL | yes | no | no |
| NEWSLETTER | PREFIX/NEWSLETTER | yes | yes | yes |
| SPAM | PREFIX/SPAM | no* | yes* | no |

`PREFIX` = `GMAIL_LABEL_PREFIX` (default `OC`).

\* Post-unsub recidivists (sender watched after a successful approve, past grace): forced to **SPAM**, marked read by default (`GMAIL_MARK_POST_UNSUB_SPAM_READ`), and **not** re-queued for unsubscribe. **URGENT** / **ACTION-REQUIRED** are never overridden. Domain promotion only counts **NEWSLETTER** / **SPAM** siblings.

## Post-unsub watch

```
approve_unsubscribe (ok)
        │
        ▼
unsubscribe_watch.json  (email scope by default + grace_days)
        │
   after grace
        ▼
finalize_triage → category=SPAM, mark read, skip propose
        │
   optional: promote to domain after N distinct From emails
```

State file: `$GMAIL_UNSUB_STATE/unsubscribe_watch.json` (default `~/.openclaw/gmail/`).
## Failure modes & mitigations

| Failure | Mitigation |
|---|---|
| Model output length on big pages | Chunk ≤25 |
| `tool_call` missing `id` | Compact args + verify/recover script |
| Fake Slack success | VERIFY checks session / applies orphan |
| Re-processing | `skip_seen` + `skip_labeled` |
| Overlapping crons | `flock` on 2h script |
| Missing RAG URLs | Fail fast with required-env error |

## Trust boundary

LLM chooses **category strings only**. All Gmail mutations go through `finalize_triage` / explicit unsub approve tools with allowlists.
