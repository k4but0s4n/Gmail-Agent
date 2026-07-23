#!/usr/bin/env python3
"""Shared Slack chat.postMessage helper for triage digests (Block Kit Approve).

Used by gmail_triage_2h.sh / gmail_e2e_200.sh. Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

import _config as cfg

ACTION_ID = "gmail_unsub_approve"


def load_bot_token() -> str:
    secrets = cfg.secrets_path()
    if not secrets.exists():
        raise RuntimeError(f"secrets not found: {secrets}")
    data = json.loads(secrets.read_text(encoding="utf-8"))
    providers = data.get("providers") or {}
    slack = providers.get("slack") or data.get("slack") or {}
    if isinstance(slack, dict):
        for key in ("botToken", "bot_token", "token"):
            if slack.get(key):
                return str(slack[key])
    for key in ("SLACK_BOT_TOKEN", "slack_bot_token"):
        if data.get(key):
            return str(data[key])
    raise RuntimeError("Slack bot token missing in secrets.json")


def post_message(
    channel: str,
    text: str,
    *,
    thread_ts: str = "",
    blocks: list | None = None,
    timeout: float = 20.0,
) -> dict:
    """Post to chat.postMessage. Returns Slack API JSON (includes ts when ok)."""
    channel = (channel or "").strip()
    if not channel:
        return {"ok": False, "error": "channel_required"}
    token = load_bot_token()
    payload: dict = {"channel": channel, "text": text or ""}
    if thread_ts:
        payload["thread_ts"] = thread_ts
    if blocks:
        payload["blocks"] = blocks
    req = urllib.request.Request(
        "https://slack.com/api/chat.postMessage",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace") if exc.fp else ""
        return {"ok": False, "error": f"http_{exc.code}", "detail": body[:300]}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def build_approve_blocks(digest_text: str, batch_id: str) -> list:
    """Block Kit: digest section + Approve button + allowlist context."""
    # Slack section text hard limit ~3000; keep a safe margin.
    body = (digest_text or "").strip()
    if len(body) > 2900:
        body = body[:2900].rstrip() + "…"
    return [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": body or "_Triage digest_"},
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "action_id": ACTION_ID,
                    "text": {"type": "plain_text", "text": "Approve these unsubs", "emoji": False},
                    "style": "primary",
                    "value": batch_id,
                }
            ],
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": (
                        "Only allowlisted operators; executes one-click unsubs. "
                        "CLI `--approve` still works."
                    ),
                }
            ],
        },
    ]


def _parse_ids(raw: str) -> list[str]:
    out: list[str] = []
    for part in (raw or "").replace("\n", ",").split(","):
        pid = part.strip()
        if pid and pid not in out:
            out.append(pid)
    return out


def post_digest(
    channel: str,
    slack_text: str,
    *,
    unsub_draft_text: str = "",
    unsub_ids: list[str] | None = None,
    session_key: str = "",
) -> dict:
    """Post digest (with Approve button when ids present) + optional CLI draft thread."""
    ids = [str(x).strip() for x in (unsub_ids or []) if str(x).strip()]
    blocks = None
    batch_id = ""
    if ids:
        import list_unsubscribe_mcp as unsub  # noqa: WPS433

        batch_id = unsub.save_unsub_draft_batch(
            ids,
            {"session_key": session_key, "channel": channel},
        )
        blocks = build_approve_blocks(slack_text, batch_id)

    parent = post_message(channel, slack_text, blocks=blocks)
    result = {
        "ok": bool(parent.get("ok")),
        "ts": parent.get("ts") or "",
        "batch_id": batch_id,
        "parent": parent,
        "thread": None,
    }
    if not result["ok"]:
        print(f"slack_err {parent}", file=sys.stderr)
        return result
    print(f"slack_ok ts={result['ts']} batch={batch_id or '-'}", file=sys.stderr)

    draft = (unsub_draft_text or "").strip()
    if draft and result["ts"]:
        thread = post_message(channel, draft, thread_ts=result["ts"])
        result["thread"] = thread
        if not thread.get("ok"):
            print(f"slack_err thread {thread}", file=sys.stderr)
    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Post Slack triage digest / plain message")
    ap.add_argument("--channel", required=True)
    ap.add_argument("--text", default="", help="Plain text (also digest fallback)")
    ap.add_argument("--thread-ts", default="", help="Reply in thread (plain post only)")
    ap.add_argument(
        "--digest",
        action="store_true",
        help="Post as triage digest (Block Kit Approve when --unsub-ids set)",
    )
    ap.add_argument("--unsub-draft", default="", help="CLI draft text for thread reply")
    ap.add_argument("--unsub-ids", default="", help="Comma-separated pending proposal ids")
    ap.add_argument("--session-key", default="")
    args = ap.parse_args(argv)

    # Prefer OPENCLAW_SECRETS if runners export it.
    if os.environ.get("OPENCLAW_SECRETS"):
        os.environ.setdefault("OPENCLAW_SECRETS", os.environ["OPENCLAW_SECRETS"])

    if args.digest or args.unsub_ids or args.unsub_draft:
        out = post_digest(
            args.channel,
            args.text,
            unsub_draft_text=args.unsub_draft,
            unsub_ids=_parse_ids(args.unsub_ids),
            session_key=args.session_key,
        )
        if out.get("ts"):
            print(out["ts"], end="")
        return 0 if out.get("ok") else 1

    data = post_message(args.channel, args.text, thread_ts=args.thread_ts)
    if data.get("ok"):
        print(data.get("ts") or "", end="")
        print(f"slack_ok ts={data.get('ts')}", file=sys.stderr)
        return 0
    print(f"slack_err {data}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
