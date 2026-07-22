#!/usr/bin/env python3
"""gmail_triage_ops MCP — one-shot finalize after LLM categorization.

LLM categorizes; this tool applies GMAIL_LABEL_PREFIX/<CAT> labels, marks
NEWSLETTER/SOCIAL read, and queues NEWSLETTER/SPAM into the unsubscribe
approval pipeline. Stdlib + NDJSON.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import _config as cfg

GMAIL_CREDS = cfg.gmail_creds()
GMAIL_KEYS = cfg.gmail_keys()
UNSUB_MCP = Path(
    os.environ.get("LIST_UNSUB_MCP") or str(cfg.bin_dir() / "list_unsubscribe_mcp.py")
)
if not UNSUB_MCP.exists():
    _sibling = Path(__file__).resolve().parent / "list_unsubscribe_mcp.py"
    if _sibling.exists():
        UNSUB_MCP = _sibling
TRIAGE_LOG = Path(
    os.environ.get("GMAIL_TRIAGE_LOG") or str(cfg.state_dir() / "triage_finalize.jsonl")
)

# Label apply mode: "batch" (default) = messages.batchModify by category;
# "sequential" = per-message modify (regress / debug).
LABEL_MODE = os.environ.get("GMAIL_TRIAGE_LABEL_MODE", "batch").strip().lower()
BATCH_MODIFY_CHUNK = max(1, min(int(os.environ.get("GMAIL_TRIAGE_BATCH_CHUNK", "100")), 1000))
# Max items per finalize call (chunked triage). Soft override: GMAIL_TRIAGE_MAX_ITEMS
MAX_ITEMS = max(1, min(int(os.environ.get("GMAIL_TRIAGE_MAX_ITEMS", "25")), 500))
# After LABEL_PREFIX/NEWSLETTER (and SOCIAL/SPAM) label, remove UNREAD. Soft off:
# GMAIL_MARK_NEWSLETTER_READ=0 / GMAIL_MARK_SOCIAL_READ=0 / GMAIL_MARK_SPAM_READ=0
MARK_NEWSLETTER_READ = os.environ.get("GMAIL_MARK_NEWSLETTER_READ", "1").strip().lower() in {"1", "true", "yes", "on"}
MARK_SOCIAL_READ = os.environ.get("GMAIL_MARK_SOCIAL_READ", "1").strip().lower() in {"1", "true", "yes", "on"}
MARK_SPAM_READ = os.environ.get("GMAIL_MARK_SPAM_READ", "1").strip().lower() in {"1", "true", "yes", "on"}
# Post-unsub recidivists forced to SPAM are marked read by default.
MARK_POST_UNSUB_SPAM_READ = os.environ.get("GMAIL_MARK_POST_UNSUB_SPAM_READ", "1").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
MARK_READ_CATS = set()
if MARK_NEWSLETTER_READ:
    MARK_READ_CATS.add("NEWSLETTER")
if MARK_SOCIAL_READ:
    MARK_READ_CATS.add("SOCIAL")
if MARK_SPAM_READ:
    MARK_READ_CATS.add("SPAM")

TRIAGE_SEEN = Path(
    os.environ.get("GMAIL_TRIAGE_SEEN") or str(cfg.state_dir() / "triage_seen.json")
)

ALLOWED = {"URGENT", "ACTION-REQUIRED", "FYI", "SOCIAL", "NEWSLETTER", "SPAM"}
LABEL_PREFIX = cfg.label_prefix()
OC_LABELS = {c: cfg.label_name(c) for c in ALLOWED}
SERVER_INFO = {"name": "gmail-triage-ops", "version": "0.3.1"}
PROTOCOL_VERSION = "2024-11-05"

TOOLS = [
    {
        "name": "finalize_triage",
        "description": (
            "After you categorize email_query hits, call this ONCE with the full list. "
            f"Applies {LABEL_PREFIX}/<CAT> labels (replacing any other {LABEL_PREFIX}/* "
            "category label on the message), marks NEWSLETTER/SOCIAL/SPAM read, and queues "
            "NEWSLETTER/SPAM into the unsubscribe approval pipeline (propose only — "
            "never executes unsubscribe; SOCIAL is never auto-queued). "
            "Senders successfully unsubscribed earlier are watched: after a grace window, "
            "matching mail is forced to SPAM (mark-read) and not re-queued for unsub. "
            "Do not call per-message label or propose tools during triage."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "description": "Categorized hits from email_query",
                    "items": {
                        "type": "object",
                        "properties": {
                            "message_id": {"type": "string"},
                            "category": {
                                "type": "string",
                                "enum": sorted(ALLOWED),
                            },
                            "from": {"type": "string"},
                            "subject": {"type": "string"},
                        },
                        "required": ["message_id", "category"],
                    },
                },
            },
            "required": ["items"],
        },
    },
]


def log(msg: str) -> None:
    print(f"[gmail_triage_ops] {msg}", file=sys.stderr, flush=True)


def _http(url, data=None, headers=None, method=None, timeout=45):
    if data is not None and not isinstance(data, (bytes, bytearray)):
        data = json.dumps(data).encode()
    req = urllib.request.Request(url, data=data, method=method or ("POST" if data is not None else "GET"))
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        return r.status, raw


def load_creds():
    return json.loads(GMAIL_CREDS.read_text(encoding="utf-8"))


def save_creds(creds):
    cfg.atomic_write_json(GMAIL_CREDS, creds)


def refresh_if_expired(creds):
    exp_ms = creds.get("expiry_date") or 0
    if exp_ms / 1000 > time.time() + 30:
        return creds
    keys = json.loads(GMAIL_KEYS.read_text(encoding="utf-8"))
    inst = keys.get("installed") or keys.get("web") or keys
    body = urllib.parse.urlencode(
        {
            "client_id": inst["client_id"],
            "client_secret": inst["client_secret"],
            "refresh_token": creds["refresh_token"],
            "grant_type": "refresh_token",
        }
    ).encode()
    _, raw = _http(
        "https://oauth2.googleapis.com/token",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    tok = json.loads(raw.decode())
    creds["access_token"] = tok["access_token"]
    creds["expiry_date"] = int(time.time() * 1000) + int(tok.get("expires_in", 3600)) * 1000
    return creds


def gmail_api(method: str, path: str, params=None, payload=None):
    creds = refresh_if_expired(load_creds())
    save_creds(creds)
    url = "https://gmail.googleapis.com/gmail/v1/users/me/" + path
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)
    headers = {"Authorization": "Bearer " + creds["access_token"]}
    if payload is not None:
        headers["Content-Type"] = "application/json"
        status, raw = _http(url, data=payload, headers=headers, method=method)
    else:
        status, raw = _http(url, headers=headers, method=method)
    return status, json.loads(raw.decode()) if raw else {}


def load_unsub_module():
    spec = importlib.util.spec_from_file_location("list_unsubscribe_mcp", UNSUB_MCP)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # Avoid running MCP main loop: module only defines functions when imported
    # if __name__ guard protects main.
    sys.modules["list_unsubscribe_mcp"] = mod
    spec.loader.exec_module(mod)
    return mod


def ensure_oc_labels() -> dict[str, str]:
    """Return map category -> Gmail label id for LABEL_PREFIX/*."""
    _, data = gmail_api("GET", "labels")
    by_name = {l["name"]: l["id"] for l in data.get("labels") or []}
    out = {}
    for cat, name in OC_LABELS.items():
        if name in by_name:
            out[cat] = by_name[name]
            continue
        status, created = gmail_api(
            "POST",
            "labels",
            payload={
                "name": name,
                "labelListVisibility": "labelShow",
                "messageListVisibility": "show",
            },
        )
        if status >= 300 or not created.get("id"):
            raise RuntimeError(f"failed to create label {name}: {created}")
        out[cat] = created["id"]
        by_name[name] = created["id"]
    return out


def sibling_oc_label_ids(label_ids: dict[str, str], keep_cat: str) -> list[str]:
    """Other PREFIX/* category label ids to strip so a message has exactly one category."""
    out = []
    for cat, lid in (label_ids or {}).items():
        if cat == keep_cat or not lid:
            continue
        out.append(lid)
    return out


def apply_label(
    message_id: str,
    label_id: str,
    remove_unread: bool = False,
    remove_label_ids: list[str] | None = None,
) -> dict:
    remove = list(remove_label_ids or [])
    if remove_unread and "UNREAD" not in remove:
        remove.append("UNREAD")
    status, data = gmail_api(
        "POST",
        f"messages/{message_id}/modify",
        payload={"addLabelIds": [label_id], "removeLabelIds": remove},
    )
    if status >= 300:
        return {"ok": False, "message_id": message_id, "error": data, "http_status": status}
    return {
        "ok": True,
        "message_id": message_id,
        "label_id": label_id,
        "mode": "sequential",
        "marked_read": bool(remove_unread),
        "replaced_oc_labels": len([x for x in remove if x != "UNREAD"]),
    }


def apply_labels_batch(items: list, label_ids: dict[str, str]) -> list:
    """Group by (category, mark_read) and call messages.batchModify (chunked).

    Always strips other PREFIX/* category labels so reclassify replaces, not stacks.
    """
    # key: (category, mark_read_bool) -> message_ids
    groups: dict[tuple[str, bool], list[str]] = {}
    for item in items:
        cat = item["category"]
        mark_read = bool(item.get("force_mark_read")) or cat in MARK_READ_CATS
        groups.setdefault((cat, mark_read), []).append(item["message_id"])

    results = []
    for (cat, mark_read), mids in groups.items():
        label_id = label_ids[cat]
        remove = sibling_oc_label_ids(label_ids, cat)
        if mark_read:
            remove = list(remove) + ["UNREAD"]
        seen = set()
        uniq = []
        for mid in mids:
            if mid not in seen:
                seen.add(mid)
                uniq.append(mid)
        for i in range(0, len(uniq), BATCH_MODIFY_CHUNK):
            chunk = uniq[i : i + BATCH_MODIFY_CHUNK]
            status, data = gmail_api(
                "POST",
                "messages/batchModify",
                payload={
                    "ids": chunk,
                    "addLabelIds": [label_id],
                    "removeLabelIds": remove,
                },
            )
            if status >= 300:
                for mid in chunk:
                    results.append(
                        {
                            "ok": False,
                            "message_id": mid,
                            "category": cat,
                            "label_id": label_id,
                            "error": data,
                            "http_status": status,
                            "mode": "batch",
                        }
                    )
            else:
                for mid in chunk:
                    results.append(
                        {
                            "ok": True,
                            "message_id": mid,
                            "category": cat,
                            "label_id": label_id,
                            "mode": "batch",
                            "marked_read": bool(mark_read),
                            "replaced_oc_labels": len([x for x in remove if x != "UNREAD"]),
                        }
                    )
    return results


def apply_post_unsub_overrides(items: list, unsub, *, record_hits: bool = True) -> list:
    """Force SPAM for senders on post-unsub watch past grace. Mutates items in place.

    Returns list of override records for the finalize summary. Same-domain siblings of
    an email-scoped watch count toward domain promotion; a second pass re-matches after
    promotion so later items in the same batch can also override.

    Guards (runtime-proven):
    - Never bump hits / promote for mail still inside grace (exact watch, not past grace).
    - Only NEWSLETTER/SPAM participate in sibling promotion notes.
    - Never override URGENT / ACTION-REQUIRED (high-signal categories).
    - Pass-2 overrides do not double-count hits already recorded via sibling note.
    """
    overrides = []
    if not getattr(unsub, "POST_UNSUB_WATCH_ENABLED", True):
        return overrides
    match_fn = getattr(unsub, "match_post_unsub_watch", None)
    hit_fn = getattr(unsub, "record_post_unsub_hit", None)
    if not callable(match_fn):
        return overrides

    # Marketing-like cats may promote domain scope and be forced to SPAM.
    # High-signal cats are never overridden by post-unsub watch.
    PROMOTE_CATS = {"NEWSLETTER", "SPAM"}
    PROTECTED_CATS = {"URGENT", "ACTION-REQUIRED"}

    def _ensure_from(item: dict) -> str:
        from_hdr = item.get("from") or ""
        if from_hdr or not item.get("message_id"):
            return from_hdr
        try:
            meta = unsub.fetch_headers(item["message_id"])
            from_hdr = (meta.get("from") or "")[:120]
            if from_hdr:
                item["from"] = from_hdr
        except Exception as exc:
            log(f"post-unsub From fetch failed for {item.get('message_id')}: {exc}")
            item["post_unsub_skipped_reason"] = "missing_from"
        return from_hdr

    def _override_one(item: dict, entry: dict, from_hdr: str, *, do_hit: bool) -> dict:
        original = item["category"]
        item["category"] = "SPAM"
        item["post_unsub_override"] = True
        item["post_unsub_watch_key"] = entry.get("key")
        item["original_category"] = original
        if MARK_POST_UNSUB_SPAM_READ:
            item["force_mark_read"] = True
        hit_entry = None
        if do_hit and record_hits and callable(hit_fn):
            try:
                hit_entry = hit_fn(from_hdr, entry)
            except Exception as exc:
                log(f"post-unsub hit record failed: {exc}")
        return {
            "message_id": item["message_id"],
            "from": from_hdr[:120],
            "original_category": original,
            "category": "SPAM",
            "watch_key": (hit_entry or entry).get("key"),
            "hits": (hit_entry or entry).get("hits"),
            "marked_read": bool(item.get("force_mark_read")),
        }

    sibling_noted_ids: set[str] = set()

    # Pass 1: exact email / domain matches past grace
    pending_sibling_note: list[tuple[dict, str]] = []
    for item in items:
        if item.get("post_unsub_override"):
            continue
        if item.get("category") in PROTECTED_CATS:
            continue
        from_hdr = _ensure_from(item)
        if not from_hdr:
            if not item.get("post_unsub_skipped_reason"):
                item["post_unsub_skipped_reason"] = "missing_from"
            continue
        matched, entry = match_fn(from_hdr, require_past_grace=True)
        if matched and entry:
            overrides.append(_override_one(item, entry, from_hdr, do_hit=True))
            continue
        # In-grace exact watch: do not bump hits (confirmation mail).
        watched_ignoring_grace, _ = match_fn(from_hdr, require_past_grace=False)
        if watched_ignoring_grace:
            continue
        # Only marketing-like siblings count toward domain promotion.
        if (
            item.get("category") in PROMOTE_CATS
            and record_hits
            and callable(hit_fn)
        ):
            pending_sibling_note.append((item, from_hdr))

    # Sibling notes may promote email → domain for later / same-batch items
    for item, from_hdr in pending_sibling_note:
        if item.get("post_unsub_override"):
            continue
        try:
            hit_fn(from_hdr)
            mid = item.get("message_id")
            if mid:
                sibling_noted_ids.add(mid)
        except Exception as exc:
            log(f"post-unsub sibling note failed: {exc}")

    # Pass 2: re-match after possible promotion (no double-count for sibling-noted ids)
    for item in items:
        if item.get("post_unsub_override"):
            continue
        if item.get("category") in PROTECTED_CATS:
            continue
        if item.get("post_unsub_skipped_reason") == "missing_from":
            continue
        from_hdr = item.get("from") or _ensure_from(item)
        if not from_hdr:
            continue
        matched, entry = match_fn(from_hdr, require_past_grace=True)
        if matched and entry:
            mid = item.get("message_id") or ""
            overrides.append(
                _override_one(
                    item,
                    entry,
                    from_hdr,
                    do_hit=(mid not in sibling_noted_ids),
                )
            )

    return overrides


def mark_triage_seen(items: list, label_results: list) -> int:
    """Record successfully labeled message_ids so list_recent(skip_seen) ignores them."""
    ok_ids = {r.get("message_id") for r in label_results if r.get("ok")}
    if not ok_ids:
        return 0
    seen = {}
    if TRIAGE_SEEN.exists():
        try:
            seen = json.loads(TRIAGE_SEEN.read_text(encoding="utf-8") or "{}")
            if not isinstance(seen, dict):
                seen = {}
        except Exception:
            seen = {}
    ts = time.strftime("%Y-%m-%dT%H:%M:%S")
    n = 0
    by_id = {i["message_id"]: i for i in items}
    for mid in ok_ids:
        if not mid:
            continue
        prev = seen.get(mid) or {}
        item = by_id.get(mid) or {}
        seen[mid] = {
            "ts": ts,
            "category": item.get("category") or prev.get("category"),
            "from": (item.get("from") or prev.get("from") or "")[:120],
            "subject": (item.get("subject") or prev.get("subject") or "")[:160],
        }
        n += 1
    cfg.atomic_write_json(TRIAGE_SEEN, seen)
    return n

def finalize_triage(items: list) -> dict:
    if not isinstance(items, list) or not items:
        return {"ok": False, "error": "items must be a non-empty array"}

    normalized = []
    errors = []
    for raw in items:
        if not isinstance(raw, dict):
            errors.append({"item": raw, "error": "not an object"})
            continue
        mid = str(raw.get("message_id") or "").strip()
        cat = str(raw.get("category") or "").strip().upper()
        if not mid:
            errors.append({"item": raw, "error": "message_id required"})
            continue
        if cat not in ALLOWED:
            errors.append({"message_id": mid, "error": f"invalid category {cat}"})
            continue
        normalized.append(
            {
                "message_id": mid,
                "category": cat,
                "from": str(raw.get("from") or "")[:120],
                "subject": str(raw.get("subject") or "")[:160],
            }
        )

    if not normalized:
        return {"ok": False, "error": "no valid items", "errors": errors}

    if len(normalized) > MAX_ITEMS:
        return {
            "ok": False,
            "error": (
                f"too many items ({len(normalized)}>{MAX_ITEMS}); "
                "chunk with list_recent limit<=25 and finalize per page"
            ),
            "max_items": MAX_ITEMS,
            "total": len(normalized),
        }

    unsub = load_unsub_module()
    post_unsub_overrides = apply_post_unsub_overrides(normalized, unsub)

    label_ids = ensure_oc_labels()

    label_results = []
    unsub_results = []
    counts = {c: 0 for c in ALLOWED}
    for item in normalized:
        counts[item["category"]] += 1

    mode = LABEL_MODE if LABEL_MODE in {"batch", "sequential"} else "batch"
    if mode == "sequential":
        for item in normalized:
            remove_unread = item["category"] in MARK_READ_CATS or bool(item.get("force_mark_read"))
            lr = apply_label(
                item["message_id"],
                label_ids[item["category"]],
                remove_unread=remove_unread,
                remove_label_ids=sibling_oc_label_ids(label_ids, item["category"]),
            )
            lr["category"] = item["category"]
            label_results.append(lr)
    else:
        label_results = apply_labels_batch(normalized, label_ids)

    for item in normalized:
        cat = item["category"]
        # Post-unsub overrides: label SPAM but do not re-queue unsubscribe.
        if item.get("post_unsub_override"):
            unsub_results.append(
                {
                    "message_id": item["message_id"],
                    "category": cat,
                    "from": item["from"],
                    "subject": item["subject"],
                    "proposed": False,
                    "skipped": True,
                    "reason": "post_unsub_watch",
                    "suppressed_key": item.get("post_unsub_watch_key"),
                    "note": "Post-unsub recidivist — labeled SPAM, unsub not re-queued",
                }
            )
            continue
        if cat in {"NEWSLETTER", "SPAM"}:
            # Hand off to unsubscribe approval pipeline (propose only).
            ur = unsub.propose_unsubscribe(item["message_id"], cat)
            unsub_results.append(
                {
                    "message_id": item["message_id"],
                    "category": cat,
                    "from": item["from"],
                    "subject": item["subject"],
                    "proposed": ur.get("proposed"),
                    "skipped": ur.get("skipped"),
                    "id": ur.get("id"),
                    "status": ur.get("status"),
                    "method": ur.get("method"),
                    "reason": ur.get("reason") or ur.get("note"),
                    "suppressed_key": ur.get("suppressed_key") or ur.get("watch_key"),
                }
            )

    queued = [u for u in unsub_results if u.get("proposed")]
    skipped = [u for u in unsub_results if u.get("skipped")]
    needs_manual = [u for u in unsub_results if u.get("status") == "needs_manual"]
    label_ok = sum(1 for r in label_results if r.get("ok"))
    marked_read = sum(1 for r in label_results if r.get("ok") and r.get("marked_read"))
    seen_marked = mark_triage_seen(normalized, label_results)
    label_fail = [r for r in label_results if not r.get("ok")]

    summary = {
        "ok": not label_fail and not errors,
        "total": len(normalized),
        "counts": counts,
        "label_mode": mode,
        "triage_seen_marked": seen_marked,
        "labels_applied": label_ok,
        "newsletters_marked_read": marked_read,  # NEWSLETTER + SOCIAL + post-unsub SPAM when enabled
        "marked_read": marked_read,
        "label_failures": label_fail,
        "post_unsub_overrides": post_unsub_overrides,
        "post_unsub_override_count": len(post_unsub_overrides),
        "unsub_queued": [
            {"id": u.get("id"), "category": u["category"], "from": u["from"], "subject": u["subject"], "method": u.get("method")}
            for u in queued
        ],
        "unsub_skipped": skipped,
        "unsub_needs_manual": needs_manual,
        "unsub_queued_count": len(queued),
        "unsub_skipped_count": len(skipped),
        "parse_errors": errors,
        "note": (
            "Labels applied (exclusive PREFIX/* — prior category labels removed). "
            "NEWSLETTER/SPAM handed to unsubscribe approval queue. "
            "Post-unsub watch past grace → SPAM (no re-queue). "
            "SOCIAL labeled only (not auto-unsub). Nothing was unsubscribed. "
            "User must approve pending ids."
        ),
    }

    TRIAGE_LOG.parent.mkdir(parents=True, exist_ok=True)
    with TRIAGE_LOG.open("a", encoding="utf-8") as fh:
        fh.write(
            json.dumps(
                {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "summary": summary, "items": normalized},
                sort_keys=True,
            )
            + "\n"
        )
    return summary


def call_tool(name: str, args: dict):
    if name == "finalize_triage":
        return finalize_triage(args.get("items") or [])
    return {"ok": False, "error": f"unknown tool: {name}"}


def handle_message(msg):
    method = msg.get("method")
    req_id = msg.get("id")
    params = msg.get("params") or {}
    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": SERVER_INFO,
            },
        }
    if method == "notifications/initialized":
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}}
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        try:
            result = call_tool(name, args)
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"content": [{"type": "text", "text": json.dumps(result, indent=2)}]},
            }
        except Exception as exc:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32603, "message": f"tool error: {exc}"},
            }
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"method not found: {method}"},
    }


def read_message(stream):
    line = stream.readline()
    if not line:
        return None
    text_line = line.decode("utf-8", errors="replace").rstrip("\r\n")
    if not text_line:
        return read_message(stream)
    if text_line.lower().startswith("content-length:"):
        headers = {"content-length": text_line.split(":", 1)[1].strip()}
        while True:
            hline = stream.readline()
            if not hline:
                return None
            htext = hline.decode("ascii", errors="replace").rstrip("\r\n")
            if not htext:
                break
            if ":" in htext:
                k, v = htext.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        length = int(headers.get("content-length", "0"))
        if length <= 0:
            return None
        return json.loads(stream.read(length).decode("utf-8"))
    return json.loads(text_line)


def write_message(msg):
    body = (json.dumps(msg, separators=(",", ":")) + "\n").encode("utf-8")
    sys.stdout.buffer.write(body)
    sys.stdout.buffer.flush()


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--finalize-json":
        payload = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
        print(json.dumps(finalize_triage(payload.get("items") or payload), indent=2))
        return
    if len(sys.argv) > 1 and sys.argv[1] == "--preview-post-unsub-json":
        # Dry path: apply watch overrides only (no Gmail mutate). For e2e/unit.
        payload = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
        raw_items = payload.get("items") or payload
        normalized = []
        for raw in raw_items:
            if not isinstance(raw, dict):
                continue
            mid = str(raw.get("message_id") or "").strip()
            cat = str(raw.get("category") or "").strip().upper()
            if not mid or cat not in ALLOWED:
                continue
            normalized.append(
                {
                    "message_id": mid,
                    "category": cat,
                    "from": str(raw.get("from") or "")[:120],
                    "subject": str(raw.get("subject") or "")[:160],
                }
            )
        unsub = load_unsub_module()
        overrides = apply_post_unsub_overrides(normalized, unsub, record_hits=False)
        print(
            json.dumps(
                {
                    "ok": True,
                    "items": normalized,
                    "post_unsub_overrides": overrides,
                    "post_unsub_override_count": len(overrides),
                },
                indent=2,
            )
        )
        return
    log(f"gmail_triage_ops MCP starting (protocol {PROTOCOL_VERSION})")
    stdin = sys.stdin.buffer
    while True:
        try:
            msg = read_message(stdin)
        except (json.JSONDecodeError, ValueError) as exc:
            log(f"parse error: {exc}")
            continue
        if msg is None:
            log("stdin closed, exiting")
            break
        resp = handle_message(msg)
        if resp is not None:
            write_message(resp)


if __name__ == "__main__":
    main()
