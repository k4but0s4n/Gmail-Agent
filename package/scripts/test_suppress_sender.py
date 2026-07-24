#!/usr/bin/env python3
"""Offline test: suppress domain/email dismisses matching pending.

Usage:
  python3 package/scripts/test_suppress_sender.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

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


def main() -> None:
    import list_unsubscribe_mcp as u

    with tempfile.TemporaryDirectory(prefix="suppress-") as tmp:
        state = Path(tmp)
        os.environ["GMAIL_UNSUB_STATE"] = str(state)
        u.STATE_DIR = state
        u.PENDING_FILE = state / "unsubscribe_pending.json"
        u.SUPPRESSED_FILE = state / "unsubscribe_suppressed_senders.json"
        u.SEEN_FILE = state / "unsubscribe_seen.json"
        u.LOG_FILE = state / "unsubscribe_log.jsonl"
        u.WATCH_FILE = state / "unsubscribe_watch.json"

        pending = {
            "items": {
                "aaa111aaa111": {
                    "id": "aaa111aaa111",
                    "status": "pending",
                    "from": "Jobs <jobalerts-noreply@linkedin.com>",
                    "message_id": "msg1",
                },
                "bbb222bbb222": {
                    "id": "bbb222bbb222",
                    "status": "pending",
                    "from": "Other <alerts@linkedin.com>",
                    "message_id": "msg2",
                },
                "ccc333ccc333": {
                    "id": "ccc333ccc333",
                    "status": "pending",
                    "from": "Shop <promo@example.com>",
                    "message_id": "msg3",
                },
            }
        }
        u.save_pending(pending)

        out = u.suppress_sender("linkedin.com", scope="domain")
        if not out.get("ok"):
            fail(f"suppress failed: {out}")
        dismissed = set(out.get("dismissed_ids") or [])
        if dismissed != {"aaa111aaa111", "bbb222bbb222"}:
            fail(f"expected two linkedin dismissals, got {dismissed}")
        ok("suppress domain dismisses matching pending")

        pending2 = u.load_pending()
        if pending2["items"]["aaa111aaa111"]["status"] != "rejected":
            fail("aaa not rejected")
        if pending2["items"]["ccc333ccc333"]["status"] != "pending":
            fail("unrelated pending was dismissed")
        ok("unrelated pending kept")

        if not u.is_sender_suppressed("Jobs <x@linkedin.com>")[0]:
            fail("linkedin.com not suppressed for new proposes")
        ok("future proposes skipped for domain")

        out_email = u.suppress_sender("promo@example.com", scope="email")
        if not out_email.get("ok"):
            fail(f"email suppress failed: {out_email}")
        if "ccc333ccc333" not in (out_email.get("dismissed_ids") or []):
            fail(f"email dismiss missing: {out_email}")
        ok("suppress email dismisses that address only")

        listed = u.list_suppressed_senders()
        keys = {e.get("key") for e in listed.get("entries") or []}
        if "linkedin.com" not in keys or "promo@example.com" not in keys:
            fail(f"suppressed list missing keys: {keys}")
        ok("list suppressed")

        undo = u.unsuppress_sender("linkedin.com")
        if not undo.get("ok"):
            fail(f"unsuppress failed: {undo}")
        if u.is_sender_suppressed("Jobs <x@linkedin.com>")[0]:
            fail("linkedin still suppressed after unsuppress")
        ok("unsuppress domain")

    print("\nALL PASS — suppress sender / dismiss pending")


if __name__ == "__main__":
    main()
