#!/usr/bin/env python3
"""list_unsubscribe MCP — propose → you approve → execute. No browser. Stdlib + NDJSON."""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from email.mime.text import MIMEText
from pathlib import Path

import _config as cfg

GMAIL_CREDS = cfg.gmail_creds()
GMAIL_KEYS = cfg.gmail_keys()
STATE_DIR = cfg.state_dir()
LOG_FILE = STATE_DIR / "unsubscribe_log.jsonl"
PENDING_FILE = STATE_DIR / "unsubscribe_pending.json"
SEEN_FILE = STATE_DIR / "unsubscribe_seen.json"
SUPPRESSED_FILE = STATE_DIR / "unsubscribe_suppressed_senders.json"
WATCH_FILE = STATE_DIR / "unsubscribe_watch.json"
DRAFT_BATCHES_DIR = STATE_DIR / "unsub_draft_batches"
# Slack button value is a batch_id; batches expire so stale buttons cannot approve forever.
DRAFT_BATCH_TTL_SECONDS = int(os.environ.get("GMAIL_UNSUB_BATCH_TTL_DAYS", "7") or "7") * 86400
_BATCH_ID_RE = re.compile(r"^[a-f0-9]{16,64}$")

AUTO_CATEGORIES = {"NEWSLETTER", "SPAM"}
ALLOWED_CATEGORIES = {"NEWSLETTER", "SPAM", "FYI", "SOCIAL", "URGENT", "ACTION-REQUIRED", "USER"}
OPEN_STATUSES = frozenset({"pending", "needs_manual", "blocked"})
SERVER_INFO = {"name": "list-unsubscribe", "version": "0.5.1"}
PROTOCOL_VERSION = "2024-11-05"

# Post-unsub recidivism: after successful approve, watch sender; later mail → SPAM.
POST_UNSUB_WATCH_ENABLED = os.environ.get("GMAIL_POST_UNSUB_WATCH", "1").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
POST_UNSUB_GRACE_DAYS = max(0, int(os.environ.get("GMAIL_POST_UNSUB_GRACE_DAYS", "3") or "3"))
POST_UNSUB_SCOPE = (os.environ.get("GMAIL_POST_UNSUB_SCOPE", "email") or "email").strip().lower()
if POST_UNSUB_SCOPE not in {"email", "domain"}:
    POST_UNSUB_SCOPE = "email"
# Promote email-scoped watches to domain after this many distinct From addresses (0=off).
POST_UNSUB_DOMAIN_AFTER_HITS = max(
    0, int(os.environ.get("GMAIL_POST_UNSUB_DOMAIN_AFTER_HITS", "2") or "2")
)

TOOLS = [
    {
        "name": "propose_unsubscribe",
        "description": (
            "Inspect List-Unsubscribe headers and QUEUE a candidate for human approval. "
            "Does NOT unsubscribe. "
            "Auto-triage uses NEWSLETTER/SPAM; when the user explicitly asks to unsub a "
            "misclassified message, use category=USER (or the shown category) with that message_id. "
            "Skips senders/domains previously suppressed via reject unless force=true. "
            "User must later approve via approve_unsubscribe."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "message_id": {"type": "string"},
                "category": {
                    "type": "string",
                    "enum": ["NEWSLETTER", "SPAM", "FYI", "SOCIAL", "URGENT", "ACTION-REQUIRED", "USER"],
                    "description": "Use USER when the operator explicitly requests unsub for a non-newsletter hit",
                },
                "force": {
                    "type": "boolean",
                    "default": False,
                    "description": "If true, propose even if sender is suppressed (user override)",
                },
            },
            "required": ["message_id", "category"],
        },
    },
    {
        "name": "list_pending_unsubscribes",
        "description": "List unsubscribe candidates waiting for human approval.",
        "inputSchema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "default": 50}},
        },
    },
    {
        "name": "approve_unsubscribe",
        "description": (
            "Execute unsubscribe(s) after the human explicitly asks to approve. "
            "Accepts pending proposal ids only (from list_pending_unsubscribes / digest). "
            "Does not auto-propose. Never invent ids. "
            "Call only when the operator says `approve <pending_id>` in Slack (or CLI --approve). "
            "Never call during automated triage batches."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Pending proposal ids only",
                },
                "id": {"type": "string", "description": "Single pending proposal id"},
            },
        },
    },
    {
        "name": "reject_unsubscribe",
        "description": (
            "Dismiss pending unsubscribe proposal(s) without executing. "
            "By default also suppresses that sender so future triage will not re-propose them. "
            "suppress_scope=domain (default) blocks the From domain (e.g. email.apple.com); "
            "email blocks only that exact address; set suppress_sender=false to dismiss once only."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "ids": {"type": "array", "items": {"type": "string"}},
                "id": {"type": "string"},
                "suppress_sender": {
                    "type": "boolean",
                    "default": True,
                    "description": "If true (default), do not propose this sender again",
                },
                "suppress_scope": {
                    "type": "string",
                    "enum": ["domain", "email"],
                    "default": "domain",
                    "description": "domain=whole From domain; email=exact address only",
                },
            },
        },
    },
    {
        "name": "list_suppressed_senders",
        "description": "List senders/domains that will not be proposed for unsubscribe.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "unsuppress_sender",
        "description": "Remove a sender email or domain from the suppression list so it can be proposed again.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "key": {
                    "type": "string",
                    "description": "Exact suppressed key (email or domain) from list_suppressed_senders",
                },
            },
            "required": ["key"],
        },
    },
    {
        "name": "suppress_sender",
        "description": (
            "Exclude a sender email or domain from future unsubscribe proposes, and dismiss "
            "matching open pending proposals. scope=domain (default) for a bare domain or "
            "email's domain; scope=email for one address only. Use when the operator says "
            "suppress/exclude in Slack."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "key": {
                    "type": "string",
                    "description": "Email, From header, or domain (e.g. linkedin.com)",
                },
                "scope": {
                    "type": "string",
                    "enum": ["domain", "email"],
                    "default": "domain",
                    "description": "domain=whole domain; email=exact address (requires an email key)",
                },
            },
            "required": ["key"],
        },
    },
    {
        "name": "list_post_unsub_watch",
        "description": (
            "List senders watched after a successful unsubscribe. After grace_days, "
            "finalize_triage reclassifies matching mail as SPAM and skips re-propose."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "clear_post_unsub_watch",
        "description": "Remove a sender email or domain from the post-unsub watch list.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "key": {
                    "type": "string",
                    "description": "Exact watch key (email or domain) from list_post_unsub_watch",
                },
            },
            "required": ["key"],
        },
    },
]


def log(msg: str) -> None:
    print(f"[list_unsubscribe] {msg}", file=sys.stderr, flush=True)


