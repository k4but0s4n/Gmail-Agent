#!/usr/bin/env python3
"""Regression: verify detects leaked tool_call and builds slack_text for empty inbox."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERIFY = ROOT / "mcp" / "gmail_e2e_verify_batch.py"


def run_verify(session_file: Path, key: str = "test-session") -> tuple[int, dict]:
    env = os.environ.copy()
    env.setdefault("CHROMA_URL", "http://127.0.0.1:9")
    env.setdefault("GMAIL_EMBED_URL", "http://127.0.0.1:9")
    env.setdefault("GMAIL_RETRIEVE_URL", "http://127.0.0.1:9")
    r = subprocess.run(
        [
            sys.executable,
            str(VERIFY),
            "--session-key",
            key,
            "--session-file",
            str(session_file),
            "--no-apply-orphan",
        ],
        capture_output=True,
        text=True,
        env=env,
    )
    try:
        data = json.loads(r.stdout)
    except Exception:
        print("STDOUT:", r.stdout)
        print("STDERR:", r.stderr)
        raise
    return r.returncode, data


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="verify-leak-") as tmp:
        tmp_p = Path(tmp)

        # 1) Leaked tool_call as assistant text — must FAIL + retry
        leak = tmp_p / "leak.jsonl"
        write_jsonl(
            leak,
            [
                {
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "text",
                                "text": (
                                    'id: "email_query__list_recent"\n'
                                    "  args:\n"
                                    "    limit: 25\n"
                                    "    offset: 0\n"
                                    "    unread_only: true\n"
                                    "    skip_labeled: true\n"
                                    "    skip_seen: true\n"
                                    "    since_today: true\n"
                                ),
                            }
                        ],
                    }
                }
            ],
        )
        code, data = run_verify(leak)
        assert code == 1, data
        assert data.get("error") == "tool_call_leaked_not_executed", data
        assert data.get("retry") is True, data
        assert data.get("tool_call_leaked") is True, data
        print("ok: leaked tool_call → fail+retry")

        # 2) Real empty list_recent — must OK + slack_text
        empty = tmp_p / "empty.jsonl"
        write_jsonl(
            empty,
            [
                {
                    "message": {
                        "role": "toolResult",
                        "content": [
                            {
                                "type": "text",
                                "text": "Listed 0 emails (eligible_total=0).",
                            }
                        ],
                    }
                }
            ],
        )
        code, data = run_verify(empty)
        assert code == 0, data
        assert data.get("ok") is True, data
        assert data.get("listed_count") == 0, data
        assert "0 messages" in (data.get("slack_text") or ""), data
        print("ok: genuine empty list_recent → ok + slack_text")

        # 3) Finalize ok with items — slack_text has ACTION bullet
        fin = tmp_p / "fin.jsonl"
        summary = {
            "ok": True,
            "total": 1,
            "counts": {
                "URGENT": 0,
                "ACTION-REQUIRED": 1,
                "FYI": 0,
                "SOCIAL": 0,
                "NEWSLETTER": 0,
                "SPAM": 0,
            },
            "labels_applied": 1,
            "marked_read": 0,
            "unsub_queued_count": 0,
            "label_failures": [],
        }
        write_jsonl(
            fin,
            [
                {
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "toolCall",
                                "name": "tool_call",
                                "arguments": {
                                    "id": "gmail_triage_ops__finalize_triage",
                                    "args": {
                                        "items": [
                                            {
                                                "message_id": "abc123",
                                                "category": "ACTION-REQUIRED",
                                                "from": "A <a@b.com>",
                                                "subject": "Please review",
                                            }
                                        ]
                                    },
                                },
                            }
                        ],
                    }
                },
                {
                    "message": {
                        "role": "toolResult",
                        "content": [
                            {
                                "type": "text",
                                "text": "Listed 1 email (eligible_total=1).\n"
                                + json.dumps(summary),
                            }
                        ],
                    }
                },
            ],
        )
        code, data = run_verify(fin)
        assert code == 0, data
        assert data.get("ok") is True, data
        st = data.get("slack_text") or ""
        assert "ACTION-REQUIRED" in st and "abc123" in st, st
        print("ok: finalize success → slack_text with ACTION")

    print("\nALL PASS — verify tool-leak regression")


if __name__ == "__main__":
    main()
