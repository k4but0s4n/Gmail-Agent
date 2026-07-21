#!/usr/bin/env python3
"""email_query MCP — semantic search + paginated list_recent with unread/skip filters."""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import _config as cfg

EMBED_MODEL = cfg.embed_model()
COLLECTION = cfg.chroma_collection()
STATE_DIR = cfg.state_dir()
TRIAGE_SEEN = STATE_DIR / "triage_seen.json"
LABEL_PREFIX = cfg.label_prefix()

MAX_TOP_K = max(1, min(int(os.environ.get("GMAIL_EMAIL_QUERY_MAX_TOP_K", "200")), 500))
LIST_RECENT_MAX = max(1, min(int(os.environ.get("GMAIL_LIST_RECENT_MAX", "50")), 200))
EXCERPT_CHARS = max(40, min(int(os.environ.get("GMAIL_LIST_EXCERPT_CHARS", "120")), 300))
LOCAL_TZ = ZoneInfo(os.environ.get("GMAIL_TRIAGE_TZ", "America/New_York"))
SERVER_INFO = {"name": "email-query", "version": "0.4.0"}
PROTOCOL_VERSION = "2024-11-05"


def log(msg):
    print(f"[email_query] {msg}", file=sys.stderr, flush=True)


def _http(url, data=None, headers=None, method=None, timeout=30):
    if data is not None and not isinstance(data, (bytes, bytearray)):
        data = json.dumps(data).encode()
    req = urllib.request.Request(url, data=data, method=method or ("POST" if data else "GET"))
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
        return json.loads(raw.decode()) if raw else None
    except urllib.error.HTTPError as e:
        log(f"HTTP {e.code} from {url}: {e.read().decode(errors='replace')[:300]}")
        return None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        log(f"network error from {url}: {e}")
        return None


def embed(text):
    r = _http(
        cfg.embed_url(),
        data={"model": EMBED_MODEL, "input": text[:1000]},
        headers={"Content-Type": "application/json"},
    )
    if not r or "data" not in r:
        raise RuntimeError("embedder returned no data (check GMAIL_EMBED_URL)")
    return r["data"][0]["embedding"]


def retrieve(query, top_k, filters):
    vec = embed(query)
    body = {
        "query": query,
        "query_embedding": vec,
        "collections": [COLLECTION],
        "top_k": top_k,
        "filters": filters,
    }
    r = _http(cfg.retrieve_url(), data=body, headers={"Content-Type": "application/json"})
    if not r or "results" not in r:
        raise RuntimeError("retrieval API returned no results (check GMAIL_RETRIEVE_URL)")
    return r


def collection_id():
    cols = _http(cfg.chroma_api_base() + "/collections")
    if not cols:
        raise RuntimeError("chroma collections unreachable (check CHROMA_URL)")
    for c in cols:
        if c.get("name") == COLLECTION:
            return c["id"]
    raise RuntimeError(f"collection {COLLECTION} not found")


def _date_key(meta: dict) -> float:
    raw = str((meta or {}).get("date") or "")
    try:
        return parsedate_to_datetime(raw).timestamp()
    except Exception:
        return 0.0


def _truthy(v, default=False):
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in {"1", "true", "yes", "y", "on"}


def load_triage_seen() -> dict:
    if not TRIAGE_SEEN.exists():
        return {}
    try:
        data = json.loads(TRIAGE_SEEN.read_text(encoding="utf-8") or "{}")
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def label_set(meta: dict) -> set[str]:
    raw = str((meta or {}).get("labels") or "")
    return {p.strip() for p in raw.replace(";", ",").split(",") if p.strip()}


def _cutoff_ts(since_today: bool, max_age_hours: float | None) -> float | None:
    """Earliest acceptable message timestamp (epoch seconds), or None for no age filter."""
    now_local = datetime.now(LOCAL_TZ)
    cutoffs = []
    if since_today:
        start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        cutoffs.append(start.timestamp())
    if max_age_hours is not None and max_age_hours > 0:
        cutoffs.append((now_local - timedelta(hours=float(max_age_hours))).timestamp())
    return max(cutoffs) if cutoffs else None


