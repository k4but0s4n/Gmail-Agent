#!/usr/bin/env python3
"""Live e2e on an OpenClaw host: offline checks + backfill watch + Gmail finalize.

Run from the MCP bin dir (or with that dir on PYTHONPATH):

  cd ~/.openclaw/bin && python3 e2e_post_unsub_live.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path


def offline() -> None:
    print("=== OFFLINE ===")
    with tempfile.TemporaryDirectory(prefix="post-unsub-") as tmp:
        os.environ["GMAIL_UNSUB_STATE"] = tmp
        os.environ["GMAIL_POST_UNSUB_WATCH"] = "1"
        os.environ["GMAIL_POST_UNSUB_GRACE_DAYS"] = "3"
        os.environ["GMAIL_POST_UNSUB_SCOPE"] = "email"
        os.environ["GMAIL_POST_UNSUB_DOMAIN_AFTER_HITS"] = "2"
        os.environ["GMAIL_MARK_POST_UNSUB_SPAM_READ"] = "1"
        for n in ("list_unsubscribe_mcp", "_config", "gmail_triage_ops_mcp"):
            sys.modules.pop(n, None)
        import list_unsubscribe_mcp as unsub
        import gmail_triage_ops_mcp as triage

        unsub.add_post_unsub_watch(
            "News <news@brand.example>", grace_days=3, approved_at_epoch=time.time()
        )
        matched, _ = unsub.match_post_unsub_watch(
            "News <news@brand.example>", require_past_grace=True
        )
        assert not matched, "grace should block"
        data = unsub.load_watch()
        data["entries"]["news@brand.example"]["approved_at"] = time.strftime(
            "%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - 5 * 86400)
        )
        unsub.save_watch(data)
        items = [
            {
                "message_id": "m1",
                "category": "NEWSLETTER",
                "from": "News <news@brand.example>",
                "subject": "x",
            },
            {
                "message_id": "m2",
                "category": "FYI",
                "from": "Friend <friend@other.example>",
                "subject": "y",
            },
        ]
        ov = triage.apply_post_unsub_overrides(items, unsub, record_hits=True)
        assert len(ov) == 1 and items[0]["category"] == "SPAM"
        assert items[1]["category"] == "FYI"
        unsub.record_post_unsub_hit("Promo <promo@brand.example>")
        keys = {e["key"] for e in unsub.list_post_unsub_watch()["entries"]}
        assert "brand.example" in keys, keys
        print("OFFLINE PASS")


def live() -> None:
    print("=== LIVE BACKFILL + FINALIZE ===")
    state = Path.home() / ".openclaw" / "gmail"
    os.environ["GMAIL_UNSUB_STATE"] = str(state)
    os.environ["GMAIL_POST_UNSUB_WATCH"] = "1"
    for n in ("list_unsubscribe_mcp", "_config", "gmail_triage_ops_mcp"):
        sys.modules.pop(n, None)
    import list_unsubscribe_mcp as unsub
    import gmail_triage_ops_mcp as triage

    pending = unsub.load_pending()
    added = []
    for pid, item in (pending.get("items") or {}).items():
        if item.get("status") != "done":
            continue
        from_hdr = item.get("from") or ""
        if not from_hdr:
            continue
        entry = unsub.add_post_unsub_watch(
            from_hdr,
            pending_id=pid,
            message_id=item.get("message_id"),
            method=item.get("method"),
            approved_at=item.get("approved_ts") or item.get("ts"),
            grace_days=3,
            scope="email",
        )
        if entry:
            added.append(entry["key"])

    watch = unsub.list_post_unsub_watch()
    print(
        json.dumps(
            {
                "backfilled": len(added),
                "watch_count": watch["count"],
                "past_grace": sum(1 for e in watch["entries"] if e.get("past_grace")),
                "sample": watch["entries"][:3],
            },
            indent=2,
        )
    )

    candidates = list(watch["entries"])
    if not candidates:
        raise SystemExit("no watches after backfill")

    prefer = ["libertymutual", "uber", "infoilmeteo", "alstspecials", "suffolk", "lenscrafters"]
    ordered = sorted(
        candidates,
        key=lambda e: (
            0 if any(p in (e.get("key") or "") for p in prefer) else 1,
            e.get("key") or "",
        ),
    )

    msg = None
    chosen_entry = None
    for entry in ordered[:15]:
        key = entry["key"]
        q = f"from:{key} newer_than:21d"
        try:
            data = unsub.gmail_get("messages", {"q": q, "maxResults": 5})
        except Exception as exc:
            print(f"search fail {key}: {exc}")
            continue
        mids = [m["id"] for m in (data.get("messages") or [])]
        if not mids:
            print(f"no recent mail for {key}")
            continue
        orig = entry.get("message_id")
        mid = next((m for m in mids if m != orig), mids[0])
        meta = unsub.fetch_headers(mid)
        from_hdr = meta.get("from") or ""
        subject = meta.get("subject") or ""
        print(
            "candidate:",
            json.dumps(
                {
                    "watch": key,
                    "message_id": mid,
                    "from": from_hdr[:120],
                    "subject": subject[:100],
                    "past_grace_before_force": bool(entry.get("past_grace")),
                },
                indent=2,
            ),
        )
        msg = {
            "id": mid,
            "from": from_hdr,
            "subject": subject,
            "watch": key,
        }
        chosen_entry = entry
        break

    if not msg or not chosen_entry:
        raise SystemExit(
            "no Gmail messages found for watched senders in last 21d — cannot live finalize"
        )

    # Approvals from the last 1–2 days are still inside the default 3-day grace.
    # Force this one entry past grace for e2e (keeps real approved_at history otherwise).
    data = unsub.load_watch()
    key = chosen_entry["key"]
    if key in data["entries"]:
        data["entries"][key]["approved_at"] = time.strftime(
            "%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - 5 * 86400)
        )
        data["entries"][key]["e2e_forced_past_grace"] = True
        unsub.save_watch(data)
        print(f"forced past grace for e2e: {key}")

    preview_items = [
        {
            "message_id": msg["id"],
            "category": "NEWSLETTER",
            "from": msg["from"] or "",
            "subject": (msg["subject"] or "")[:160],
        }
    ]
    items_preview = [dict(preview_items[0])]
    ov = triage.apply_post_unsub_overrides(items_preview, unsub, record_hits=False)
    print(
        "PREVIEW:",
        json.dumps({"overrides": ov, "category": items_preview[0]["category"]}, indent=2),
    )
    if not ov or items_preview[0]["category"] != "SPAM":
        raise SystemExit("preview did not override to SPAM — abort live finalize")

    print("=== LIVE FINALIZE (applies OC/SPAM + mark read) ===")
    summary = triage.finalize_triage(preview_items)
    out = {
        "ok": summary.get("ok"),
        "counts": summary.get("counts"),
        "post_unsub_override_count": summary.get("post_unsub_override_count"),
        "post_unsub_overrides": summary.get("post_unsub_overrides"),
        "labels_applied": summary.get("labels_applied"),
        "marked_read": summary.get("marked_read"),
        "unsub_skipped_count": summary.get("unsub_skipped_count"),
        "unsub_skipped": [
            {
                k: u.get(k)
                for k in ("message_id", "reason", "skipped", "suppressed_key", "note")
            }
            for u in (summary.get("unsub_skipped") or [])
        ],
        "label_failures": summary.get("label_failures"),
    }
    print(json.dumps(out, indent=2))
    if not summary.get("ok") or summary.get("post_unsub_override_count", 0) < 1:
        raise SystemExit("LIVE FINALIZE FAILED")
    print("LIVE PASS")


def main() -> None:
    # Ensure bin dir imports resolve when script lives beside MCP modules.
    here = Path(__file__).resolve().parent
    if str(here) not in sys.path:
        sys.path.insert(0, str(here))
    offline()
    live()
    print("\nALL PASS — offline + live post-unsub e2e")


if __name__ == "__main__":
    main()
