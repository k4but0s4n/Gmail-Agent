#!/usr/bin/env python3
"""Offline tests: Slack signature, allowlist, batch TTL, mocked approve.

Usage:
  python3 package/scripts/test_slack_interact.py
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
MCP = ROOT / "mcp"
sys.path.insert(0, str(MCP))

os.environ.setdefault("CHROMA_URL", "http://127.0.0.1:9")
os.environ.setdefault("GMAIL_EMBED_URL", "http://127.0.0.1:9")
os.environ.setdefault("GMAIL_RETRIEVE_URL", "http://127.0.0.1:9")


def fail(msg: str) -> None:
    print(f"FAIL: {msg}", file=sys.stderr)
    sys.exit(1)


def ok(msg: str) -> None:
    print(f"ok: {msg}")


def _sign(secret: str, ts: str, body: bytes) -> str:
    basestring = b"v0:" + ts.encode() + b":" + body
    return "v0=" + hmac.new(secret.encode(), basestring, hashlib.sha256).hexdigest()


def main() -> None:
    import gmail_slack_interact as interact  # noqa: E402
    import gmail_slack_post as slack_post  # noqa: E402
    import list_unsubscribe_mcp as unsub  # noqa: E402

    # --- signature ---
    secret = "test_signing_secret"
    body = b"payload=%7B%7D"
    ts = str(int(time.time()))
    sig = _sign(secret, ts, body)
    if not interact.verify_slack_signature(secret, ts, body, sig):
        fail("valid signature rejected")
    ok("signature accept")
    if interact.verify_slack_signature(secret, ts, body, "v0=deadbeef"):
        fail("bad signature accepted")
    ok("signature reject bad hmac")
    if interact.verify_slack_signature(secret, str(int(time.time()) - 10_000), body, sig):
        fail("stale timestamp accepted")
    ok("signature reject skew")
    if interact.verify_slack_signature("", ts, body, sig):
        fail("empty secret accepted")
    ok("signature reject empty secret")

    with tempfile.TemporaryDirectory(prefix="slack-interact-") as tmp:
        state = Path(tmp)
        os.environ["GMAIL_UNSUB_STATE"] = str(state)
        # Re-bind module paths after env change
        unsub.STATE_DIR = state
        unsub.DRAFT_BATCHES_DIR = state / "unsub_draft_batches"
        unsub.PENDING_FILE = state / "unsubscribe_pending.json"

        # --- batch save/load + TTL ---
        bid = unsub.save_unsub_draft_batch(
            ["abc123", "def456"],
            {"session_key": "sess-1", "channel": "CCHAN"},
        )
        loaded = unsub.load_unsub_draft_batch(bid)
        if not loaded or loaded.get("ids") != ["abc123", "def456"]:
            fail(f"batch load mismatch: {loaded}")
        if loaded.get("session_key") != "sess-1" or loaded.get("channel") != "CCHAN":
            fail(f"batch meta mismatch: {loaded}")
        ok("batch save/load")

        if unsub.load_unsub_draft_batch("not-a-valid-id!!"):
            fail("invalid batch id loaded")
        ok("batch reject bad id")

        # Expire by rewriting created_ts
        path = unsub.draft_batches_dir() / f"{bid}.json"
        data = json.loads(path.read_text())
        data["created_ts"] = time.time() - (unsub.DRAFT_BATCH_TTL_SECONDS + 10)
        path.write_text(json.dumps(data))
        if unsub.load_unsub_draft_batch(bid) is not None:
            fail("expired batch still loadable")
        if path.exists():
            fail("expired batch file not deleted")
        ok("batch TTL cleanup")

        # Fresh batch for approve path
        bid2 = unsub.save_unsub_draft_batch(["p1", "p2"], {"channel": "CCHAN"})

        # --- Block Kit helper ---
        blocks = slack_post.build_approve_blocks("*Digest*", bid2)
        actions = [b for b in blocks if b.get("type") == "actions"]
        if not actions or actions[0]["elements"][0]["value"] != bid2:
            fail(f"blocks missing approve value: {blocks}")
        if actions[0]["elements"][0]["action_id"] != slack_post.ACTION_ID:
            fail("wrong action_id")
        ok("approve blocks")

        # --- allowlist fail-closed ---
        os.environ["GMAIL_SLACK_APPROVE_USERS"] = ""
        os.environ["GMAIL_SLACK_CHANNEL"] = "CCHAN"
        payload = {
            "type": "block_actions",
            "user": {"id": "UUSER"},
            "channel": {"id": "CCHAN"},
            "response_url": "https://hooks.slack.test/response",
            "actions": [{"action_id": interact.ACTION_ID, "value": bid2}],
        }
        status, body_json, bg = interact.process_payload(payload, approve_fn=lambda ids: {"ok": True, "results": []})
        if status != 200 or not body_json or "fail-closed" not in (body_json.get("text") or ""):
            fail(f"empty allowlist should deny: {body_json}")
        if bg is not None:
            fail("deny should not schedule background approve")
        ok("allowlist fail-closed")

        # --- user not allowlisted ---
        os.environ["GMAIL_SLACK_APPROVE_USERS"] = "UALLOW"
        status, body_json, bg = interact.process_payload(payload, approve_fn=lambda ids: {"ok": True, "results": []})
        if "not allowlisted" not in (body_json or {}).get("text", ""):
            fail(f"unexpected deny text: {body_json}")
        ok("allowlist deny stranger")

        # --- channel mismatch ---
        payload["user"] = {"id": "UALLOW"}
        payload["channel"] = {"id": "CWRONG"}
        status, body_json, bg = interact.process_payload(payload, approve_fn=lambda ids: {"ok": True, "results": []})
        if "wrong channel" not in (body_json or {}).get("text", ""):
            fail(f"channel mismatch not denied: {body_json}")
        ok("channel mismatch deny")

        # --- approve mocked (no network) ---
        payload["channel"] = {"id": "CCHAN"}
        called: list[list[str]] = []

        def fake_approve(ids: list[str]) -> dict:
            called.append(list(ids))
            return {
                "ok": True,
                "results": [
                    {"id": "p1", "ok": True},
                    {"id": "p2", "ok": False, "error": "not auto-executable"},
                ],
            }

        with mock.patch.object(interact, "post_response_url", return_value={"ok": True}) as post_mock:
            status, body_json, bg = interact.process_payload(payload, approve_fn=fake_approve)
            if status != 200 or body_json is not None or bg is None:
                fail(f"approve path should ack empty + background: {status} {body_json} {bg}")
            bg()
            if called != [["p1", "p2"]]:
                fail(f"approve ids wrong: {called}")
            if not post_mock.called:
                fail("response_url not posted")
            args = post_mock.call_args[0]
            reply = args[1] if len(args) > 1 else post_mock.call_args[1].get("payload")
            text = (reply or {}).get("text") or ""
            if "1 ok" not in text or "1 failed" not in text:
                fail(f"bad summary text: {text}")
            if "http" in text.lower() and "hooks.slack" in text:
                fail("response leaked url into text")
        ok("approve mocked + response_url summary")

        # --- summarize never dumps targets ---
        summary = interact.summarize_approve(
            {
                "results": [
                    {
                        "id": "x1",
                        "ok": False,
                        "error": "boom",
                        "target": "https://evil.example/unsub?token=SECRET",
                    }
                ]
            }
        )
        if "SECRET" in summary or "evil.example" in summary:
            fail(f"summary leaked target: {summary}")
        ok("summary omits targets")

    print("\nALL PASS — slack interact / batch / allowlist")


if __name__ == "__main__":
    main()