_LABEL_ID_TO_NAME: dict[str, str] | None = None


def _gmail_label_id_to_name() -> dict[str, str]:
    """Best-effort Gmail label id → name map (cached). Empty if creds unavailable."""
    global _LABEL_ID_TO_NAME
    if _LABEL_ID_TO_NAME is not None:
        return _LABEL_ID_TO_NAME
    _LABEL_ID_TO_NAME = {}
    try:
        creds_path = cfg.gmail_creds()
        keys_path = cfg.gmail_keys()
        if not creds_path.exists() or not keys_path.exists():
            return _LABEL_ID_TO_NAME
        creds = json.loads(creds_path.read_text(encoding="utf-8"))
        keys = json.loads(keys_path.read_text(encoding="utf-8"))
        inst = keys.get("installed") or keys.get("web") or keys
        exp_ms = creds.get("expiry_date") or 0
        if exp_ms / 1000 <= time.time() + 30:
            body = urllib.parse.urlencode(
                {
                    "client_id": inst["client_id"],
                    "client_secret": inst["client_secret"],
                    "refresh_token": creds["refresh_token"],
                    "grant_type": "refresh_token",
                }
            ).encode()
            tok = _http(
                "https://oauth2.googleapis.com/token",
                data=body,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            if not tok or "access_token" not in tok:
                return _LABEL_ID_TO_NAME
            creds["access_token"] = tok["access_token"]
            creds["expiry_date"] = int(time.time() * 1000) + int(tok.get("expires_in", 3600)) * 1000
            cfg.atomic_write_json(creds_path, creds)
        data = _http(
            "https://gmail.googleapis.com/gmail/v1/users/me/labels",
            headers={"Authorization": "Bearer " + creds["access_token"]},
        )
        for lab in (data or {}).get("labels") or []:
            lid, name = lab.get("id"), lab.get("name")
            if lid and name:
                _LABEL_ID_TO_NAME[str(lid)] = str(name)
    except Exception as exc:
        log(f"label map unavailable: {exc}")
    return _LABEL_ID_TO_NAME


def _normalized_label_names(labs: set[str]) -> set[str]:
    """Expand Gmail label IDs to names so skip_labeled can match PREFIX/*."""
    if not labs:
        return set()
    id_map = _gmail_label_id_to_name()
    out: set[str] = set()
    for lab in labs:
        out.add(lab)
        if lab in id_map:
            out.add(id_map[lab])
    return out


def list_recent_docs(
    limit: int,
    offset: int,
    unread_only: bool,
    skip_labeled: bool,
    skip_seen: bool,
    since_today: bool = False,
    max_age_hours: float | None = None,
):
    """Newest-first after filters, then slice [offset:offset+limit]."""
    cid = collection_id()
    fetch_n = min(1000, 1000)
    body = {"limit": fetch_n, "include": ["metadatas", "documents"]}
    data = _http(
        cfg.chroma_api_base() + f"/collections/{cid}/get",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    if not data:
        raise RuntimeError("chroma get failed")
    ids = data.get("ids") or []
    metas = data.get("metadatas") or []
    docs = data.get("documents") or []
    seen = load_triage_seen() if skip_seen else {}
    cutoff = _cutoff_ts(since_today, max_age_hours)

    rows = []
    skipped = {
        "not_unread": 0,
        "already_oc_labeled": 0,
        "already_triaged": 0,
        "not_gmail": 0,
        "too_old": 0,
    }
    for i, mid in enumerate(ids):
        m = metas[i] if i < len(metas) else {}
        m = m or {}
        if m.get("source") and m.get("source") != "gmail":
            skipped["not_gmail"] += 1
            continue
        message_id = str(m.get("message_id") or mid)
        labs = label_set(m)
        labs |= label_set({"labels": m.get("label_names") or ""})
        if unread_only and "UNREAD" not in labs:
            skipped["not_unread"] += 1
            continue
        label_pfx = f"{LABEL_PREFIX}/"
        names = _normalized_label_names(labs) if skip_labeled else labs
        if skip_labeled and any(l.startswith(label_pfx) for l in names):
            skipped["already_oc_labeled"] += 1
            continue
        if skip_seen and message_id in seen:
            skipped["already_triaged"] += 1
            continue
        sort_ts = _date_key(m)
        if cutoff is not None and (sort_ts <= 0 or sort_ts < cutoff):
            skipped["too_old"] += 1
            continue
        excerpt = (docs[i] if i < len(docs) else "") or m.get("snippet") or ""
        rows.append(
            {
                "message_id": message_id,
                "from_addr": m.get("from_addr") or "?",
                "subject": m.get("subject") or "(no subject)",
                "date": m.get("date") or "?",
                "labels": ",".join(sorted(labs)) if labs else "",
                "excerpt": excerpt,
                "_sort": sort_ts,
            }
        )
    rows.sort(key=lambda r: r["_sort"], reverse=True)
    total = len(rows)
    page = rows[offset : offset + limit]
    for r in page:
        r.pop("_sort", None)
    return page, total, skipped


def format_results(resp):
    results = resp.get("results", [])
    if not results:
        lim = resp.get("limitations") or []
        return f"No matches in {COLLECTION}." + (f" Notes: {'; '.join(lim)}" if lim else "")
    lines = [f"Found {len(results)} email(s) (elapsed {resp.get('elapsed_ms', '?')}ms):"]
    for i, r in enumerate(results, 1):
        m = r.get("metadata", {})
        lines.append(f"\n[{i}] score={r.get('similarity_score', 0):.3f}")
        lines.append(f"    from:    {m.get('from_addr', '?')[:60]}")
        lines.append(f"    subject: {m.get('subject', '(no subject)')[:80]}")
        lines.append(f"    date:    {str(m.get('date', '?'))[:50]}")
        lines.append(f"    msg_id:  {m.get('message_id', '?')}")
        ex = r.get("excerpt", "")
        if len(ex) > EXCERPT_CHARS:
            ex = ex[:EXCERPT_CHARS] + "..."
        lines.append(f"    excerpt: {ex}")
    if resp.get("limitations"):
        lines.append(f"\nNotes: {'; '.join(resp['limitations'])}")
    return "\n".join(lines)


def format_list_recent(rows, offset, total, skipped, flags):
    if not rows:
        return (
            f"No emails matched filters in {COLLECTION} for offset={offset}. "
            f"eligible_total={total} skipped={skipped} flags={flags}"
        )
    lines = [
        f"Listed {len(rows)} email(s) (offset={offset}, eligible_total={total}, newest-first) flags={flags}",
        f"skipped={skipped}",
    ]
    for i, r in enumerate(rows, 1):
        lines.append(f"\n[{i}]")
        lines.append(f"    from:    {str(r.get('from_addr', '?'))[:60]}")
        lines.append(f"    subject: {str(r.get('subject', '(no subject)'))[:80]}")
        lines.append(f"    date:    {str(r.get('date', '?'))[:50]}")
        lines.append(f"    msg_id:  {r.get('message_id', '?')}")
        labs = str(r.get("labels") or "")
        if labs:
            lines.append(f"    labels:  {labs[:100]}")
        ex = str(r.get("excerpt") or "")
        if len(ex) > EXCERPT_CHARS:
            ex = ex[:EXCERPT_CHARS] + "..."
        lines.append(f"    excerpt: {ex}")
    return "\n".join(lines)


TOOL_EMAIL_QUERY = {
    "name": "email_query",
    "description": (
        "Semantic search across indexed Gmail. For triage batches prefer list_recent "
        "(unread_only/skip_labeled) with limit<=25–50."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "top_k": {"type": "integer", "default": 5},
        },
        "required": ["query"],
    },
}

TOOL_LIST_RECENT = {
    "name": "list_recent",
    "description": (
        "List indexed inbox emails newest-first with pagination. "
        f"Defaults: unread_only=true, skip_labeled=true (skip {LABEL_PREFIX}/*), skip_seen=true "
        "(skip ids in triage_seen.json). For recurring triage set since_today=true. "
        "Use limit<=25 for Gemma-safe finalize."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "default": 25},
            "offset": {"type": "integer", "default": 0},
            "unread_only": {"type": "boolean", "default": True},
            "skip_labeled": {
                "type": "boolean",
                "default": True,
                "description": f"Skip messages that already have any {LABEL_PREFIX}/* label",
            },
            "skip_seen": {
                "type": "boolean",
                "default": True,
                "description": "Skip message_ids recorded in triage_seen.json",
            },
            "since_today": {
                "type": "boolean",
                "default": False,
                "description": "Only emails dated on/after local midnight (America/New_York)",
            },
            "max_age_hours": {
                "type": "number",
                "description": "Optional extra age cap in hours (combined with since_today via latest cutoff)",
            },
        },
        "required": [],
    },
}


