# Security

## What this project never does in triage

- Send mail (except approved mailto unsubscribe, via CLI/human path)
- Delete, trash, or archive mail
- Auto-execute unsubscribe without an explicit human approve of a **pending** id

## Post-unsub SPAM override

After a human **approves** an unsubscribe, the sender is watched. Past a configurable grace window, `finalize_triage` may **relabel** matching mail as `SPAM` and mark it read. That is intentional recidivism handling — not auto-unsubscribe. Clearing a watch (`--unwatch` / `clear_post_unsub_watch`) is a human operator action.

## Operator checklist

1. Do **not** allowlist `list_unsubscribe__approve_unsubscribe` on the triage agent.
2. Do **not** allowlist `reject_unsubscribe`, `unsuppress_sender`, or `clear_post_unsub_watch` on the triage agent (prefer CLI).
3. Keep Gmail OAuth files and OpenClaw `secrets.json` outside git (`credentials.json`, `gcp-oauth.keys.json`).
4. Treat Chroma / embedder / retrieve URLs as trusted internal services; do not expose them publicly without auth.
5. Prefer `0o600` on state files; scripts already chmod when writing.
6. Review pending unsubs before `--approve`; one-click targets are re-validated and SSRF-hardened but ESP redirects may fail closed.
7. Review `--watch` periodically; use `--unwatch` if a legitimate sender was promoted too broadly (domain scope).
8. Unsubscribe approve is **human CLI only** (`list_unsubscribe_mcp.py --approve <pending_id>`). Digests list pending ids + senders in Slack; do not allowlist `approve_unsubscribe` on the triage agent.

## Reporting issues

Open a GitHub issue describing the impact and reproduction. Do not attach tokens, mailbox dumps, or full `secrets.json`.
