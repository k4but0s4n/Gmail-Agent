#!/usr/bin/env python3
"""Post-batch verifier/recovery for Gmail triage e2e.

Small models sometimes:
- call OpenClaw meta-tool tool_call with args.items but omit finalize id
- echo a tool_call as chat text (never executed) — previously treated as empty inbox

This script:
1) Locates the session for --session-key
2) If finalize succeeded in-session → exit 0 (+ optional slack_text)
3) If a tool_call has items but missing/wrong id → apply finalize_triage directly
4) If tool_call was leaked as text and list_recent never ran → exit 1 (retry signal)
5) Exit 1 if nothing applied and mail was listed
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
from pathlib import Path

import _config as cfg

AGENT_ID = cfg.agent_id()
SESS_DIR = cfg.openclaw_home() / "agents" / AGENT_ID / "sessions"
SESSIONS_INDEX = SESS_DIR / "sessions.json"
TRIAGE_OPS = Path(
    os.environ.get("GMAIL_TRIAGE_OPS_MCP")
    or str(cfg.bin_dir() / "gmail_triage_ops_mcp.py")
)
if not TRIAGE_OPS.exists():
    _sibling = Path(__file__).resolve().parent / "gmail_triage_ops_mcp.py"
    if _sibling.exists():
        TRIAGE_OPS = _sibling
FINALIZE_ID = "gmail_triage_ops__finalize_triage"
LIST_RECENT_ID = "email_query__list_recent"

LEAK_RE = re.compile(
    r'(?:^|\n)\s*(?:tool_call\s*:)?\s*\n?\s*id:\s*["\']?(?:email_query__list_recent|gmail_triage_ops__finalize_triage)',
    re.I | re.M,
)
LEAK_RE_LOOSE = re.compile(
    r'id:\s*["\']email_query__list_recent["\']\s*\n\s*args\s*:',
    re.I,
)


def load_finalize():
    spec = importlib.util.spec_from_file_location("triage_ops", str(TRIAGE_OPS))
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod.finalize_triage


def find_session_file(session_key: str) -> Path | None:
    if SESSIONS_INDEX.exists():
        try:
            idx = json.loads(SESSIONS_INDEX.read_text())
        except Exception:
            idx = {}
        if isinstance(idx, dict):
            for k, v in list(idx.items()):
                if isinstance(v, dict):
                    sk = v.get("sessionKey") or v.get("key") or ""
                    if session_key in str(sk) or session_key in k:
                        for field in ("sessionFile", "file", "path", "id"):
                            if field in v:
                                p = Path(str(v[field]))
                                if not p.is_absolute():
                                    p = SESS_DIR / p
                                if p.suffix != ".jsonl":
                                    p = SESS_DIR / f"{v.get('sessionId', v.get('id', ''))}.jsonl"
                                if p.exists():
                                    return p
                        sid = v.get("sessionId") or v.get("id")
                        if sid:
                            p = SESS_DIR / f"{sid}.jsonl"
                            if p.exists():
                                return p
        elif isinstance(idx, list):
            for v in idx:
                if isinstance(v, dict) and session_key in json.dumps(v):
                    sid = v.get("sessionId") or v.get("id")
                    if sid and (SESS_DIR / f"{sid}.jsonl").exists():
                        return SESS_DIR / f"{sid}.jsonl"

    newest = None
    newest_mtime = -1.0
    for p in SESS_DIR.glob("*.jsonl"):
        if p.name.endswith(".trajectory.jsonl"):
            continue
        try:
            text = p.read_text(errors="ignore")
        except Exception:
            continue
        if session_key in text and p.stat().st_mtime > newest_mtime:
            newest, newest_mtime = p, p.stat().st_mtime
    return newest


def iter_messages(path: Path):
    for line in path.read_text(errors="ignore").splitlines():
        try:
            yield json.loads(line)
        except Exception:
            continue


def _tool_result_ok_true(blob: str):
    if not blob:
        return None
    start = blob.rfind("{")
    if start >= 0:
        chunk = blob[start:]
        for end in range(len(chunk), max(0, len(chunk) - 8000), -1):
            try:
                data = json.loads(chunk[:end])
            except Exception:
                continue
            if isinstance(data, dict) and "ok" in data:
                return data.get("ok") is True
            break
    has_finalize = "labels_applied" in blob or FINALIZE_ID in blob or "Labels applied" in blob
    if not has_finalize:
        return None
    if '"ok": false' in blob or '"ok":false' in blob:
        return False
    if '"ok": true' in blob or '"ok":true' in blob:
        return True
    return None


def _parse_finalize_summary(blob: str) -> dict | None:
    if not blob or "labels_applied" not in blob:
        return None
    start = blob.find("{")
    if start < 0:
        return None
    chunk = blob[start:]
    for end in range(len(chunk), max(0, len(chunk) - 12000), -1):
        try:
            data = json.loads(chunk[:end])
        except Exception:
            continue
        if isinstance(data, dict) and ("labels_applied" in data or "counts" in data):
            return data
        break
    return None


def _assistant_texts(content: list) -> list[str]:
    texts = []
    for c in content:
        if isinstance(c, dict) and c.get("type") == "text":
            texts.append(c.get("text") or "")
        elif isinstance(c, str):
            texts.append(c)
    return texts


def text_looks_like_leaked_tool_call(text: str) -> bool:
    if not text:
        return False
    if LEAK_RE_LOOSE.search(text) or LEAK_RE.search(text):
        return True
    stripped = text.strip()
    if stripped.startswith('id: "email_query__list_recent"') or stripped.startswith(
        "id: 'email_query__list_recent'"
    ):
        return True
    if stripped.startswith('id: "gmail_triage_ops__finalize_triage"'):
        return True
    return False


def parse_listed_message_meta(blob: str) -> dict[str, dict[str, str]]:
    """Parse list_recent tool text into msg_id -> {from, subject}."""
    meta: dict[str, dict[str, str]] = {}
    if not blob or "msg_id:" not in blob:
        return meta
    # Blocks look like:
    # [1]
    #     from:    Name <a@b.com>
    #     subject: Hello
    #     msg_id:  abc123
    for block in re.split(r"\n(?=\[\d+\])", blob):
        mid_m = re.search(r"msg_id:\s*(\S+)", block)
        if not mid_m:
            continue
        mid = mid_m.group(1).strip()
        frm_m = re.search(r"from:\s*(.+)", block)
        sub_m = re.search(r"subject:\s*(.+)", block)
        meta[mid] = {
            "from": (frm_m.group(1).strip() if frm_m else "")[:60],
            "subject": (sub_m.group(1).strip() if sub_m else "")[:80],
        }
    return meta


def enrich_items_with_meta(items: list | None, listed_meta: dict[str, dict[str, str]]) -> list | None:
    if not items:
        return items
    out = []
    for it in items:
        if not isinstance(it, dict):
            out.append(it)
            continue
        row = dict(it)
        mid = str(row.get("message_id") or "")
        meta = listed_meta.get(mid) or {}
        if not row.get("from") and meta.get("from"):
            row["from"] = meta["from"]
        if not row.get("subject") and meta.get("subject"):
            row["subject"] = meta["subject"]
        out.append(row)
    return out


def extract_tool_events(path: Path) -> dict:
    finalize_ok = False
    orphan_items = None
    finalize_items = None
    listed_count = 0
    validation_errors = 0
    list_recent_called = False
    tool_call_leaked = False
    finalize_summary = None
    listed_meta: dict[str, dict[str, str]] = {}

    for o in iter_messages(path):
        msg = o.get("message") or {}
        role = msg.get("role")
        content = msg.get("content")
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        if not isinstance(content, list):
            continue

        if role == "assistant":
            for text in _assistant_texts(content):
                if text_looks_like_leaked_tool_call(text):
                    tool_call_leaked = True
            for c in content:
                if not isinstance(c, dict) or c.get("type") != "toolCall":
                    continue
                name = c.get("name")
                args = c.get("arguments") or {}
                if name == "tool_call" and isinstance(args, dict):
                    tid = args.get("id")
                    inner = args.get("args") if isinstance(args.get("args"), dict) else None
                    if tid == FINALIZE_ID and inner and "items" in inner:
                        orphan_items = inner["items"]
                        finalize_items = inner["items"]
                    elif inner and "items" in inner and not tid:
                        orphan_items = inner["items"]
                        finalize_items = inner["items"]
                elif name == FINALIZE_ID:
                    items = args.get("items") if isinstance(args, dict) else None
                    if items:
                        orphan_items = items
                        finalize_items = items

        if role == "toolResult":
            texts = []
            for c in content:
                if isinstance(c, dict) and c.get("type") == "text":
                    texts.append(c.get("text") or "")
                elif isinstance(c, str):
                    texts.append(c)
            blob = "\n".join(texts)
            # Unwrap OpenClaw MCP envelope: {"tool":..., "result":{"content":[{"text":"..."}]}}
            if blob.lstrip().startswith("{") and '"result"' in blob:
                try:
                    env = json.loads(blob)
                    parts = []
                    for block in (env.get("result") or {}).get("content") or []:
                        if isinstance(block, dict) and isinstance(block.get("text"), str):
                            parts.append(block["text"])
                    if parts:
                        blob = "\n".join(parts)
                except Exception:
                    pass
            if "Validation failed for tool" in blob:
                validation_errors += 1

            listed_meta.update(parse_listed_message_meta(blob))

            m = re.search(r"Listed\s+(\d+)\s+email", blob)
            if m:
                listed_count = max(listed_count, int(m.group(1)))
                list_recent_called = True
            m2 = re.search(r"eligible_total\s*=\s*(\d+)", blob)
            if m2:
                listed_count = max(listed_count, int(m2.group(1)))
                list_recent_called = True
            if "No emails matched filters" in blob or "Listed 0 email" in blob:
                list_recent_called = True

            summary = _parse_finalize_summary(blob)
            if summary and summary.get("ok") is True:
                finalize_ok = True
                finalize_summary = summary
                orphan_items = None
            else:
                parsed_ok = _tool_result_ok_true(blob)
                if parsed_ok is True:
                    finalize_ok = True
                    finalize_summary = summary or finalize_summary
                    orphan_items = None

    return {
        "finalize_ok": finalize_ok,
        "orphan_items": orphan_items,
        "finalize_items": finalize_items,
        "listed_count": listed_count,
        "validation_errors": validation_errors,
        "list_recent_called": list_recent_called,
        "tool_call_leaked": tool_call_leaked and not list_recent_called,
        "finalize_summary": finalize_summary,
        "listed_meta": listed_meta,
    }


def _unsub_pending_ids(summary: dict | None) -> list[str]:
    """Pending proposal ids newly queued or already open for this batch."""
    ids: list[str] = []
    for key in ("unsub_queued", "unsub_already_queued"):
        for u in (summary or {}).get(key) or []:
            if not isinstance(u, dict):
                continue
            pid = str(u.get("id") or "").strip()
            if pid and pid not in ids:
                ids.append(pid)
    return ids


def build_unsub_draft_text(summary: dict | None) -> str:
    """CLI approve draft for Slack thread reply (button is primary; CLI is fallback)."""
    ids = _unsub_pending_ids(summary)
    if not ids:
        return ""
    id_line = " ".join(f"`{i}`" for i in ids)
    return (
        "*Unsub draft* (prefer the digest **Approve these unsubs** button; "
        "CLI fallback — do not paste ids into chat to approve)\n"
        "`python3 $OPENCLAW_HOME/bin/list_unsubscribe_mcp.py --approve <id>…`\n"
        f"ids: {id_line}\n"
    )


def build_slack_text(
    session_key: str,
    *,
    listed_count: int,
    summary: dict | None,
    items: list | None,
) -> str:
    """Short AGENTS-style digest for the runner to post (no agent --deliver)."""
    if listed_count == 0 and not summary:
        return (
            f"*Triage · today · 0 messages*\n"
            f"session: `{session_key}`\n"
            f"_No new unread emails for this batch._"
        )

    counts = (summary or {}).get("counts") or {}
    total = (summary or {}).get("total")
    if total is None:
        total = listed_count if listed_count else sum(int(counts.get(c, 0) or 0) for c in counts)

    unsub_n = int((summary or {}).get("unsub_queued_count", 0) or 0)
    open_total = (summary or {}).get("pending_open_total")
    if open_total is None:
        open_total = "?"

    lines = [
        f"*Triage · today · {total} messages*",
        f"session: `{session_key}`",
        (
            f"URGENT:{counts.get('URGENT', 0)} · ACTION:{counts.get('ACTION-REQUIRED', 0)} · "
            f"FYI:{counts.get('FYI', 0)} · SOCIAL:{counts.get('SOCIAL', 0)} · "
            f"NEWSLETTER:{counts.get('NEWSLETTER', 0)} · SPAM:{counts.get('SPAM', 0)}"
        ),
        f"Unsub queued (this batch): {unsub_n} · Open pending total: {open_total}",
        "",
    ]

    by_cat: dict[str, list] = {}
    for it in items or []:
        if not isinstance(it, dict):
            continue
        cat = str(it.get("category") or "").upper()
        by_cat.setdefault(cat, []).append(it)

    def bullets(cat: str, title: str) -> None:
        rows = by_cat.get(cat) or []
        if not rows:
            return
        lines.append(f"*{title}*")
        for it in rows[:25]:
            mid = it.get("message_id") or "?"
            frm = (it.get("from") or "")[:60]
            sub = (it.get("subject") or "")[:80]
            extra = " · ".join(x for x in (frm, sub) if x)
            if extra:
                lines.append(f"• `{mid}` · {extra}")
            else:
                lines.append(f"• `{mid}`")
        lines.append("")

    bullets("URGENT", "URGENT")
    bullets("ACTION-REQUIRED", "ACTION-REQUIRED")

    # Index pending ids by message_id for NEWSLETTER bullets.
    pending_by_mid: dict[str, str] = {}
    for key in ("unsub_queued", "unsub_already_queued"):
        for u in (summary or {}).get(key) or []:
            if not isinstance(u, dict):
                continue
            mid = str(u.get("message_id") or "").strip()
            pid = str(u.get("id") or "").strip()
            if mid and pid:
                pending_by_mid[mid] = pid

    already_queued_mids = {
        str(u.get("message_id") or "").strip()
        for u in (summary or {}).get("unsub_already_queued") or []
        if isinstance(u, dict) and u.get("message_id")
    }
    already_done_mids = {
        str(u.get("message_id") or "").strip()
        for u in (summary or {}).get("unsub_already_done") or []
        if isinstance(u, dict) and u.get("message_id")
    }

    news = list(by_cat.get("NEWSLETTER") or [])
    if summary:
        seen_mids = {str(it.get("message_id") or "") for it in news}
        for u in (
            (summary.get("unsub_queued") or [])
            + (summary.get("unsub_already_queued") or [])
            + (summary.get("unsub_already_done") or [])
        ):
            if not isinstance(u, dict):
                continue
            if (u.get("category") or "").upper() not in {"", "NEWSLETTER"}:
                continue
            mid = str(u.get("message_id") or "")
            if mid and mid not in seen_mids:
                news.append(u)
                seen_mids.add(mid)

    if news:
        lines.append("*NEWSLETTER* (queued for unsub / marked read)")
        for it in news[:25]:
            mid = str(it.get("message_id") or "?")
            frm = (it.get("from") or "")[:60]
            sub = (it.get("subject") or "")[:80]
            extra = " · ".join(x for x in (frm, sub) if x)
            pid = pending_by_mid.get(mid) or str(it.get("id") or "").strip()
            if mid in already_done_mids and mid not in already_queued_mids and not pid:
                note = " _(already unsubscribed)_"
                head = f"• `{mid}`"
            elif pid:
                note = " _(already in queue)_" if mid in already_queued_mids else ""
                head = f"• `{mid}` · pending:`{pid}`"
            else:
                note = ""
                head = f"• `{mid}`"
            lines.append(head + (f" · {extra}" if extra else "") + note)
        lines.append("")

    labels = (summary or {}).get("labels_applied", "?")
    marked = (summary or {}).get("marked_read") or (summary or {}).get("newsletters_marked_read") or 0
    fails = len((summary or {}).get("label_failures") or [])
    lines.append(
        f"Labels: {labels} · Unsub queued: {unsub_n} · Marked read: {marked} · Failures: {fails}"
    )

    draft = build_unsub_draft_text(summary)
    if draft:
        lines.append("")
        lines.append(draft.rstrip())

    return "\n".join(lines).rstrip() + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--session-key", required=True)
    ap.add_argument("--apply-orphan", action="store_true", default=True)
    ap.add_argument("--no-apply-orphan", action="store_false", dest="apply_orphan")
    ap.add_argument("--session-file", default="", help="Override session jsonl path (tests)")
    args = ap.parse_args()

    path = Path(args.session_file) if args.session_file else find_session_file(args.session_key)
    if not path or not path.exists():
        print(json.dumps({"ok": False, "error": "session not found", "session_key": args.session_key}))
        return 1

    ev = extract_tool_events(path)
    finalize_ok = ev["finalize_ok"]
    orphan_items = ev["orphan_items"]
    listed_count = ev["listed_count"]
    validation_errors = ev["validation_errors"]
    list_recent_called = ev["list_recent_called"]
    tool_call_leaked = ev["tool_call_leaked"]
    finalize_summary = ev["finalize_summary"]
    finalize_items = enrich_items_with_meta(ev["finalize_items"], ev["listed_meta"])
    orphan_items = enrich_items_with_meta(orphan_items, ev["listed_meta"])

    out = {
        "session_key": args.session_key,
        "session_file": str(path),
        "finalize_ok": finalize_ok,
        "listed_count": listed_count,
        "validation_errors": validation_errors,
        "orphan_items": len(orphan_items or []),
        "list_recent_called": list_recent_called,
        "tool_call_leaked": tool_call_leaked,
    }

    if tool_call_leaked:
        out["ok"] = False
        out["error"] = "tool_call_leaked_not_executed"
        out["retry"] = True
        print(json.dumps(out, indent=2))
        return 1

    if not list_recent_called and not finalize_ok and not orphan_items:
        out["ok"] = False
        out["error"] = "list_recent_not_executed"
        out["retry"] = True
        print(json.dumps(out, indent=2))
        return 1

    if finalize_ok:
        out["ok"] = True
        out["slack_text"] = build_slack_text(
            args.session_key,
            listed_count=listed_count,
            summary=finalize_summary,
            items=finalize_items,
        )
        out["unsub_draft_text"] = build_unsub_draft_text(finalize_summary)
        out["unsub_pending_ids"] = _unsub_pending_ids(finalize_summary)
        print(json.dumps(out))
        return 0

    if orphan_items and args.apply_orphan:
        finalize_triage = load_finalize()
        summary = finalize_triage(orphan_items)
        out["recovered"] = True
        out["summary"] = {
            "ok": summary.get("ok"),
            "total": summary.get("total"),
            "counts": summary.get("counts"),
            "labels_applied": summary.get("labels_applied"),
            "marked_read": summary.get("marked_read") or summary.get("newsletters_marked_read"),
            "unsub_queued_count": summary.get("unsub_queued_count"),
            "pending_open_total": summary.get("pending_open_total"),
            "label_failures": len(summary.get("label_failures") or []),
            "unsub_queued": summary.get("unsub_queued"),
            "unsub_already_queued": summary.get("unsub_already_queued"),
            "unsub_already_done": summary.get("unsub_already_done"),
        }
        out["ok"] = bool(summary.get("ok"))
        if out["ok"]:
            out["slack_text"] = build_slack_text(
                args.session_key,
                listed_count=listed_count or int(summary.get("total") or 0),
                summary=summary,
                items=orphan_items,
            )
            out["unsub_draft_text"] = build_unsub_draft_text(summary)
            out["unsub_pending_ids"] = _unsub_pending_ids(summary)
        print(json.dumps(out, indent=2))
        return 0 if summary.get("ok") else 1

    if list_recent_called and listed_count == 0 and validation_errors == 0:
        out["ok"] = True
        out["note"] = "no eligible mail / nothing to finalize"
        # Empty inbox: still ok, but no Slack post (runner skips blank digests).
        out["slack_text"] = ""
        print(json.dumps(out))
        return 0

    out["ok"] = False
    out["error"] = "finalize did not succeed and no recoverable orphan items"
    out["retry"] = True
    print(json.dumps(out, indent=2))
    return 1


if __name__ == "__main__":
    sys.exit(main())
