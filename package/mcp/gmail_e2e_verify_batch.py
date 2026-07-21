#!/usr/bin/env python3
"""Post-batch verifier/recovery for Gmail triage e2e.

Small models sometimes call OpenClaw meta-tool tool_call with args.items but omit
id="gmail_triage_ops__finalize_triage". That fails validation and the model
may still post a fake Slack digest.

This script:
1) Locates the session for --session-key
2) If finalize succeeded in-session → exit 0
3) If a tool_call has items but missing/wrong id → apply finalize_triage directly
4) Exit 1 if nothing applied and mail was listed
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
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


def load_finalize():
    spec = importlib.util.spec_from_file_location("triage_ops", str(TRIAGE_OPS))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.finalize_triage


def find_session_file(session_key: str) -> Path | None:
    # Prefer sessions.json mapping
    if SESSIONS_INDEX.exists():
        try:
            idx = json.loads(SESSIONS_INDEX.read_text())
        except Exception:
            idx = {}
        # shapes vary: dict of key->meta or list
        candidates = []
        if isinstance(idx, dict):
            for k, v in idx.items():
                blob = json.dumps(v) if not isinstance(v, str) else v
                if session_key in k or session_key in blob:
                    candidates.append((k, v))
            # also values may contain sessionFile
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

    # Fallback: newest jsonl whose content mentions the session key
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
    """Return True/False if finalize ok is clear; None if inconclusive."""
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
    # Do NOT treat bare "labels_applied" as success
    return None


def extract_tool_events(path: Path):
    """Return (finalize_ok, orphan_items, listed_count, validation_errors)."""
    finalize_ok = False
    orphan_items = None
    listed_count = 0
    validation_errors = 0

    for o in iter_messages(path):
        msg = o.get("message") or {}
        role = msg.get("role")
        content = msg.get("content")
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        if not isinstance(content, list):
            continue

        if role == "assistant":
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
                    elif inner and "items" in inner and not tid:
                        orphan_items = inner["items"]
                elif name == FINALIZE_ID:
                    items = args.get("items") if isinstance(args, dict) else None
                    if items:
                        orphan_items = items

        if role == "toolResult":
            texts = []
            for c in content:
                if isinstance(c, dict) and c.get("type") == "text":
                    texts.append(c.get("text") or "")
                elif isinstance(c, str):
                    texts.append(c)
            blob = "\n".join(texts)
            if "Validation failed for tool" in blob:
                validation_errors += 1
            import re

            m = re.search(r"Listed\s+(\d+)\s+email", blob)
            if m:
                listed_count = max(listed_count, int(m.group(1)))
            m2 = re.search(r"eligible_total\s*=\s*(\d+)", blob)
            if m2:
                listed_count = max(listed_count, int(m2.group(1)))

            parsed_ok = _tool_result_ok_true(blob)
            if parsed_ok is True:
                finalize_ok = True
                orphan_items = None

    return finalize_ok, orphan_items, listed_count, validation_errors


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--session-key", required=True)
    ap.add_argument("--apply-orphan", action="store_true", default=True)
    ap.add_argument("--no-apply-orphan", action="store_false", dest="apply_orphan")
    args = ap.parse_args()

    path = find_session_file(args.session_key)
    if not path:
        print(json.dumps({"ok": False, "error": "session not found", "session_key": args.session_key}))
        return 1

    finalize_ok, orphan_items, listed_count, validation_errors = extract_tool_events(path)
    out = {
        "session_key": args.session_key,
        "session_file": str(path),
        "finalize_ok": finalize_ok,
        "listed_count": listed_count,
        "validation_errors": validation_errors,
        "orphan_items": len(orphan_items or []),
    }

    if finalize_ok:
        out["ok"] = True
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
            "label_failures": len(summary.get("label_failures") or []),
        }
        out["ok"] = bool(summary.get("ok"))
        print(json.dumps(out, indent=2))
        return 0 if summary.get("ok") else 1

    # zero hits is fine
    if listed_count == 0 and validation_errors == 0:
        out["ok"] = True
        out["note"] = "no eligible mail / nothing to finalize"
        print(json.dumps(out))
        return 0

    out["ok"] = False
    out["error"] = "finalize did not succeed and no recoverable orphan items"
    print(json.dumps(out, indent=2))
    return 1


if __name__ == "__main__":
    sys.exit(main())
