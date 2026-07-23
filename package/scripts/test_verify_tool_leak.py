#!/usr/bin/env python3
"""Regression: verify detects leaked tool_call and builds slack_text for empty inbox."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
# Repo layout: package/scripts → package/mcp; flat deploy: ~/.openclaw/bin/*.py
_VERIFY_CANDIDATES = (
    ROOT / "mcp" / "gmail_e2e_verify_batch.py",
    HERE / "gmail_e2e_verify_batch.py",
)
VERIFY = next((p for p in _VERIFY_CANDIDATES if p.exists()), _VERIFY_CANDIDATES[0])


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

        # 2) Real empty list_recent — must OK, no Slack digest
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
        assert not (data.get("slack_text") or "").strip(), data
        print("ok: genuine empty list_recent → ok + no slack_text")

        # 3) Compact finalize items + list_recent meta → From · Subject bullets
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
        listed = (
            "Listed 1 email(s) (offset=0, eligible_total=1, newest-first)\n"
            "\n[1]\n"
            "    from:    Chris Shoulet <chris@example.com>\n"
            "    subject: Cyber sales networking question\n"
            "    date:    Wed, 22 Jul 2026 12:00:00 -0400\n"
            "    msg_id:  abc123\n"
        )
        # OpenClaw wraps MCP tool text in a JSON envelope
        listed_envelope = json.dumps(
            {"tool": {"name": "email_query__list_recent"}, "result": {"content": [{"type": "text", "text": listed}]}}
        )
        write_jsonl(
            fin,
            [
                {
                    "message": {
                        "role": "toolResult",
                        "content": [{"type": "text", "text": listed_envelope}],
                    }
                },
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
                                "text": json.dumps(summary),
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
        assert "Chris Shoulet" in st and "Cyber sales networking question" in st, st
        print("ok: finalize success → slack_text with From · Subject")

        # 4) NEWSLETTER digest: both IDs, open-total, already-in-queue, unsub draft
        news = tmp_p / "news.jsonl"
        news_summary = {
            "ok": True,
            "total": 2,
            "counts": {
                "URGENT": 0,
                "ACTION-REQUIRED": 0,
                "FYI": 0,
                "SOCIAL": 0,
                "NEWSLETTER": 2,
                "SPAM": 0,
            },
            "labels_applied": 2,
            "marked_read": 2,
            "unsub_queued_count": 1,
            "pending_open_total": 7,
            "label_failures": [],
            "unsub_queued": [
                {
                    "id": "a5b1c2d3e4f5",
                    "message_id": "msg_new",
                    "category": "NEWSLETTER",
                    "from": "Promo <promo@example.com>",
                    "subject": "This week only",
                }
            ],
            "unsub_already_queued": [
                {
                    "id": "3ef612345678",
                    "message_id": "msg_old",
                    "category": "NEWSLETTER",
                    "from": "Digest <digest@example.com>",
                    "subject": "Already queued digest",
                }
            ],
            "unsub_already_done": [],
        }
        listed_news = (
            "Listed 2 email(s) (offset=0, eligible_total=2, newest-first)\n"
            "\n[1]\n"
            "    from:    Promo <promo@example.com>\n"
            "    subject: This week only\n"
            "    date:    Wed, 22 Jul 2026 12:00:00 -0400\n"
            "    msg_id:  msg_new\n"
            "\n[2]\n"
            "    from:    Digest <digest@example.com>\n"
            "    subject: Already queued digest\n"
            "    date:    Wed, 22 Jul 2026 11:00:00 -0400\n"
            "    msg_id:  msg_old\n"
        )
        listed_news_env = json.dumps(
            {
                "tool": {"name": "email_query__list_recent"},
                "result": {"content": [{"type": "text", "text": listed_news}]},
            }
        )
        write_jsonl(
            news,
            [
                {
                    "message": {
                        "role": "toolResult",
                        "content": [{"type": "text", "text": listed_news_env}],
                    }
                },
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
                                            {"message_id": "msg_new", "category": "NEWSLETTER"},
                                            {"message_id": "msg_old", "category": "NEWSLETTER"},
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
                        "content": [{"type": "text", "text": json.dumps(news_summary)}],
                    }
                },
            ],
        )
        code, data = run_verify(news)
        assert code == 0, data
        st = data.get("slack_text") or ""
        assert "Unsub queued (this batch): 1 · Open pending total: 7" in st, st
        assert "`msg_new` · pending:`a5b1c2d3e4f5`" in st, st
        assert "`msg_old` · pending:`3ef612345678`" in st, st
        assert "_(already in queue)_" in st, st
        assert "*Unsub draft*" in st and "--approve" in st, st
        draft = data.get("unsub_draft_text") or ""
        assert "a5b1c2d3e4f5" in draft and "3ef612345678" in draft, draft
        assert "CLI approve only" in draft, draft
        print("ok: newsletter digest → pending ids + open-total + already-in-queue + draft")

    print("\nALL PASS — verify tool-leak regression")


if __name__ == "__main__":
    main()
