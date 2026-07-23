#!/usr/bin/env python3
"""Regression: list_pending limit=0 must return an empty page (not default 50)."""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("CHROMA_URL", "http://127.0.0.1:9")
os.environ.setdefault("GMAIL_EMBED_URL", "http://127.0.0.1:9")
os.environ.setdefault("GMAIL_RETRIEVE_URL", "http://127.0.0.1:9")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "mcp"))
import list_unsubscribe_mcp as unsub  # noqa: E402


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="list-pending-limit-"))
    unsub.PENDING_FILE = tmp / "unsubscribe_pending.json"
    unsub.save_pending(
        {
            "items": {
                "a": {"id": "a", "status": "pending", "ts": "2026-01-02"},
                "b": {"id": "b", "status": "approved", "ts": "2026-01-03"},
                "c": {"id": "c", "status": "needs_manual", "ts": "2026-01-04"},
            }
        }
    )
    r0 = unsub.list_pending(limit=0)
    assert r0["count"] == 2 and r0["returned"] == 0 and r0["items"] == [], r0
    r1 = unsub.list_pending(limit=1)
    assert r1["count"] == 2 and r1["returned"] == 1 and r1["items"][0]["id"] == "c", r1
    print("ok: list_pending limit=0 → empty page; limit=1 → newest open only")
    print("\nALL PASS — list_pending limit regression")


if __name__ == "__main__":
    main()
