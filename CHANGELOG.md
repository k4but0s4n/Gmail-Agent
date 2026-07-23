# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Fixed
- **Digest bullets missing From/Subject** — verify enriches compact finalize items from `list_recent` metadata so Slack matches the prior `id · From · Subject` standard; empty inbox skips Slack post.

### Changed
- **Triage schedule** — replace every-2h with **7am · 5pm · 10pm · 2am** America/New_York (`gmail-triage-digest` in `cron.example.json`).

## [0.2.1] — 2026-07-22

### Fixed
- **Slack tool_call leak** — verify fails (with `retry: true`) when the model echoes `tool_call` YAML as chat text instead of invoking tools; runners retry the batch once on a new session key. Previously `listed_count: 0` was treated as an empty inbox and `--deliver` posted the leak to Slack.
- **Post-unsub override guards** — no hit bumps during grace; no double-count on same-batch domain promote; never override URGENT/ACTION-REQUIRED; sibling promotion only from NEWSLETTER/SPAM; surface `missing_from` when From cannot be resolved.
- **Reclassify stacks OC labels** — `finalize_triage` now removes other `PREFIX/*` category labels when applying a new one (e.g. ACTION-REQUIRED → SPAM replaces, does not stack). SPAM is marked read by default (`GMAIL_MARK_SPAM_READ=1`).

### Changed
- **Triage runners** (`gmail_triage_2h.sh`, `gmail_e2e_200.sh`) — no agent `--deliver`; runner posts `slack_text` from verify only after success.

### Added
- **Verify `slack_text`** — `gmail_e2e_verify_batch.py` builds the Slack digest; regression `scripts/test_verify_tool_leak.py`.
- **Commands doc** — [`package/docs/COMMANDS.md`](package/docs/COMMANDS.md): imperative operator phrases with `[placeholders]` (e.g. *List pending to unsubscribe*, *Unsuppress this sender/domain `[…]`*) plus CLI invocations; linked from README / INSTALL / MANIFEST.

## [0.2.0] — 2026-07-22

### Added
- **Post-unsub watch → SPAM** — after a successful `approve_unsubscribe`, the sender is recorded in `$GMAIL_UNSUB_STATE/unsubscribe_watch.json`. Once `GMAIL_POST_UNSUB_GRACE_DAYS` elapses (default **3**), `finalize_triage` forces matching mail to **SPAM**, marks it read by default, and **does not** re-queue unsubscribe.
- **Domain promotion** — email-scoped watches promote to domain scope after `GMAIL_POST_UNSUB_DOMAIN_AFTER_HITS` distinct From addresses (default **2**); siblings on the same domain count toward promotion without SPAM-labeling until promoted.
- **CLI / MCP** — `list_post_unsub_watch`, `clear_post_unsub_watch`; CLI `--watch`, `--watch-add FROM [--grace N] [--days-ago N] [--scope email|domain]`, `--unwatch KEY`.
- **Env knobs** — `GMAIL_POST_UNSUB_WATCH`, `GMAIL_POST_UNSUB_GRACE_DAYS`, `GMAIL_POST_UNSUB_SCOPE`, `GMAIL_POST_UNSUB_DOMAIN_AFTER_HITS`, `GMAIL_MARK_POST_UNSUB_SPAM_READ` (see `.env.example`).
- **Tests** — offline `scripts/test_post_unsub_watch.py`; optional live host harness `scripts/e2e_post_unsub_live.py`.

### Changed
- `list_unsubscribe` MCP **v0.5.0**; `gmail_triage_ops` MCP **v0.3.0**.
- Docs (README, INSTALL, ARCHITECTURE, SECURITY, AGENTS, MANIFEST) document the watch → SPAM path and operator CLI.

### Security
- Keep `approve_unsubscribe`, `reject_unsubscribe`, `unsuppress_sender`, and `clear_post_unsub_watch` off the triage agent allowlist. Read-only `list_post_unsub_watch` is optional.

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

[0.2.1]: https://github.com/k4but0s4n/Gmail-Agent/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/k4but0s4n/Gmail-Agent/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/k4but0s4n/Gmail-Agent/releases/tag/v0.1.0
