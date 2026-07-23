#!/usr/bin/env python3
"""Shared Slack chat.postMessage helper for triage digests.

Used by gmail_triage_2h.sh / gmail_e2e_200.sh. Stdlib only.
Plain text digests only (no Block Kit Approve buttons).
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


def post_digest(
    channel: str,
    slack_text: str,
    *,
    unsub_draft_text: str = "",
    unsub_ids: list[str] | None = None,
    approve_lines: list[str] | None = None,
    session_key: str = "",
) -> dict:
    """Post plain-text digest. Extra kwargs kept for runner CLI compat."""
    del unsub_draft_text, unsub_ids, approve_lines, session_key
    parent = post_message(channel, slack_text)
    result = {
        "ok": bool(parent.get("ok")),
        "ts": parent.get("ts") or "",
        "batch_id": "",
        "parent": parent,
        "thread": None,
    }
    if not result["ok"]:
        print(f"slack_err {parent}", file=sys.stderr)
        return result
    print(f"slack_ok ts={result['ts']}", file=sys.stderr)
    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Post Slack triage digest / plain message")
    ap.add_argument("--channel", required=True)
    ap.add_argument("--text", default="", help="Plain text (also digest fallback)")
    ap.add_argument("--thread-ts", default="", help="Reply in thread (plain post only)")
    ap.add_argument("--digest", action="store_true", help="Post as triage digest")
    ap.add_argument("--unsub-draft", default="", help="Ignored (compat)")
    ap.add_argument("--unsub-ids", default="", help="Ignored (compat)")
    ap.add_argument("--approve-lines", default="", help="Ignored (compat)")
    ap.add_argument(
        "--from-verify",
        default="",
        help="Path to verify JSON (uses slack_text)",
    )
    ap.add_argument("--session-key", default="")
    args = ap.parse_args(argv)

    text = args.text
    if args.from_verify:
        data = json.loads(Path(args.from_verify).read_text(encoding="utf-8"))
        text = data.get("slack_text") or text

    if args.digest or args.from_verify:
        if not (text or "").strip():
            return 0
        out = post_digest(args.channel, text)
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
