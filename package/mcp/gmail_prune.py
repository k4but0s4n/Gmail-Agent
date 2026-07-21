#!/usr/bin/env python3
"""Prune Gmail Chroma collection to the last N days. Stdlib only."""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import _config as cfg

CHROMA_TENANT = os.environ.get("CHROMA_TENANT", "default_tenant").strip() or "default_tenant"
CHROMA_DB = os.environ.get("CHROMA_DATABASE", "default_database").strip() or "default_database"
CHROMA_COLL = cfg.chroma_collection()
KEEP_DAYS = int(os.environ.get("GMAIL_PRUNE_KEEP_DAYS", "90"))
PAGE = 200


def _chroma_url():
    return cfg.chroma_url()


def _http(url, data=None, headers=None, method=None, timeout=60):
    if data is not None and not isinstance(data, (bytes, bytearray)):
        data = json.dumps(data).encode()
    req = urllib.request.Request(url, data=data, method=method or ("POST" if data else "GET"))
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    return json.loads(raw.decode()) if raw else None


def collection_id():
    url = f"{_chroma_url()}/api/v2/tenants/{CHROMA_TENANT}/databases/{CHROMA_DB}/collections"
    cols = _http(url)
    for c in cols:
        if c.get("name") == CHROMA_COLL:
            return c["id"]
    raise SystemExit(f"collection {CHROMA_COLL} not found")


def parse_meta_date(meta):
    """Return timezone-aware UTC datetime, or None if unparseable."""
    if not meta:
        return None
    indexed = meta.get("indexed_at")
    if indexed is not None:
        try:
            v = float(indexed)
            if v > 1e12:
                v /= 1000.0
            return datetime.fromtimestamp(v, tz=timezone.utc)
        except (TypeError, ValueError, OSError):
            pass
    raw = meta.get("date") or ""
    if not raw:
        return None
    try:
        dt = parsedate_to_datetime(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None


def list_all(cid):
    """Page through all ids+metadatas."""
    ids, metas = [], []
    offset = 0
    url = f"{_chroma_url()}/api/v2/tenants/{CHROMA_TENANT}/databases/{CHROMA_DB}/collections/{cid}/get"
    while True:
        body = {"limit": PAGE, "offset": offset, "include": ["metadatas"]}
        try:
            data = _http(url, data=body, headers={"Content-Type": "application/json"})
        except urllib.error.HTTPError:
            body = {"limit": 10000, "include": ["metadatas"]}
            data = _http(url, data=body, headers={"Content-Type": "application/json"})
            return data.get("ids") or [], data.get("metadatas") or []
        batch_ids = data.get("ids") or []
        batch_metas = data.get("metadatas") or []
        if not batch_ids:
            break
        ids.extend(batch_ids)
        metas.extend(batch_metas)
        if len(batch_ids) < PAGE:
            break
        offset += len(batch_ids)
        if offset > 0 and batch_ids and ids[: len(batch_ids)] == batch_ids:
            break
    return ids, metas


def delete_ids(cid, to_delete):
    if not to_delete:
        return
    url = (
        f"{_chroma_url()}/api/v2/tenants/{CHROMA_TENANT}/databases/{CHROMA_DB}"
        f"/collections/{cid}/delete"
    )
    for i in range(0, len(to_delete), 100):
        chunk = to_delete[i : i + 100]
        _http(url, data={"ids": chunk}, headers={"Content-Type": "application/json"})
        print(f"  deleted chunk {i // 100 + 1}: {len(chunk)} ids")


def main():
    ap = argparse.ArgumentParser(description="Prune Gmail chroma index older than N days")
    ap.add_argument("--keep-days", type=int, default=KEEP_DAYS)
    ap.add_argument(
        "--dry-run",
        action="store_true",
        default=os.environ.get("GMAIL_PRUNE_DRY_RUN") == "1",
    )
    args = ap.parse_args()

    cutoff = datetime.now(timezone.utc) - timedelta(days=args.keep_days)
    print(
        f"=== gmail prune keep_days={args.keep_days} "
        f"cutoff_utc={cutoff.isoformat()} dry_run={args.dry_run} ==="
    )
    cid = collection_id()
    ids, metas = list_all(cid)
    print(f"  scanned {len(ids)} docs")

    stale, keep, unknown = [], 0, 0
    for doc_id, meta in zip(ids, metas):
        dt = parse_meta_date(meta)
        if dt is None:
            unknown += 1
            continue
        if dt < cutoff:
            stale.append(doc_id)
        else:
            keep += 1

    print(f"  keep={keep} prune={len(stale)} unknown_date_kept={unknown}")
    if not stale:
        print("  nothing to prune")
        return 0
    if args.dry_run:
        print("  dry-run: sample stale ids:", stale[:10])
        return 0
    delete_ids(cid, stale)
    print(f"=== prune complete: removed {len(stale)} ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
