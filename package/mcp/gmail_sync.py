#!/usr/bin/env python3
"""Gmail -> Chroma inbox sync. Stdlib only."""
from __future__ import annotations

import base64
import datetime
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import _config as cfg

GMAIL_CREDS = str(cfg.gmail_creds())
GMAIL_KEYS = str(cfg.gmail_keys())
CHROMA_TENANT = os.environ.get("CHROMA_TENANT", "default_tenant").strip() or "default_tenant"
CHROMA_DB = os.environ.get("CHROMA_DATABASE", "default_database").strip() or "default_database"
CHROMA_COLL = cfg.chroma_collection()
EMBED_MODEL = cfg.embed_model()
LOOKBACK_DAYS = int(os.environ.get("GMAIL_SYNC_LOOKBACK_DAYS", "30"))
MAX_EMAILS = int(os.environ.get("GMAIL_SYNC_MAX_EMAILS", "50"))
PAGE_SIZE = 50


def _chroma_url():
    return cfg.chroma_url()


def _embed_url():
    return cfg.embed_url()


def _http(url, data=None, headers=None, method=None, timeout=120):
    if data is not None and not isinstance(data, (bytes, bytearray)):
        data = json.dumps(data).encode()
    req = urllib.request.Request(url, data=data, method=method or ("POST" if data else "GET"))
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    return json.loads(raw.decode()) if raw else None


def load_creds():
    return json.loads(Path(GMAIL_CREDS).read_text(encoding="utf-8"))


def save_creds(creds):
    cfg.atomic_write_json(Path(GMAIL_CREDS), creds)


def refresh_if_expired(creds):
    exp_ms = creds.get("expiry_date")
    if exp_ms and datetime.datetime.fromtimestamp(exp_ms / 1000) > datetime.datetime.now() + datetime.timedelta(seconds=30):
        return creds
    keys = json.loads(Path(GMAIL_KEYS).read_text(encoding="utf-8"))
    if "installed" in keys:
        cid, cs = keys["installed"]["client_id"], keys["installed"]["client_secret"]
    else:
        cid, cs = keys["client_id"], keys["client_secret"]
    body = urllib.parse.urlencode({
        "client_id": cid, "client_secret": cs,
        "refresh_token": creds["refresh_token"], "grant_type": "refresh_token",
    }).encode()
    req = urllib.request.Request("https://oauth2.googleapis.com/token", data=body, method="POST")
    with urllib.request.urlopen(req, timeout=15) as r:
        tok = json.loads(r.read().decode())
    creds["access_token"] = tok["access_token"]
    creds["expiry_date"] = int((datetime.datetime.now() + datetime.timedelta(seconds=tok.get("expires_in", 3600))).timestamp() * 1000)
    if "scope" in tok:
        creds["scope"] = tok["scope"]
    if "token_type" in tok:
        creds["token_type"] = tok["token_type"]
    return creds


_LABEL_CACHE: dict[str, str] | None = None


def gmail_label_names(label_ids) -> list[str]:
    """Resolve Gmail label IDs to human names (cached)."""
    global _LABEL_CACHE
    if _LABEL_CACHE is None:
        _LABEL_CACHE = {}
        try:
            data = gmail_get("labels")
            for lab in (data or {}).get("labels") or []:
                lid, name = lab.get("id"), lab.get("name")
                if lid and name:
                    _LABEL_CACHE[str(lid)] = str(name)
        except Exception as exc:
            print(f"  warning: could not load Gmail labels: {exc}", file=sys.stderr)
    names = []
    for lid in label_ids or []:
        names.append(_LABEL_CACHE.get(str(lid), str(lid)))
    return names


def gmail_get(path, params=None):
    creds = refresh_if_expired(load_creds())
    save_creds(creds)
    url = "https://gmail.googleapis.com/gmail/v1/users/me/" + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    return _http(url, headers={"Authorization": "Bearer " + creds["access_token"]})


def list_recent(lookback_days, max_n):
    since = int((datetime.datetime.now() - datetime.timedelta(days=lookback_days)).timestamp())
    out, page_token = [], None
    while len(out) < max_n:
        params = {
            "q": "after:" + str(since),
            "maxResults": min(PAGE_SIZE, max_n - len(out)),
        }
        if page_token:
            params["pageToken"] = page_token
        r = gmail_get("messages", params)
        batch = r.get("messages") or []
        out.extend(batch)
        page_token = r.get("nextPageToken")
        if not page_token or not batch:
            break
    return out[:max_n]


def get_email(msg_id):
    return gmail_get("messages/" + msg_id, {"format": "full"})


def extract_body(payload):
    out = []

    def walk(p):
        if p.get("mimeType", "").startswith("text/plain") and p.get("body", {}).get("data"):
            out.append(base64.urlsafe_b64decode(p["body"]["data"]).decode("utf-8", errors="replace"))
        for s in p.get("parts", []):
            walk(s)

    if payload.get("parts"):
        for p in payload["parts"]:
            walk(p)
    elif payload.get("mimeType", "").startswith("text/plain") and payload.get("body", {}).get("data"):
        walk(payload)
    return "\n\n".join(out) or "(no plain text body)"


def extract_headers(headers):
    return {h["name"].lower(): h["value"] for h in headers}


def embed(text):
    """Embed with retries; keep inputs short for small embedding batch limits."""
    last_err = None
    for limit in (2000, 1500, 1000, 700, 400, 200):
        body = {"model": EMBED_MODEL, "input": text[:limit]}
        try:
            r = _http(_embed_url(), data=body, headers={"Content-Type": "application/json"}, timeout=60)
            return r["data"][0]["embedding"]
        except Exception as e:
            last_err = e
            continue
    raise RuntimeError(f"embed failed after truncations: {last_err}")