def call_email_query(args):
    query = str(args.get("query", "")).strip()
    if not query:
        return "email_query error: query is required"
    top_k = max(1, min(int(args.get("top_k", 5)), MAX_TOP_K))
    try:
        resp = retrieve(query, top_k, filters={"source": "gmail"})
    except Exception as e:
        return f"email_query error: {e}"
    return format_results(resp)


def call_list_recent(args):
    limit = max(1, min(int(args.get("limit", 25)), LIST_RECENT_MAX))
    offset = max(0, int(args.get("offset", 0)))
    unread_only = _truthy(args.get("unread_only"), True)
    skip_labeled = _truthy(args.get("skip_labeled"), True)
    skip_seen = _truthy(args.get("skip_seen"), True)
    since_today = _truthy(args.get("since_today"), False)
    max_age_hours = args.get("max_age_hours")
    if max_age_hours is not None and str(max_age_hours).strip() != "":
        try:
            max_age_hours = float(max_age_hours)
        except Exception:
            max_age_hours = None
    else:
        max_age_hours = None
    flags = {
        "unread_only": unread_only,
        "skip_labeled": skip_labeled,
        "skip_seen": skip_seen,
        "since_today": since_today,
        "max_age_hours": max_age_hours,
        "tz": str(LOCAL_TZ),
        "excerpt_chars": EXCERPT_CHARS,
    }
    try:
        rows, total, skipped = list_recent_docs(
            limit,
            offset,
            unread_only,
            skip_labeled,
            skip_seen,
            since_today=since_today,
            max_age_hours=max_age_hours,
        )
    except Exception as e:
        return f"list_recent error: {e}"
    return format_list_recent(rows, offset, total, skipped, flags)


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
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"tools": [TOOL_EMAIL_QUERY, TOOL_LIST_RECENT]},
        }
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        try:
            if name == "email_query":
                text = call_email_query(args)
            elif name == "list_recent":
                text = call_list_recent(args)
            else:
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32601, "message": f"unknown tool: {name}"},
                }
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"content": [{"type": "text", "text": text}]},
            }
        except Exception as e:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32603, "message": f"tool error: {e}"},
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
    log(f"email_query MCP starting v{SERVER_INFO['version']}")
    stdin = sys.stdin.buffer
    while True:
        try:
            msg = read_message(stdin)
        except (json.JSONDecodeError, ValueError) as e:
            log(f"parse error: {e}")
            continue
        if msg is None:
            log("stdin closed, exiting")
            break
        resp = handle_message(msg)
        if resp is not None:
            write_message(resp)


if __name__ == "__main__":
    main()
