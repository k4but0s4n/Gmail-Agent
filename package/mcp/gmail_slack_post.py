#!/usr/bin/env python3
"""Shared Slack chat.postMessage helper for triage digests (Block Kit Approve).

Used by gmail_triage_2h.sh / gmail_e2e_200.sh. Stdlib only.

Approve button modes (first match wins):
  1) Signed link — GMAIL_SLACK_APPROVE_PUBLIC_BASE + GMAIL_SLACK_APPROVE_LINK_SECRET
     (Tailscale Funnel / reverse-proxy; no Slack signing secret required)
  2) Slack Interactivity action button — needs GMAIL_SLACK_SIGNING_SECRET + Request URL
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import _config as cfg

ACTION_ID = "gmail_unsub_approve"
DEFAULT_LINK_TTL_SEC = 7 * 24 * 3600


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def approve_link_secret() -> str:
    return _env("GMAIL_SLACK_APPROVE_LINK_SECRET")


def approve_public_base() -> str:
    return _env("GMAIL_SLACK_APPROVE_PUBLIC_BASE").rstrip("/")


def link_mode_enabled() -> bool:
    return bool(approve_link_secret() and approve_public_base())


def sign_approve_link(batch_id: str, *, ttl_sec: int = DEFAULT_LINK_TTL_SEC) -> str:
    """Return absolute HTTPS URL for click-to-approve (confirm page)."""
    secret = approve_link_secret()
    base = approve_public_base()
    if not secret or not base or not batch_id:
        return ""
    exp = str(int(time.time()) + max(60, int(ttl_sec)))
    msg = f"{batch_id}:{exp}".encode()
    sig = hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()
    q = urllib.parse.urlencode({"b": batch_id, "e": exp, "s": sig})
    return f"{base}/slack/approve?{q}"


def verify_approve_link(batch_id: str, exp: str, sig: str) -> bool:
    secret = approve_link_secret()
    if not secret or not batch_id or not exp or not sig:
        return False
    try:
        exp_i = int(exp)
    except (TypeError, ValueError):
        return False
    if exp_i < int(time.time()):
        return False
    msg = f"{batch_id}:{exp}".encode()
    expected = hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig.strip().lower())


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


def build_approve_blocks(
    digest_text: str,
    batch_id: str,
    *,
    approve_lines: list[str] | None = None,
) -> list:
    """Block Kit: digest → what will be approved → Approve button (no thread draft)."""
    body = (digest_text or "").strip()
    if len(body) > 2600:
        body = body[:2600].rstrip() + "…"

    lines = [ln for ln in (approve_lines or []) if ln]
    if not lines:
        lines = ["• _(pending ids for this batch)_"]
    approve_body = "*Approve will unsubscribe:*\n" + "\n".join(lines[:20])
    if len(approve_body) > 2800:
        approve_body = approve_body[:2800].rstrip() + "…"

    blocks: list = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": body or "_Triage digest_"},
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": approve_body},
        },
    ]

    link = sign_approve_link(batch_id) if link_mode_enabled() else ""
    if link:
        btn = {
            "type": "button",
            "text": {"type": "plain_text", "text": "Approve these unsubs", "emoji": False},
            "style": "primary",
            "url": link,
        }
        ctx = "Opens confirm page · then posts Unsub confirmation in this channel"
    else:
        btn = {
            "type": "button",
            "action_id": ACTION_ID,
            "text": {"type": "plain_text", "text": "Approve these unsubs", "emoji": False},
            "style": "primary",
            "value": batch_id,
        }
        ctx = "Allowlisted operators only · Slack Interactivity endpoint must be live"

    blocks.extend(
        [
            {"type": "actions", "elements": [btn]},
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": ctx}],
            },
        ]
    )
    return blocks


def _parse_ids(raw: str) -> list[str]:
    out: list[str] = []
    for part in (raw or "").replace("\n", ",").split(","):
        pid = part.strip()
        if pid and pid not in out:
            out.append(pid)
    return out


def _parse_approve_lines(raw: str) -> list[str]:
    """Newline-separated approve preview lines (from verify JSON)."""
    return [ln.strip() for ln in (raw or "").split("\n") if ln.strip()]


def post_digest(
    channel: str,
    slack_text: str,
    *,
    unsub_draft_text: str = "",  # ignored — kept for CLI compat; no thread post
    unsub_ids: list[str] | None = None,
    approve_lines: list[str] | None = None,
    session_key: str = "",
) -> dict:
    """Post digest with Approve button when ids present. No auto thread reply."""
    del unsub_draft_text  # no Slack thread draft
    ids = [str(x).strip() for x in (unsub_ids or []) if str(x).strip()]
    blocks = None
    batch_id = ""
    if ids:
        import list_unsubscribe_mcp as unsub  # noqa: WPS433

        batch_id = unsub.save_unsub_draft_batch(
            ids,
            {"session_key": session_key, "channel": channel},
        )
        blocks = build_approve_blocks(
            slack_text, batch_id, approve_lines=approve_lines
        )

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
    ap.add_argument("--unsub-draft", default="", help="Ignored (compat; no thread post)")
    ap.add_argument("--unsub-ids", default="", help="Comma-separated pending proposal ids")
    ap.add_argument(
        "--approve-lines",
        default="",
        help="Newline-separated preview lines for Approve section",
    )
    ap.add_argument(
        "--from-verify",
        default="",
        help="Path to verify JSON (uses slack_text, unsub_pending_ids, unsub_approve_lines)",
    )
    ap.add_argument("--session-key", default="")
    args = ap.parse_args(argv)

    if os.environ.get("OPENCLAW_SECRETS"):
        os.environ.setdefault("OPENCLAW_SECRETS", os.environ["OPENCLAW_SECRETS"])

    text = args.text
    unsub_ids = _parse_ids(args.unsub_ids)
    approve_lines = _parse_approve_lines(args.approve_lines)
    session_key = args.session_key
    if args.from_verify:
        data = json.loads(Path(args.from_verify).read_text(encoding="utf-8"))
        text = data.get("slack_text") or text
        unsub_ids = list(data.get("unsub_pending_ids") or unsub_ids)
        approve_lines = list(data.get("unsub_approve_lines") or approve_lines)
        session_key = data.get("session_key") or session_key

    if args.digest or unsub_ids or args.from_verify:
        if not (text or "").strip():
            return 0
        out = post_digest(
            args.channel,
            text,
            unsub_draft_text="",
            unsub_ids=unsub_ids,
            approve_lines=approve_lines,
            session_key=session_key,
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