def chroma_collection_id():
    url = f"{_chroma_url()}/api/v2/tenants/{CHROMA_TENANT}/databases/{CHROMA_DB}/collections"
    cols = _http(url)
    for c in cols:
        if c["name"] == CHROMA_COLL:
            return c["id"]
    raise ValueError(f"{CHROMA_COLL} not found")


def chroma_existing_ids(candidate_ids):
    if not candidate_ids:
        return set(), None
    try:
        cid = chroma_collection_id()
        url = f"{_chroma_url()}/api/v2/tenants/{CHROMA_TENANT}/databases/{CHROMA_DB}/collections/{cid}/get"
        data = _http(url, data={"ids": candidate_ids, "include": []}, headers={"Content-Type": "application/json"})
        return set(data.get("ids") or []), None
    except Exception as exc:
        return set(), exc


def chroma_upsert(ids, embs, docs, metas):
    """Upsert batch. Returns (ok_count, fail_count)."""
    cid = chroma_collection_id()
    payload = {"ids": ids, "embeddings": embs, "documents": docs, "metadatas": metas}
    headers = {"Content-Type": "application/json"}
    base = f"{_chroma_url()}/api/v2/tenants/{CHROMA_TENANT}/databases/{CHROMA_DB}/collections/{cid}"
    for endpoint in ("add", "upsert", "update"):
        try:
            _http(f"{base}/{endpoint}", data=payload, headers=headers, timeout=120)
            return len(ids), 0
        except Exception:
            continue
    ok, fail = 0, 0
    for i in range(len(ids)):
        one = {"ids": [ids[i]], "embeddings": [embs[i]], "documents": [docs[i]], "metadatas": [metas[i]]}
        try:
            _http(f"{base}/add", data=one, headers=headers, timeout=120)
            ok += 1
        except Exception:
            try:
                _http(f"{base}/update", data=one, headers=headers, timeout=120)
                ok += 1
            except Exception as e2:
                fail += 1
                print(f"  chroma fail id={ids[i][:14]}: {e2}", file=sys.stderr)
    return ok, fail


def main():
    print(f"=== Gmail sync starting (lookback={LOOKBACK_DAYS}d, max={MAX_EMAILS}) ===")
    try:
        msgs = list_recent(LOOKBACK_DAYS, MAX_EMAILS)
    except Exception as exc:
        print(f"=== Sync FAILED listing Gmail: {exc} ===", file=sys.stderr)
        return 1
    print(f"  found {len(msgs)} messages")
    if not msgs:
        print("  nothing to do")
        return 0
    candidate_ids = [m["id"] for m in msgs]
    existing, existing_err = chroma_existing_ids(candidate_ids)
    if existing_err is not None:
        print(f"=== Sync FAILED reading Chroma: {existing_err} ===", file=sys.stderr)
        return 1
    print(f"  already indexed: {len(existing)}; to process: {len(candidate_ids) - len(existing)}")
    ids, embs, docs, metas = [], [], [], []
    skipped = 0
    embed_failures = 0
    upsert_ok = 0
    upsert_fail = 0

    def flush_batch():
        nonlocal ids, embs, docs, metas, upsert_ok, upsert_fail
        if not ids:
            return
        ok, fail = chroma_upsert(ids, embs, docs, metas)
        upsert_ok += ok
        upsert_fail += fail
        print(f"  upserted batch ok={ok} fail={fail}")
        import time
        time.sleep(0.2)
        ids, embs, docs, metas = [], [], [], []

    for m in msgs:
        if m["id"] in existing:
            skipped += 1
            continue
        try:
            full = get_email(m["id"])
            hdrs = extract_headers(full.get("payload", {}).get("headers", []))
            body = extract_body(full.get("payload", {}))
            snippet = body[:200].replace("\n", " ")
            doc_text = (
                f"Subject: {hdrs.get('subject', '(no subject)')}\n"
                f"From: {hdrs.get('from', '?')}\n"
                f"Date: {hdrs.get('date', '?')}\n\n{body}"
            )
            v = embed(doc_text)
        except Exception as e:
            embed_failures += 1
            print(f"  SKIP {m['id'][:14]}... embed/fetch error: {e}", file=sys.stderr)
            continue
        label_ids = full.get("labelIds", []) or []
        label_names = gmail_label_names(label_ids)
        ids.append(full["id"])
        embs.append(v)
        docs.append(doc_text)
        metas.append({
            "source": "gmail", "message_id": full["id"], "thread_id": full.get("threadId", ""),
            "from_addr": hdrs.get("from", ""), "to_addr": hdrs.get("to", ""),
            "subject": hdrs.get("subject", ""), "date": hdrs.get("date", ""),
            "snippet": snippet,
            "labels": ",".join(label_ids),
            "label_names": ",".join(label_names),
            "status": "Indexed",
            "indexed_at": int(__import__("time").time()),
        })
        print(f"  indexed {full['id'][:14]}... subj={hdrs.get('subject', '?')[:50]!r}")
        if len(ids) >= 5:
            flush_batch()
    flush_batch()
    to_process = len(candidate_ids) - skipped
    print(
        f"=== Sync complete: upsert_ok={upsert_ok} upsert_fail={upsert_fail} "
        f"embed_fail={embed_failures} skipped_existing={skipped}; collection={CHROMA_COLL} ==="
    )
    if to_process > 0 and upsert_ok == 0 and embed_failures >= to_process:
        return 1
    if upsert_fail > 0 and upsert_ok == 0:
        return 1
    if upsert_fail > upsert_ok and upsert_fail > 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
