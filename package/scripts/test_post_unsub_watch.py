#!/usr/bin/env python3
"""Offline e2e for post-unsub watch → SPAM override (no Gmail / network).

Usage:
  python3 package/scripts/test_post_unsub_watch.py

Sets a temp GMAIL_UNSUB_STATE, seeds watches, and asserts grace / override /
domain promotion behavior via the MCP modules' pure helpers + preview CLI.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MCP = ROOT / "mcp"
UNSUB = MCP / "list_unsubscribe_mcp.py"
TRIAGE = MCP / "gmail_triage_ops_mcp.py"


def fail(msg: str) -> None:
    print(f"FAIL: {msg}", file=sys.stderr)
    sys.exit(1)


def ok(msg: str) -> None:
    print(f"ok: {msg}")


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="gmail-post-unsub-") as tmp:
        state = Path(tmp)
        env = os.environ.copy()
        env["GMAIL_UNSUB_STATE"] = str(state)
        env["GMAIL_POST_UNSUB_WATCH"] = "1"
        env["GMAIL_POST_UNSUB_GRACE_DAYS"] = "3"
        env["GMAIL_POST_UNSUB_SCOPE"] = "email"
        env["GMAIL_POST_UNSUB_DOMAIN_AFTER_HITS"] = "2"
        env["GMAIL_MARK_POST_UNSUB_SPAM_READ"] = "1"
        # Avoid accidental imports needing live RAG URLs
        env.setdefault("CHROMA_URL", "http://127.0.0.1:9")
        env.setdefault("GMAIL_EMBED_URL", "http://127.0.0.1:9")
        env.setdefault("GMAIL_RETRIEVE_URL", "http://127.0.0.1:9")

        # Import modules with isolated state
        sys.path.insert(0, str(MCP))
        # Force re-import clean
        for name in list(sys.modules):
            if name in {"list_unsubscribe_mcp", "_config", "gmail_triage_ops_mcp"}:
                del sys.modules[name]
        os.environ.update({k: env[k] for k in env if k.startswith("GMAIL_") or k.startswith("CHROMA")})
        import list_unsubscribe_mcp as unsub  # noqa: E402
        import gmail_triage_ops_mcp as triage  # noqa: E402

        # --- 1) Seed watch inside grace (days-ago=0) ---
        entry = unsub.add_post_unsub_watch(
            "News <news@brand.example>",
            pending_id="p1",
            message_id="m1",
            grace_days=3,
            approved_at_epoch=time.time(),
            scope="email",
        )
        if not entry or entry.get("key") != "news@brand.example":
            fail(f"watch-add failed: {entry}")
        ok("seeded email watch")

        matched, _ = unsub.match_post_unsub_watch(
            "News <news@brand.example>", require_past_grace=True
        )
        if matched:
            fail("should NOT match during grace")
        ok("grace window blocks override")

        matched, _ = unsub.match_post_unsub_watch(
            "News <news@brand.example>", require_past_grace=False
        )
        if not matched:
            fail("should match when grace not required")
        ok("watch present (ignore grace)")

        # --- 2) Backdate approved_at past grace ---
        data = unsub.load_watch()
        data["entries"]["news@brand.example"]["approved_at"] = time.strftime(
            "%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - 5 * 86400)
        )
        unsub.save_watch(data)

        matched, entry = unsub.match_post_unsub_watch(
            "News <news@brand.example>", require_past_grace=True
        )
        if not matched:
            fail("should match after grace")
        ok("past grace matches")

        # --- 3) Preview override via triage helper ---
        items = [
            {
                "message_id": "msg-recidivist",
                "category": "NEWSLETTER",
                "from": "News <news@brand.example>",
                "subject": "Still here",
            },
            {
                "message_id": "msg-other",
                "category": "FYI",
                "from": "Friend <friend@other.example>",
                "subject": "hi",
            },
        ]
        overrides = triage.apply_post_unsub_overrides(items, unsub, record_hits=True)
        if len(overrides) != 1:
            fail(f"expected 1 override, got {overrides}")
        if items[0]["category"] != "SPAM":
            fail(f"expected SPAM, got {items[0]['category']}")
        if items[1]["category"] != "FYI":
            fail("unrelated item mutated")
        if not items[0].get("force_mark_read"):
            fail("expected force_mark_read on override")
        ok("finalize override → SPAM + mark-read flag")

        # --- 4) Domain promotion after 2 distinct emails ---
        unsub.record_post_unsub_hit("Promo <promo@brand.example>")
        watch = unsub.list_post_unsub_watch()
        keys = {e["key"] for e in watch["entries"]}
        if "brand.example" not in keys:
            fail(f"expected domain promotion, keys={keys}")
        ok("promoted to domain after 2 emails")

        matched, entry = unsub.match_post_unsub_watch(
            "Alerts <alerts@brand.example>", require_past_grace=True
        )
        if not matched or (entry or {}).get("scope") != "domain":
            fail(f"domain match failed: {entry}")
        ok("domain-scoped watch matches new address")

        # --- 5) CLI round-trip (subprocess, fresh state dir) ---
        with tempfile.TemporaryDirectory(prefix="gmail-post-unsub-cli-") as tmp2:
            env2 = env.copy()
            env2["GMAIL_UNSUB_STATE"] = tmp2
            r = subprocess.run(
                [
                    sys.executable,
                    str(UNSUB),
                    "--watch-add",
                    "cli@example.com",
                    "--grace",
                    "1",
                    "--days-ago",
                    "2",
                ],
                env=env2,
                capture_output=True,
                text=True,
                check=False,
            )
            if r.returncode != 0:
                fail(f"watch-add CLI failed: {r.stderr}\n{r.stdout}")
            payload = json.loads(r.stdout)
            if not payload.get("ok"):
                fail(f"watch-add CLI not ok: {payload}")

            items_path = Path(tmp2) / "items.json"
            items_path.write_text(
                json.dumps(
                    {
                        "items": [
                            {
                                "message_id": "cli-1",
                                "category": "NEWSLETTER",
                                "from": "cli@example.com",
                                "subject": "ping",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            r2 = subprocess.run(
                [sys.executable, str(TRIAGE), "--preview-post-unsub-json", str(items_path)],
                env=env2,
                capture_output=True,
                text=True,
                check=False,
            )
            if r2.returncode != 0:
                fail(f"preview CLI failed: {r2.stderr}\n{r2.stdout}")
            preview = json.loads(r2.stdout)
            if preview.get("post_unsub_override_count") != 1:
                fail(f"preview expected 1 override: {preview}")
            if preview["items"][0]["category"] != "SPAM":
                fail(f"preview category: {preview['items'][0]}")
            ok("CLI --watch-add + --preview-post-unsub-json")

        print("\nALL PASS — post-unsub watch e2e (offline)")
        print(f"state dir used: {state}")
        print(
            "\nLive next steps (optional):\n"
            "  1) Deploy package MCP to OpenClaw bin\n"
            "  2) python3 list_unsubscribe_mcp.py --watch   # after a real --approve\n"
            "  3) Or seed: --watch-add 'News <x@y.com>' --days-ago 5\n"
            "  4) Run triage / finalize on a matching message; expect OC/SPAM + "
            "post_unsub_override_count>=1\n"
            "  5) Decide grace/scope knobs, then commit + push"
        )


if __name__ == "__main__":
    main()
