# Security

## What this project never does in triage

- Send mail (except approved mailto unsubscribe, via CLI/human path)
- Delete, trash, or archive mail
- Auto-execute unsubscribe without an explicit human approve of a **pending** id

## Operator checklist

1. Do **not** allowlist `list_unsubscribe__approve_unsubscribe` on the triage agent.
2. Keep Gmail OAuth files and OpenClaw `secrets.json` outside git (`credentials.json`, `gcp-oauth.keys.json`).
3. Treat Chroma / embedder / retrieve URLs as trusted internal services; do not expose them publicly without auth.
4. Prefer `0o600` on state files; scripts already chmod when writing.
5. Review pending unsubs before `--approve`; one-click targets are re-validated and SSRF-hardened but ESP redirects may fail closed.

## Reporting issues

Open a GitHub issue describing the impact and reproduction. Do not attach tokens, mailbox dumps, or full `secrets.json`.