def _http(url, data=None, headers=None, method=None, timeout=30):
    if data is not None and not isinstance(data, (bytes, bytearray)):
        data = json.dumps(data).encode()
    req = urllib.request.Request(url, data=data, method=method or ("POST" if data is not None else "GET"))
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        return r.status, raw, dict(r.headers)


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
    _, raw, _ = _http(
        "https://oauth2.googleapis.com/token",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    tok = json.loads(raw.decode())
    creds["access_token"] = tok["access_token"]
    creds["expiry_date"] = int(time.time() * 1000) + int(tok.get("expires_in", 3600)) * 1000
    return creds


def gmail_get(path: str, params=None):
    creds = refresh_if_expired(load_creds())
    save_creds(creds)
    url = "https://gmail.googleapis.com/gmail/v1/users/me/" + path
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)
    _, raw, _ = _http(url, headers={"Authorization": "Bearer " + creds["access_token"]})
    return json.loads(raw.decode()) if raw else {}


def gmail_post(path: str, payload: dict):
    creds = refresh_if_expired(load_creds())
    save_creds(creds)
    url = "https://gmail.googleapis.com/gmail/v1/users/me/" + path
    status, raw, _ = _http(
        url,
        data=payload,
        headers={
            "Authorization": "Bearer " + creds["access_token"],
            "Content-Type": "application/json",
        },
        method="POST",
    )
    return status, json.loads(raw.decode()) if raw else {}


def append_jsonl(path: Path, row: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        STATE_DIR.chmod(0o700)
    except OSError:
        pass
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, sort_keys=True) + "\n")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path: Path, data) -> None:
    cfg.atomic_write_json(path, data)


def load_pending() -> dict:
    data = load_json(PENDING_FILE, {"items": {}})
    if "items" not in data:
        data = {"items": {}}
    return data


def save_pending(data: dict) -> None:
    save_json(PENDING_FILE, data)


def draft_batches_dir() -> Path:
    """Directory for Slack Approve button → pending-id batches."""
    d = DRAFT_BATCHES_DIR
    d.mkdir(parents=True, exist_ok=True)
    try:
        d.chmod(0o700)
    except OSError:
        pass
    return d


def save_unsub_draft_batch(ids: list[str], meta: dict | None = None) -> str:
    """Persist pending ids for a Slack Approve button. Returns batch_id."""
    clean: list[str] = []
    for raw in ids or []:
        pid = str(raw or "").strip()
        if pid and pid not in clean:
            clean.append(pid)
    if not clean:
        raise ValueError("ids required to save unsub draft batch")
    meta = meta or {}
    batch_id = secrets.token_hex(16)
    payload = {
        "ids": clean,
        "session_key": str(meta.get("session_key") or ""),
        "created_ts": float(time.time()),
        "channel": str(meta.get("channel") or ""),
    }
    path = draft_batches_dir() / f"{batch_id}.json"
    save_json(path, payload)
    return batch_id


def load_unsub_draft_batch(batch_id: str) -> dict | None:
    """Load a draft batch; returns None if missing/invalid/expired (TTL cleanup)."""
    bid = (batch_id or "").strip().lower()
    if not _BATCH_ID_RE.match(bid):
        return None
    path = draft_batches_dir() / f"{bid}.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    try:
        created = float(data.get("created_ts") or 0)
    except (TypeError, ValueError):
        created = 0.0
    if created <= 0 or (time.time() - created) > DRAFT_BATCH_TTL_SECONDS:
        try:
            path.unlink()
        except OSError:
            pass
        return None
    ids = [str(x).strip() for x in (data.get("ids") or []) if str(x).strip()]
    if not ids:
        return None
    data["ids"] = ids
    return data


def load_seen() -> dict:
    return load_json(SEEN_FILE, {})


def save_seen(seen: dict) -> None:
    save_json(SEEN_FILE, seen)


def load_suppressed() -> dict:
    data = load_json(SUPPRESSED_FILE, {"entries": {}})
    if "entries" not in data:
        data = {"entries": {}}
    return data


def save_suppressed(data: dict) -> None:
    save_json(SUPPRESSED_FILE, data)


def extract_sender_email(from_header: str) -> str | None:
    if not from_header:
        return None
    m = re.search(r"<([^>]+@[^>]+)>", from_header)
    if m:
        return m.group(1).strip().lower()
    m = re.search(r"([A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,})", from_header, re.I)
    if m:
        return m.group(1).strip().lower()
    return None


def sender_domain(email: str | None) -> str | None:
    if not email or "@" not in email:
        return None
    return email.rsplit("@", 1)[-1].lower()


def is_sender_suppressed(from_header: str, suppressed: dict | None = None) -> tuple[bool, str | None]:
    """Return (suppressed?, matched_key)."""
    suppressed = suppressed if suppressed is not None else load_suppressed()
    entries = suppressed.get("entries") or {}
    email = extract_sender_email(from_header)
    if not email:
        return False, None
    if email in entries:
        return True, email
    domain = sender_domain(email)
    if domain and domain in entries:
        return True, domain
    return False, None


