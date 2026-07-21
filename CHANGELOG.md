# Changelog

All notable changes to this project are documented here.

## [0.1.0] — 2026-07-21

First public-ready release of **openclaw-gmail-triage**: stdlib MCP servers, cron scripts, agent templates, and install docs.

### Fixed

#### Correctness
- **`list_recent` crash** — undefined `CHROMA_BASE` after env scrub; now uses `cfg.chroma_api_base()`.
- **Python 3.9 import** — added `from __future__ import annotations` where union types are used.
- **`skip_labeled` ineffective** — sync stored Gmail label *IDs*; filter expected `OC/*` names. Sync now stores `label_names`; list resolves IDs → names via Gmail labels API.
- **Multi-page under-triage** — `offset` advanced while `skip_seen` shrunk the eligible set. Runners keep `offset=0` and advance a processed counter.
- **Sync false success** — sync could exit 0 after Chroma/embed failures; now fails closed when indexing largely fails.
- **Verify false positives** — bare `labels_applied` substring treated as success; now requires parsed/`ok: true` with finalize context.
- **Verify empty-inbox false negatives** — also recognize `eligible_total=N` in tool results.
- **State/creds corruption risk** — non-atomic JSON writes; now temp-file + `os.replace` with mode `0o600`.
- **Flock busy vs failure** — nightly treated lock-busy as failure; both runners skip busy locks with exit 0. Locks live under `$OPENCLAW_HOME/run/`.
- **Missing env at cron** — scripts auto-source `$OPENCLAW_HOME/gmail.env` and require URL/Slack vars upfront.

#### Security
- **Honor-system unsub approve** — triage agents must not allowlist `approve_unsubscribe`; docs/CLI gate human approve (`--approve` / `--reject`).
- **Approve auto-propose with `force=True`** — approve no longer creates proposals or bypasses suppression; pending ids only.
- **One-click SSRF** — HTTPS only, block private/link-local hosts, refuse HTTP redirects; re-validate live List-Unsubscribe headers on approve.
- **Mailto header injection** — reject CR/LF in to/subject/body; basic address shape check.
- **Shell injection via `bash -c` env splice** — runners use safer heredoc/`bash -s` and path env vars.
- **World-readable state** — pending/seen/creds writes chmod `0o600`; state dirs prefer `0o700`.

### Changed
- Config centralized in `package/mcp/_config.py` (no LAN IPs or absolute home paths in source).
- Default agent id `gmail-triage`; default collection `gmail_inbox`; label prefix still `OC` via `GMAIL_LABEL_PREFIX`.
- Eval set uses category fixtures only (no live Gmail message ids).

### Security notes for operators
- Keep `approve_unsubscribe` off the triage agent allowlist.
- Bind Chroma/embed/retrieve carefully; treat them as trusted internal services.
- Never commit `credentials.json`, `gcp-oauth.keys.json`, `secrets.json`, or a filled-in `gmail.env`.
