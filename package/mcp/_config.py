"""Shared env/path helpers for openclaw-gmail-triage (stdlib only).

No LAN IPs or absolute home paths in source. Set URLs via env (see .env.example).
Paths default to $OPENCLAW_HOME and $GMAIL_CREDS_DIR under the current user's home.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


def openclaw_home() -> Path:
    raw = os.environ.get("OPENCLAW_HOME", "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".openclaw"


def gmail_creds_dir() -> Path:
    raw = os.environ.get("GMAIL_CREDS_DIR", "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".gmail-mcp"


def gmail_creds() -> Path:
    raw = os.environ.get("GMAIL_CREDS", "").strip()
    if raw:
        return Path(raw).expanduser()
    return gmail_creds_dir() / "credentials.json"


def gmail_keys() -> Path:
    raw = os.environ.get("GMAIL_KEYS", "").strip()
    if raw:
        return Path(raw).expanduser()
    return gmail_creds_dir() / "gcp-oauth.keys.json"


def state_dir() -> Path:
    raw = os.environ.get("GMAIL_UNSUB_STATE", "").strip()
    if raw:
        return Path(raw).expanduser()
    return openclaw_home() / "gmail"


def secrets_path() -> Path:
    raw = os.environ.get("OPENCLAW_SECRETS", "").strip()
    if raw:
        return Path(raw).expanduser()
    return openclaw_home() / "secrets.json"


def bin_dir() -> Path:
    raw = os.environ.get("OPENCLAW_BIN", "").strip()
    if raw:
        return Path(raw).expanduser()
    return openclaw_home() / "bin"


def label_prefix() -> str:
    return os.environ.get("GMAIL_LABEL_PREFIX", "OC").strip().rstrip("/") or "OC"


def label_name(category: str) -> str:
    return f"{label_prefix()}/{category}"


def chroma_collection() -> str:
    return os.environ.get("GMAIL_CHROMA_COLLECTION", "gmail_inbox").strip() or "gmail_inbox"


def agent_id() -> str:
    return os.environ.get("GMAIL_AGENT_ID", "gmail-triage").strip() or "gmail-triage"


def embed_model() -> str:
    return os.environ.get("GMAIL_EMBED_MODEL", "nomic-embed-text-v1.5").strip()


def require_env(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        raise RuntimeError(
            f"{name} is required. Copy package/.env.example and export it "
            f"(or source an env file) before running."
        )
    return val


def chroma_url() -> str:
    return require_env("CHROMA_URL").rstrip("/")


def chroma_api_base() -> str:
    tenant = os.environ.get("CHROMA_TENANT", "default_tenant").strip() or "default_tenant"
    db = os.environ.get("CHROMA_DATABASE", "default_database").strip() or "default_database"
    return f"{chroma_url()}/api/v2/tenants/{tenant}/databases/{db}"


def embed_url() -> str:
    return require_env("GMAIL_EMBED_URL").rstrip("/")


def retrieve_url() -> str:
    return require_env("GMAIL_RETRIEVE_URL").rstrip("/")


def slack_channel() -> str:
    return os.environ.get("GMAIL_SLACK_CHANNEL", "").strip()


def alert_slack_channel() -> str:
    return os.environ.get("GMAIL_ALERT_SLACK_CHANNEL", "").strip()


def atomic_write_text(path: Path, text: str, mode: int = 0o600) -> None:
    """Write text atomically (temp + replace) and chmod."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def atomic_write_json(path: Path, data, mode: int = 0o600) -> None:
    atomic_write_text(path, json.dumps(data, indent=2, sort_keys=True) + "\n", mode=mode)