def add_suppressed_sender(
    from_header: str,
    scope: str = "domain",
    reason: str = "rejected",
    pending_id: str | None = None,
) -> dict | None:
    email = extract_sender_email(from_header)
    if not email:
        return None
    scope = (scope or "domain").lower()
    if scope not in {"domain", "email"}:
        scope = "domain"
    key = sender_domain(email) if scope == "domain" else email
    if not key:
        key = email
    data = load_suppressed()
    data["entries"][key] = {
        "key": key,
        "scope": "domain" if key == sender_domain(email) else "email",
        "example_email": email,
        "example_from": (from_header or "")[:120],
        "reason": reason,
        "pending_id": pending_id,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    save_suppressed(data)
    return data["entries"][key]


def dismiss_pending_for_suppressed(pending: dict, key: str, scope: str) -> list[str]:
    """Mark other pending items matching suppressed email/domain as rejected."""
    dismissed = []
    for pid, item in list((pending.get("items") or {}).items()):
        if item.get("status") not in {"pending", "needs_manual", "blocked"}:
            continue
        email = extract_sender_email(item.get("from") or "")
        if not email:
            continue
        match = email == key if scope == "email" else sender_domain(email) == key
        if match:
            item["status"] = "rejected"
            item["rejected_ts"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            item["reject_reason"] = f"auto-dismissed; sender suppressed ({key})"
            pending["items"][pid] = item
            dismissed.append(pid)
    return dismissed


def load_watch() -> dict:
    data = load_json(WATCH_FILE, {"entries": {}})
    if "entries" not in data or not isinstance(data.get("entries"), dict):
        data = {"entries": {}}
    return data


def save_watch(data: dict) -> None:
    save_json(WATCH_FILE, data)


def _parse_ts(ts: str | None) -> float | None:
    if not ts:
        return None
    text = str(ts).strip()
    for fmt, n in (("%Y-%m-%dT%H:%M:%S", 19), ("%Y-%m-%d %H:%M:%S", 19), ("%Y-%m-%d", 10)):
        try:
            return time.mktime(time.strptime(text[:n], fmt))
        except Exception:
            continue
    return None


def watch_grace_elapsed(entry: dict, now: float | None = None) -> bool:
    """True when approved_at + grace_days is in the past (or grace is 0)."""
    now = time.time() if now is None else now
    grace = entry.get("grace_days")
    if grace is None:
        grace = POST_UNSUB_GRACE_DAYS
    try:
        grace_days = max(0, int(grace))
    except (TypeError, ValueError):
        grace_days = POST_UNSUB_GRACE_DAYS
    if grace_days <= 0:
        return True
    approved = _parse_ts(entry.get("approved_at") or entry.get("ts"))
    if approved is None:
        return False
    return now >= approved + grace_days * 86400


def add_post_unsub_watch(
    from_header: str,
    *,
    pending_id: str | None = None,
    message_id: str | None = None,
    method: str | None = None,
    approved_at: str | None = None,
    grace_days: int | None = None,
    scope: str | None = None,
    approved_at_epoch: float | None = None,
) -> dict | None:
    """Record sender after successful unsubscribe. Idempotent per key."""
    if not POST_UNSUB_WATCH_ENABLED:
        return None
    email = extract_sender_email(from_header)
    if not email:
        return None
    scope = (scope or POST_UNSUB_SCOPE or "email").lower()
    if scope not in {"email", "domain"}:
        scope = "email"
    domain = sender_domain(email)
    key = domain if scope == "domain" and domain else email
    if not key:
        return None
    if grace_days is None:
        grace_days = POST_UNSUB_GRACE_DAYS
    if approved_at_epoch is not None:
        approved_at = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(approved_at_epoch))
    elif not approved_at:
        approved_at = time.strftime("%Y-%m-%dT%H:%M:%S")

    data = load_watch()
    entries = data.setdefault("entries", {})
    prev = entries.get(key) or {}
    seen_emails = list(prev.get("seen_emails") or [])
    if email not in seen_emails:
        seen_emails.append(email)
    entry = {
        "key": key,
        "scope": scope if key == (domain if scope == "domain" else email) else ("domain" if key == domain else "email"),
        "example_email": email,
        "example_from": (from_header or "")[:120],
        "approved_at": approved_at,
        "grace_days": int(grace_days),
        "hits": int(prev.get("hits") or 0),
        "seen_emails": seen_emails[:50],
        "pending_id": pending_id or prev.get("pending_id"),
        "message_id": message_id or prev.get("message_id"),
        "method": method or prev.get("method"),
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    # Prefer earliest approved_at so grace does not reset on re-approve.
    prev_approved = _parse_ts(prev.get("approved_at"))
    new_approved = _parse_ts(approved_at)
    if prev_approved is not None and (new_approved is None or prev_approved < new_approved):
        entry["approved_at"] = prev["approved_at"]
        if prev.get("grace_days") is not None:
            entry["grace_days"] = prev["grace_days"]
    entries[key] = entry
    save_watch(data)
    return entry


def match_post_unsub_watch(
    from_header: str,
    *,
    require_past_grace: bool = True,
    now: float | None = None,
    watch: dict | None = None,
) -> tuple[bool, dict | None]:
    """Return (matched?, entry) for exact email- or domain-scoped watch keys.

    Same-domain siblings of an email-scoped watch do not match here (they only
    count toward promotion via record_post_unsub_hit).
    """
    if not POST_UNSUB_WATCH_ENABLED:
        return False, None
    email = extract_sender_email(from_header)
    if not email:
        return False, None
    domain = sender_domain(email)
    watch = watch if watch is not None else load_watch()
    entries = watch.get("entries") or {}
    candidates: list[dict] = []
    email_entry = entries.get(email)
    if email_entry and (email_entry.get("scope") or "email") == "email":
        candidates.append(email_entry)
    if domain:
        domain_entry = entries.get(domain)
        if domain_entry and (domain_entry.get("scope") or "domain") in {"domain", None}:
            candidates.append(domain_entry)
    for entry in candidates:
        if require_past_grace and not watch_grace_elapsed(entry, now=now):
            continue
        return True, entry
    return False, None


def _email_watches_for_domain(domain: str, entries: dict) -> list[dict]:
    if not domain:
        return []
    out = []
    for key, entry in entries.items():
        if (entry.get("scope") or "email") != "email":
            continue
        edom = sender_domain(entry.get("key") or entry.get("example_email") or key)
        if edom == domain:
            out.append(entry)
    return out


def record_post_unsub_hit(from_header: str, entry: dict | None = None) -> dict | None:
    """Bump recidivist hit count; may promote email-scope → domain after N distinct emails.

    Same-domain siblings (e.g. promo@brand when news@brand is watched) count toward
    promotion but are not SPAM-overridden until the watch is domain-scoped.
    """
    if not POST_UNSUB_WATCH_ENABLED:
        return None
    email = extract_sender_email(from_header)
    if not email:
        return None
    domain = sender_domain(email)
    data = load_watch()
    entries = data.setdefault("entries", {})

    if entry is None:
        _matched, found = match_post_unsub_watch(from_header, require_past_grace=False, watch=data)
        if found:
            entry = found
        elif domain:
            siblings = _email_watches_for_domain(domain, entries)
            past = [s for s in siblings if watch_grace_elapsed(s)]
            entry = (past or siblings or [None])[0]
    if not entry:
        return None

    key = entry.get("key")
    if not key or key not in entries:
        _matched, found = match_post_unsub_watch(from_header, require_past_grace=False, watch=data)
        if not found and domain:
            siblings = _email_watches_for_domain(domain, entries)
            found = (siblings or [None])[0]
        if not found:
            return None
        entry = found
        key = entry.get("key")
        if not key or key not in entries:
            return None

    cur = dict(entries.get(key) or entry)
    seen_emails = list(cur.get("seen_emails") or [])
    if email not in seen_emails:
        seen_emails.append(email)
    cur["seen_emails"] = seen_emails[:50]
    cur["hits"] = int(cur.get("hits") or 0) + 1
    cur["last_hit_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    cur["last_hit_from"] = (from_header or "")[:120]

    promote = (
        POST_UNSUB_DOMAIN_AFTER_HITS > 0
        and (cur.get("scope") or "email") == "email"
        and domain
        and len(seen_emails) >= POST_UNSUB_DOMAIN_AFTER_HITS
    )
    if promote:
        old_key = key
        cur["key"] = domain
        cur["scope"] = "domain"
        cur["promoted_at"] = cur["last_hit_at"]
        entries.pop(old_key, None)
        existing_dom = entries.get(domain) or {}
        if existing_dom:
            cur["hits"] = int(existing_dom.get("hits") or 0) + int(cur.get("hits") or 0)
            merged = list(existing_dom.get("seen_emails") or [])
            for e in seen_emails:
                if e not in merged:
                    merged.append(e)
            cur["seen_emails"] = merged[:50]
            prev_a = _parse_ts(existing_dom.get("approved_at"))
            cur_a = _parse_ts(cur.get("approved_at"))
            if prev_a is not None and (cur_a is None or prev_a < cur_a):
                cur["approved_at"] = existing_dom["approved_at"]
        entries[domain] = cur
    else:
        entries[key] = cur
    save_watch(data)
    return cur


def list_post_unsub_watch() -> dict:
    data = load_watch()
    entries = list((data.get("entries") or {}).values())
    now = time.time()
    enriched = []
    for e in entries:
        row = dict(e)
        row["past_grace"] = watch_grace_elapsed(row, now=now)
        enriched.append(row)
    enriched.sort(key=lambda x: x.get("approved_at") or x.get("ts") or "", reverse=True)
    return {
        "count": len(enriched),
        "enabled": POST_UNSUB_WATCH_ENABLED,
        "grace_days_default": POST_UNSUB_GRACE_DAYS,
        "entries": enriched,
    }


def clear_post_unsub_watch(key: str) -> dict:
    key = (key or "").strip().lower()
    if not key:
        return {"ok": False, "error": "key required"}
    data = load_watch()
    entries = data.get("entries") or {}
    if key not in entries:
        return {"ok": False, "error": f"not watched: {key}", "keys": list(entries.keys())}
    removed = entries.pop(key)
    data["entries"] = entries
    save_watch(data)
    append_jsonl(
        LOG_FILE,
        {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": "clear_watch", "key": key},
    )
    return {"ok": True, "removed": removed}


def parse_list_unsubscribe(value: str) -> list[str]:
    if not value:
        return []
    targets = re.findall(r"<([^>]+)>", value)
    normalized = []
    for t in targets:
        t = t.strip()
        if not t:
            continue
        if "@" in t and "://" not in t and not t.lower().startswith("mailto:"):
            t = "mailto:" + t
        normalized.append(t)
    return normalized


def is_one_click(post_header: str) -> bool:
    if not post_header:
        return False
    return "list-unsubscribe=one-click" in post_header.lower().replace(" ", "")


def target_key(target: str) -> str:
    return hashlib.sha256(target.encode("utf-8")).hexdigest()[:16]


def fetch_headers(message_id: str) -> dict:
    data = gmail_get(f"messages/{message_id}", {"format": "full"})
    headers = {
        h["name"].lower(): h["value"]
        for h in (data.get("payload") or {}).get("headers") or []
    }
    targets = parse_list_unsubscribe(headers.get("list-unsubscribe", ""))
    one_click = is_one_click(headers.get("list-unsubscribe-post", ""))
    https_targets = [t for t in targets if t.lower().startswith("https://")]
    mailto_targets = [t for t in targets if t.lower().startswith("mailto:")]
    http_targets = [t for t in targets if t.lower().startswith("http://") and not t.lower().startswith("https://")]

    if one_click and https_targets:
        method = "one_click"
        target = https_targets[0]
        executable = True
    elif mailto_targets:
        method = "mailto"
        target = mailto_targets[0]
        executable = True
    elif https_targets:
        method = "page_based"
        target = https_targets[0]
        executable = False  # needs human/browser — we never auto
    elif http_targets:
        method = "insecure_http"
        target = http_targets[0]
        executable = False
    else:
        method = "none"
        target = None
        executable = False

    return {
        "message_id": message_id,
        "from": headers.get("from", ""),
        "subject": headers.get("subject", ""),
        "list_unsubscribe": headers.get("list-unsubscribe", ""),
        "list_unsubscribe_post": headers.get("list-unsubscribe-post", ""),
        "targets": targets,
        "one_click_header": one_click,
        "method": method,
        "target": target,
        "executable": executable,
    }


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _ssrf_block_reason(url: str) -> str | None:
    """Refuse non-https and private/link-local/metadata targets."""
    import ipaddress

    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        return "invalid_url"
    if parsed.scheme.lower() != "https":
        return "https_required"
    host = (parsed.hostname or "").strip().lower()
    if not host:
        return "missing_host"
    if host in {"localhost", "metadata.google.internal"} or host.endswith(".localhost"):
        return "private_host_blocked"
    try:
        ip = ipaddress.ip_address(host)
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            return "private_ip_blocked"
    except ValueError:
        if host.endswith(".local") or host.endswith(".internal") or host.endswith(".lan"):
            return "private_host_blocked"
    return None


def do_one_click(url: str) -> dict:
    blocked = _ssrf_block_reason(url)
    if blocked:
        return {"action": "one_click", "url": url, "status": "blocked", "detail": blocked}
    body = b"List-Unsubscribe=One-Click"
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "OpenClaw-ListUnsubscribe/0.2 (RFC8058)",
        },
    )
    opener = urllib.request.build_opener(NoRedirectHandler)
    try:
        with opener.open(req, timeout=30) as resp:
            return {"action": "one_click", "url": url, "status": "ok", "http_status": resp.status}
    except urllib.error.HTTPError as exc:
        if 300 <= exc.code < 400:
            return {
                "action": "one_click",
                "url": url,
                "status": "blocked",
                "http_status": exc.code,
                "detail": "redirect_refused",
            }
        return {
            "action": "one_click",
            "url": url,
            "status": "http_error",
            "http_status": exc.code,
            "detail": exc.read()[:200].decode(errors="replace"),
        }
    except Exception as exc:
        return {"action": "one_click", "url": url, "status": "error", "detail": str(exc)[:200]}


def do_mailto(target: str) -> dict:
    import base64
    import re

    parsed = urllib.parse.urlparse(target)
    if parsed.scheme.lower() != "mailto" or not parsed.path:
        return {"action": "mailto", "target": target, "status": "invalid"}
    to_addr = urllib.parse.unquote(parsed.path)
    qs = urllib.parse.parse_qs(parsed.query)
    subject = (qs.get("subject") or ["Unsubscribe"])[0]
    body_text = (qs.get("body") or [""])[0]
    for label, val in (("to", to_addr), ("subject", subject), ("body", body_text)):
        if "\r" in val or "\n" in val:
            return {"action": "mailto", "target": target, "status": "invalid", "detail": f"{label}_has_newline"}
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", to_addr):
        return {"action": "mailto", "target": target, "status": "invalid", "detail": "bad_to_addr"}
    msg = MIMEText(body_text or "")
    msg["To"] = to_addr
    msg["Subject"] = subject
    raw_b64 = base64.urlsafe_b64encode(msg.as_bytes()).decode().rstrip("=")
    try:
        status, data = gmail_post("messages/send", {"raw": raw_b64})
        return {
            "action": "mailto",
            "to": to_addr,
            "subject": subject,
            "status": "ok" if status < 300 else "error",
            "http_status": status,
            "gmail_id": data.get("id"),
        }
    except Exception as exc:
        return {
            "action": "mailto",
            "to": to_addr,
            "subject": subject,
            "status": "error",
            "detail": str(exc)[:200],
        }


def propose_unsubscribe(message_id: str, category: str, force: bool = False) -> dict:
    """Propose unsub for approval.

    Skip (no new queue entry) when:
      - sender suppressed (unless force)
      - sender on post-unsub watch past grace (unless force)
      - already unsubscribed (target in seen)
      - already in queue for this message_id (pending/needs_manual/blocked)
      - same proposal id already done/rejected
    First-time messages create a new pending item.

    If ``message_id`` is actually a pending proposal id (digest *Pending unsubscribe*),
    return already-in-queue guidance — do not call Gmail (avoids HTTP 404).
    """
    category = (category or "").strip().upper()
    if category not in ALLOWED_CATEGORIES:
        return {"ok": False, "error": f"category {category!r} not allowed; use NEWSLETTER|SPAM|FYI|SOCIAL|URGENT|ACTION-REQUIRED|USER"}

    token = (message_id or "").strip()
    if not token:
        return {"ok": False, "error": "message_id required"}

    # Digests list pending ids; operators often paste those into `unsub <id>`.
    pending_items = (load_pending().get("items") or {})
    if token in pending_items:
        item = pending_items[token] or {}
        status = item.get("status") or "pending"
        frm = (item.get("from") or "")[:120]
        return {
            "ok": True,
            "proposed": False,
            "skipped": True,
            "reason": "already_in_queue",
            "id": token,
            "status": status,
            "from": frm,
            "subject": (item.get("subject") or "")[:160],
            "message_id": item.get("message_id"),
            "executed": False,
            "first_seen": False,
            "note": (
                f"Already in queue (`{status}`) — pending id `{token}`"
                + (f" · {frm}" if frm else "")
                + f". To unsubscribe now in Slack: `approve {token}`"
                + f" (CLI: `python3 $OPENCLAW_HOME/bin/list_unsubscribe_mcp.py --approve {token}`)"
            ),
        }

    meta = fetch_headers(token)
    message_id = token
    from_header = meta.get("from") or ""
    subject = (meta.get("subject") or "")[:160]
    suppressed, matched = is_sender_suppressed(from_header)
    if suppressed and not force:
        return {
            "ok": True,
            "proposed": False,
            "skipped": True,
            "reason": "sender_suppressed",
            "suppressed_key": matched,
            "from": from_header[:120],
            "subject": subject,
            "message_id": message_id,
            "executed": False,
            "first_seen": False,
            "note": f"Skipped — sender suppressed ({matched}). Use force=true or unsuppress_sender to allow again.",
        }

    # Already unsubscribed this List-Unsubscribe target (approve recorded it in seen).
    # Also skip re-propose when sender is on post-unsub watch past grace (recidivist → SPAM path).
    watched, watch_entry = match_post_unsub_watch(from_header, require_past_grace=True)
    if watched and not force:
        return {
            "ok": True,
            "proposed": False,
            "skipped": True,
            "reason": "post_unsub_watch",
            "watch_key": (watch_entry or {}).get("key"),
            "from": from_header[:120],
            "subject": subject,
            "message_id": message_id,
            "executed": False,
            "first_seen": False,
            "note": (
                "Skipped — sender still mailing after successful unsubscribe "
                f"({(watch_entry or {}).get('key')}); triage should label SPAM."
            ),
        }

    # Already unsubscribed this List-Unsubscribe target (approve recorded it in seen).
    target = meta.get("target")
    if target and not force:
        seen = load_seen()
        skey = target_key(target)
        if seen.get(skey):
            return {
                "ok": True,
                "proposed": False,
                "skipped": True,
                "reason": "already_unsubscribed",
                "seen_key": skey,
                "seen": seen.get(skey),
                "from": from_header[:120],
                "subject": subject,
                "message_id": message_id,
                "executed": False,
                "first_seen": False,
                "note": "Skipped — this unsubscribe target was already executed/approved.",
            }

    pending = load_pending()
    items = pending.get("items") or {}

    # Already in queue for this Gmail message (any open status).
    for existing in items.values():
        if existing.get("message_id") != message_id:
            continue
        st = existing.get("status")
        if st in OPEN_STATUSES and not force:
            return {
                "ok": True,
                "proposed": False,
                "skipped": True,
                "reason": "already_in_queue",
                "id": existing.get("id"),
                "status": st,
                "from": existing.get("from") or from_header[:120],
                "subject": existing.get("subject") or subject,
                "message_id": message_id,
                "executed": False,
                "first_seen": False,
                "note": (
                    f"Skipped — already in unsubscribe queue ({st}) — pending id `{existing.get('id')}`."
                    f" To unsubscribe now in Slack: `approve {existing.get('id')}`"
                    f" (CLI: `python3 $OPENCLAW_HOME/bin/list_unsubscribe_mcp.py --approve {existing.get('id')}`)"
                ),
            }
        if st in {"done", "rejected"} and not force:
            return {
                "ok": True,
                "proposed": False,
                "skipped": True,
                "reason": f"already_{st}",
                "id": existing.get("id"),
                "status": st,
                "from": existing.get("from") or from_header[:120],
                "subject": existing.get("subject") or subject,
                "message_id": message_id,
                "executed": False,
                "first_seen": False,
                "note": f"Skipped — this message was already {st}.",
            }

    # Stable id from message + target
    raw_key = f"{message_id}|{target or 'none'}"
    pid = hashlib.sha256(raw_key.encode()).hexdigest()[:12]

    existing = items.get(pid)
    if existing and existing.get("status") in {"rejected", "done"} and not force:
        return {
            "ok": True,
            "proposed": False,
            "skipped": True,
            "reason": f"already_{existing.get('status')}",
            "id": pid,
            "from": existing.get("from"),
            "subject": existing.get("subject"),
            "message_id": message_id,
            "executed": False,
            "first_seen": False,
            "note": "Skipped — this proposal was already decided.",
        }

    item = {
        "id": pid,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "message_id": message_id,
        "category": category,
        "from": from_header[:120],
        "sender_email": extract_sender_email(from_header),
        "sender_domain": sender_domain(extract_sender_email(from_header)),
        "subject": subject,
        "method": meta["method"],
        "target": target,
        "executable": meta["executable"],
        "one_click_header": meta["one_click_header"],
        "targets": meta["targets"],
        "status": "pending",
        "first_seen": True,
    }

    if not meta["targets"] or meta["method"] == "none":
        item["status"] = "blocked"
        item["reason"] = "no List-Unsubscribe header"
    elif not meta["executable"]:
        item["status"] = "needs_manual"
        item["reason"] = (
            "page-based or insecure http — will not auto-execute; approve only marks dismissed or note"
        )

    pending.setdefault("items", {})[pid] = item
    save_pending(pending)
    append_jsonl(
        LOG_FILE,
        {
            "ts": item["ts"],
            "event": "propose",
            "id": pid,
            "message_id": message_id,
            "method": item["method"],
            "first_seen": True,
        },
    )
    return {
        "ok": True,
        "proposed": True,
        "skipped": False,
        "executed": False,
        "id": pid,
        "status": item["status"],
        "method": item["method"],
        "executable": item["executable"],
        "from": item["from"],
        "sender_email": item.get("sender_email"),
        "sender_domain": item.get("sender_domain"),
        "subject": item["subject"],
        "message_id": message_id,
        "first_seen": True,
        "note": "Queued for approval — first time seen for this message/target.",
    }


def count_open_pending() -> int:
    """Count proposals still awaiting operator action (not page-sliced)."""
    pending = load_pending()
    return sum(
        1
        for i in (pending.get("items") or {}).values()
        if i.get("status") in OPEN_STATUSES
    )


def list_pending(limit: int = 50) -> dict:
    pending = load_pending()
    items = [
        i
        for i in (pending.get("items") or {}).values()
        if i.get("status") in OPEN_STATUSES
    ]
    # newest first
    items.sort(key=lambda x: x.get("ts", ""), reverse=True)
    open_total = len(items)
    # Treat limit=0 as empty page; do not coerce 0 → default via `limit or 50`.
    try:
        lim = 50 if limit is None else int(limit)
    except (TypeError, ValueError):
        lim = 50
    lim = max(0, lim)
    page = items[:lim]
    return {
        "count": open_total,
        "returned": len(page),
        "open_total": open_total,
        "items": [
            {
                "id": i.get("id"),
                "status": i.get("status"),
                "category": i.get("category"),
                "method": i.get("method"),
                "executable": i.get("executable"),
                "from": i.get("from"),
                "subject": i.get("subject"),
                "message_id": i.get("message_id"),
                "target": (i.get("target") or "")[:120],
                "ts": i.get("ts"),
            }
            for i in page
        ],
        "hint": "User must explicitly approve ids. Then call approve_unsubscribe.",
    }


def _normalize_ids(args: dict) -> list[str]:
    ids = []
    if args.get("id"):
        ids.append(str(args["id"]).strip())
    for x in args.get("ids") or []:
        if x:
            ids.append(str(x).strip())
    # unique preserve order
    out = []
    for i in ids:
        if i and i not in out:
            out.append(i)
    return out


def _looks_like_gmail_message_id(value: str) -> bool:
    v = (value or "").strip().lower()
    # Gmail API ids are typically 16+ hex; our pending ids are 12 hex.
    return bool(v) and len(v) >= 14 and all(c in "0123456789abcdef" for c in v)


def resolve_to_pending_id(token: str, force: bool = False, create_if_missing: bool = False) -> dict:
    """Map a pending id or Gmail message_id to a pending proposal id.

    By default does NOT create proposals (approve must target an existing pending id).
    Set create_if_missing=True only for explicit propose workflows.
    """
    token = (token or "").strip()
    pending = load_pending()
    items = pending.get("items") or {}
    if token in items:
        return {"ok": True, "pending_id": token, "created": False, "item": items[token]}

    # Match existing pending by message_id (prefer pending/executable)
    matches = [it for it in items.values() if it.get("message_id") == token]
    if matches:
        order = {"pending": 0, "needs_manual": 1, "blocked": 2, "failed": 3, "done": 4, "rejected": 5}
        matches.sort(key=lambda x: (order.get(x.get("status"), 9), x.get("ts") or ""))
        chosen = matches[0]
        return {"ok": True, "pending_id": chosen["id"], "created": False, "item": chosen, "resolved_from": "message_id"}

    if not _looks_like_gmail_message_id(token):
        return {"ok": False, "error": f"unknown pending id: {token}", "token": token}

    if not create_if_missing:
        return {
            "ok": False,
            "error": "message_id is not in the pending queue — propose_unsubscribe first, then approve the pending id",
            "token": token,
        }

    proposed = propose_unsubscribe(token, "USER", force=force)
    if not proposed.get("ok"):
        return {"ok": False, "error": proposed.get("error") or "propose failed", "propose": proposed, "token": token}
    pid = proposed.get("id")
    if not pid:
        return {
            "ok": False,
            "error": proposed.get("reason") or "could not create pending proposal",
            "propose": proposed,
            "token": token,
        }
    pending = load_pending()
    item = (pending.get("items") or {}).get(pid)
    return {
        "ok": True,
        "pending_id": pid,
        "created": True,
        "item": item,
        "propose": proposed,
        "resolved_from": "message_id_proposed",
    }


def approve_unsubscribe(ids: list[str]) -> dict:
    if not ids:
        return {"ok": False, "error": "ids required — pass pending proposal ids (from list_pending_unsubscribes)"}
    pending = load_pending()
    seen = load_seen()
    results = []
    for token in ids:
        # Never auto-propose or force-suppress-bypass from approve.
        resolved = resolve_to_pending_id(token, force=False, create_if_missing=False)
        if not resolved.get("ok"):
            results.append({"id": token, "ok": False, "error": resolved.get("error"), "detail": resolved})
            continue
        pid = resolved["pending_id"]
        pending = load_pending()
        item = pending.get("items", {}).get(pid) or resolved.get("item")
        if not item:
            results.append({"id": token, "pending_id": pid, "ok": False, "error": "pending item missing after resolve"})
            continue
        if item.get("status") in {"done", "rejected"}:
            results.append({"id": pid, "ok": False, "error": f"already {item['status']}"})
            continue
        if not item.get("executable"):
            results.append(
                {
                    "id": pid,
                    "input": token,
                    "ok": False,
                    "error": "not auto-executable (page-based). Reject or handle manually.",
                    "method": item.get("method"),
                    "target": item.get("target"),
                    "message_id": item.get("message_id"),
                }
            )
            continue

        # Re-fetch live headers; refuse if pending target no longer matches.
        mid = item.get("message_id")
        if mid:
            live = fetch_headers(mid)
            live_target = live.get("target")
            live_method = live.get("method")
            if live_target and item.get("target") and live_target != item.get("target"):
                results.append(
                    {
                        "id": pid,
                        "ok": False,
                        "error": "pending target mismatch vs live List-Unsubscribe header — re-propose",
                        "pending_target": item.get("target"),
                        "live_target": live_target,
                    }
                )
                continue
            if live_method and item.get("method") and live_method != item.get("method"):
                results.append(
                    {
                        "id": pid,
                        "ok": False,
                        "error": "pending method mismatch vs live headers — re-propose",
                        "pending_method": item.get("method"),
                        "live_method": live_method,
                    }
                )
                continue
            # Prefer live target/method when present
            if live_target:
                item["target"] = live_target
            if live_method:
                item["method"] = live_method

        method = item.get("method")
        target = item.get("target")
        key = target_key(target) if target else pid
        if seen.get(key):
            item["status"] = "done"
            item["result"] = {"status": "already_done", "key": key}
            pending["items"][pid] = item
            watch_entry = add_post_unsub_watch(
                item.get("from") or "",
                pending_id=pid,
                message_id=item.get("message_id"),
                method=method,
                approved_at=item.get("approved_ts") or time.strftime("%Y-%m-%dT%H:%M:%S"),
            )
            results.append({
                "id": pid,
                "input": token,
                "ok": True,
                "status": "already_done",
                "method": method,
                "message_id": item.get("message_id"),
                "watch": watch_entry,
            })
            continue

        if method == "one_click":
            action = do_one_click(target)
        elif method == "mailto":
            action = do_mailto(target)
        else:
            results.append({"id": pid, "ok": False, "error": f"unsupported method {method}"})
            continue

        ok = action.get("status") == "ok" or action.get("http_status") in {200, 201, 202, 204}
        item["status"] = "done" if ok else "failed"
        item["result"] = action
        item["approved_ts"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        pending["items"][pid] = item
        watch_entry = None
        if ok:
            seen[key] = {
                "ts": item["approved_ts"],
                "message_id": item.get("message_id"),
                "pending_id": pid,
                "method": method,
            }
            watch_entry = add_post_unsub_watch(
                item.get("from") or "",
                pending_id=pid,
                message_id=item.get("message_id"),
                method=method,
                approved_at=item["approved_ts"],
            )
        results.append({
            "id": pid,
            "input": token,
            "ok": ok,
            "method": method,
            "action": action,
            "message_id": item.get("message_id"),
            "watch": watch_entry,
        })
        append_jsonl(
            LOG_FILE,
            {
                "ts": item["approved_ts"],
                "event": "approve",
                "id": pid,
                "input": token,
                "ok": ok,
                "method": method,
                "action": action,
                "watch_key": (watch_entry or {}).get("key") if watch_entry else None,
            },
        )

    save_pending(pending)
    save_seen(seen)
    ok_all = all(r.get("ok") for r in results) if results else False
    parts = []
    for r in results[:20]:
        pid = r.get("id") or r.get("pending_id") or r.get("input") or "?"
        frm = ""
        item = (pending.get("items") or {}).get(str(pid)) or {}
        if item.get("from"):
            frm = f" · {str(item.get('from'))[:60]}"
        if r.get("ok"):
            parts.append(f"• `{pid}`{frm} — unsubscribed ({r.get('status') or r.get('method') or 'ok'})")
        else:
            parts.append(f"• `{pid}`{frm} — fail: {str(r.get('error') or 'failed')[:100]}")
    note = "Unsub approve:\n" + "\n".join(parts) if parts else "Unsub approve: no results"
    return {"ok": ok_all, "results": results, "note": note}


def reject_unsubscribe(
    ids: list[str],
    suppress_sender: bool = True,
    suppress_scope: str = "domain",
) -> dict:
    if not ids:
        return {"ok": False, "error": "ids required"}
    pending = load_pending()
    results = []
    for pid in ids:
        item = pending.get("items", {}).get(pid)
        if not item:
            results.append({"id": pid, "ok": False, "error": "unknown pending id"})
            continue
        item["status"] = "rejected"
        item["rejected_ts"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        suppressed_entry = None
        dismissed: list[str] = []
        if suppress_sender:
            suppressed_entry = add_suppressed_sender(
                item.get("from") or "",
                scope=suppress_scope,
                reason="rejected",
                pending_id=pid,
            )
            if suppressed_entry:
                item["suppressed"] = suppressed_entry
                scope = suppressed_entry.get("scope") or suppress_scope
                key = suppressed_entry.get("key")
                dismissed = dismiss_pending_for_suppressed(pending, key, scope)
        pending["items"][pid] = item
        results.append(
            {
                "id": pid,
                "ok": True,
                "status": "rejected",
                "suppressed": suppressed_entry,
                "also_dismissed_ids": [d for d in dismissed if d != pid],
            }
        )
        append_jsonl(
            LOG_FILE,
            {
                "ts": item["rejected_ts"],
                "event": "reject",
                "id": pid,
                "suppress_sender": suppress_sender,
                "suppressed": suppressed_entry,
            },
        )
    save_pending(pending)
    return {
        "ok": True,
        "results": results,
        "note": (
            "Sender suppressed for future proposes"
            if suppress_sender
            else "Dismissed this proposal only; sender may be proposed again"
        ),
    }


def list_suppressed_senders() -> dict:
    data = load_suppressed()
    entries = list((data.get("entries") or {}).values())
    entries.sort(key=lambda x: x.get("ts", ""), reverse=True)
    return {"count": len(entries), "entries": entries}


def unsuppress_sender(key: str) -> dict:
    key = (key or "").strip().lower()
    if not key:
        return {"ok": False, "error": "key required"}
    data = load_suppressed()
    entries = data.get("entries") or {}
    if key not in entries:
        return {"ok": False, "error": f"not suppressed: {key}", "entries": list(entries.keys())}
    removed = entries.pop(key)
    data["entries"] = entries
    save_suppressed(data)
    append_jsonl(
        LOG_FILE,
        {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": "unsuppress", "key": key},
    )
    return {"ok": True, "removed": removed}


def suppress_sender(key: str, scope: str = "domain") -> dict:
    """Exclude email/domain from future proposes; dismiss matching open pending ids."""
    raw = (key or "").strip()
    if not raw:
        return {"ok": False, "error": "key required"}
    scope = (scope or "domain").strip().lower()
    if scope not in {"domain", "email"}:
        scope = "domain"

    email = extract_sender_email(raw)
    if not email and "@" in raw and " " not in raw:
        email = raw.strip().lower()

    if email:
        entry = add_suppressed_sender(
            f"<{email}>",
            scope=scope,
            reason="operator_suppress",
        )
        if not entry:
            return {"ok": False, "error": "could not parse email", "key": raw}
    else:
        if scope == "email":
            return {"ok": False, "error": "scope=email requires an email address", "key": raw}
        domain = raw.lower().lstrip("@").strip()
        if not domain or "@" in domain or "/" in domain or " " in domain:
            return {"ok": False, "error": f"invalid domain: {raw}", "key": raw}
        data = load_suppressed()
        entry = {
            "key": domain,
            "scope": "domain",
            "example_email": None,
            "example_from": None,
            "reason": "operator_suppress",
            "pending_id": None,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        data["entries"][domain] = entry
        save_suppressed(data)

    sk = entry.get("key") or ""
    sc = entry.get("scope") or "domain"
    pending = load_pending()
    dismissed = dismiss_pending_for_suppressed(pending, sk, sc)
    if dismissed:
        save_pending(pending)
    append_jsonl(
        LOG_FILE,
        {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "event": "suppress",
            "key": sk,
            "scope": sc,
            "dismissed_ids": dismissed,
        },
    )
    return {
        "ok": True,
        "suppressed": entry,
        "dismissed_ids": dismissed,
        "note": (
            f"Suppressed `{sk}` ({sc}); dismissed {len(dismissed)} pending"
            if dismissed
            else f"Suppressed `{sk}` ({sc}); no open pending matched"
        ),
    }


def call_tool(name: str, args: dict):
    if name == "propose_unsubscribe":
        return propose_unsubscribe(
            str(args.get("message_id", "")).strip(),
            str(args.get("category", "")).strip(),
            force=bool(args.get("force", False)),
        )
    if name == "list_pending_unsubscribes":
        return list_pending(int(args.get("limit", 50) or 50))
    if name == "approve_unsubscribe":
        return approve_unsubscribe(_normalize_ids(args))
    if name == "reject_unsubscribe":
        suppress = args.get("suppress_sender")
        if suppress is None:
            suppress = True
        scope = str(args.get("suppress_scope") or "domain")
        return reject_unsubscribe(_normalize_ids(args), suppress_sender=bool(suppress), suppress_scope=scope)
    if name == "list_suppressed_senders":
        return list_suppressed_senders()
    if name == "unsuppress_sender":
        return unsuppress_sender(str(args.get("key", "")))
    if name == "suppress_sender":
        return suppress_sender(
            str(args.get("key", "")),
            scope=str(args.get("scope") or "domain"),
        )
    if name == "list_post_unsub_watch":
        return list_post_unsub_watch()
    if name == "clear_post_unsub_watch":
        return clear_post_unsub_watch(str(args.get("key", "")))
    # legacy name → refuse live
    if name == "unsubscribe_message":
        return {
            "ok": False,
            "error": "unsubscribe_message removed. Use propose_unsubscribe, then approve_unsubscribe after user approval.",
        }
    if name == "list_unsubscribe_review":
        return list_pending(int(args.get("limit", 50) or 50))
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
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if len(sys.argv) > 1:
        cmd = sys.argv[1]
        if cmd == "--propose":
            args = sys.argv[2:]
            force = "--force" in args
            args = [a for a in args if a != "--force"]
            mid = args[0] if args else ""
            cat = args[1] if len(args) > 1 else "USER"
            print(json.dumps(propose_unsubscribe(mid, cat, force=force), indent=2))
            return
        if cmd == "--pending":
            print(json.dumps(list_pending(100), indent=2))
            return
        if cmd == "--approve":
            print(json.dumps(approve_unsubscribe(sys.argv[2:]), indent=2))
            return
        if cmd == "--reject":
            # --reject ID [ID...] [--once] [--email]
            args = sys.argv[2:]
            once = "--once" in args
            email_scope = "--email" in args
            ids = [a for a in args if not a.startswith("--")]
            print(
                json.dumps(
                    reject_unsubscribe(
                        ids,
                        suppress_sender=not once,
                        suppress_scope="email" if email_scope else "domain",
                    ),
                    indent=2,
                )
            )
            return
        if cmd == "--suppressed":
            print(json.dumps(list_suppressed_senders(), indent=2))
            return
        if cmd == "--suppress":
            # --suppress KEY [--email]
            args = sys.argv[2:]
            email_scope = "--email" in args
            keys = [a for a in args if not a.startswith("--")]
            key = keys[0] if keys else ""
            print(
                json.dumps(
                    suppress_sender(key, scope="email" if email_scope else "domain"),
                    indent=2,
                )
            )
            return
        if cmd == "--unsuppress":
            print(json.dumps(unsuppress_sender(sys.argv[2] if len(sys.argv) > 2 else ""), indent=2))
            return
        if cmd == "--watch":
            print(json.dumps(list_post_unsub_watch(), indent=2))
            return
        if cmd == "--unwatch":
            print(json.dumps(clear_post_unsub_watch(sys.argv[2] if len(sys.argv) > 2 else ""), indent=2))
            return
        if cmd == "--watch-add":
            # --watch-add FROM [--grace N] [--days-ago N] [--scope email|domain]
            args = sys.argv[2:]
            grace = POST_UNSUB_GRACE_DAYS
            days_ago = 0
            scope = POST_UNSUB_SCOPE
            positional = []
            i = 0
            while i < len(args):
                if args[i] == "--grace" and i + 1 < len(args):
                    grace = int(args[i + 1])
                    i += 2
                    continue
                if args[i] == "--days-ago" and i + 1 < len(args):
                    days_ago = float(args[i + 1])
                    i += 2
                    continue
                if args[i] == "--scope" and i + 1 < len(args):
                    scope = args[i + 1]
                    i += 2
                    continue
                positional.append(args[i])
                i += 1
            from_hdr = positional[0] if positional else ""
            epoch = time.time() - max(0.0, days_ago) * 86400
            entry = add_post_unsub_watch(
                from_hdr,
                grace_days=grace,
                scope=scope,
                approved_at_epoch=epoch,
            )
            print(json.dumps({"ok": bool(entry), "entry": entry}, indent=2))
            return
        print(
            "usage: --propose MSG CAT | --pending | --approve ID... | "
            "--reject ID... [--once] [--email] | --suppress KEY [--email] | "
            "--suppressed | --unsuppress KEY | "
            "--watch | --unwatch KEY | --watch-add FROM [--grace N] [--days-ago N] [--scope email|domain]",
            file=sys.stderr,
        )
        sys.exit(2)

    log(f"list_unsubscribe MCP starting (propose/approve, protocol {PROTOCOL_VERSION})")
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
