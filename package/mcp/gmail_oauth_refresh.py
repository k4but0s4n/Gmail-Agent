#!/usr/bin/env python3
"""Refresh Gmail OAuth access_token; alert when refresh_token nears ~7d unverified expiry."""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import _config as cfg

CREDS = cfg.gmail_creds()
KEYS = cfg.gmail_keys()
STATE = cfg.openclaw_home() / "logs" / "gmail_oauth_state.json"
SECRETS = cfg.secrets_path()

# Unverified GCP OAuth clients typically expire refresh_tokens ~7 days after consent.
WARN_DAYS = float(os.environ.get("GMAIL_OAUTH_WARN_DAYS", "5"))
CRITICAL_DAYS = float(os.environ.get("GMAIL_OAUTH_CRITICAL_DAYS", "6.5"))


def _slack_channel() -> str:
    return cfg.alert_slack_channel() or cfg.slack_channel()


def _load_slack_token() -> str | None:
    if not SECRETS.exists():
        return None
    secrets = json.loads(SECRETS.read_text(encoding="utf-8"))
    providers = secrets.get("providers") or {}
    slack = providers.get("slack") or secrets.get("slack") or {}
    if isinstance(slack, dict):
        for key in ("botToken", "bot_token", "token"):
            if slack.get(key):
                return str(slack[key])
    for key in ("SLACK_BOT_TOKEN", "slack_bot_token"):
        if secrets.get(key):
            return str(secrets[key])
    return None


def _slack(text: str) -> None:
    channel = _slack_channel()
    if not channel:
        print("slack: GMAIL_ALERT_SLACK_CHANNEL / GMAIL_SLACK_CHANNEL not set", file=sys.stderr)
        return
    token = _load_slack_token()
    if not token:
        print("slack: no bot token found", file=sys.stderr)
        return
    payload = json.dumps({"channel": channel, "text": text}).encode()
    req = urllib.request.Request(
        "https://slack.com/api/chat.postMessage",
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = json.loads(resp.read().decode())
        if not body.get("ok"):
            print(f"slack api error: {body}", file=sys.stderr)
    except Exception as exc:
        print(f"slack notify failed: {exc}", file=sys.stderr)


def _file_birth_epoch(path: Path) -> int:
    import subprocess
    try:
        out = subprocess.check_output(["stat", "-c", "%W", str(path)], text=True).strip()
        birth = int(out)
        if birth > 0:
            return birth
    except Exception:
        pass
    try:
        out = subprocess.check_output(["stat", "-c", "%Y", str(path.parent)], text=True).strip()
        return int(out)
    except Exception:
        return int(path.stat().st_mtime)


def _consent_epoch(creds: dict, creds_path: Path) -> int:
    if creds.get("consent_epoch"):
        return int(creds["consent_epoch"])
    epoch = _file_birth_epoch(creds_path)
    creds["consent_epoch"] = epoch
    return epoch


def main() -> int:
    if not CREDS.exists() or not KEYS.exists():
        print("missing credentials or keys", file=sys.stderr)
        return 1
    creds = json.loads(CREDS.read_text(encoding="utf-8"))
    keys = json.loads(KEYS.read_text(encoding="utf-8"))
    installed = keys.get("installed") or keys.get("web") or keys
    client_id = installed["client_id"]
    client_secret = installed["client_secret"]
    refresh_token = creds.get("refresh_token")
    if not refresh_token:
        print("no refresh_token", file=sys.stderr)
        return 1

    # Correct mistaken consent_epoch if it was stamped at a later refresh.
    birth = _file_birth_epoch(CREDS)
    if not creds.get("consent_epoch") or int(creds["consent_epoch"]) > birth + 60:
        creds["consent_epoch"] = birth
    consent_epoch = int(creds["consent_epoch"])
    age_days = (time.time() - consent_epoch) / 86400.0

    prev_state: dict = {}
    if STATE.exists():
        try:
            prev_state = json.loads(STATE.read_text(encoding="utf-8"))
        except Exception:
            prev_state = {}

    body = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }
    ).encode()
    req = urllib.request.Request("https://oauth2.googleapis.com/token", data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            tok = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        err = exc.read().decode(errors="replace")
        print(f"token refresh HTTP {exc.code}: {err[:400]}", file=sys.stderr)
        if exc.code in (400, 401):
            _slack(
                f":warning: Gmail OAuth refresh FAILED (HTTP {exc.code}). "
                f"Re-consent required for unverified GCP client. "
                f"consent_age_days={age_days:.1f}. Re-run Gmail MCP auth on the OpenClaw host."
            )
            return 2
        return 1

    access = tok.get("access_token")
    expires_in = int(tok.get("expires_in", 3600))
    if not access:
        print("no access_token in response", file=sys.stderr)
        return 1

    creds["access_token"] = access
    creds["expiry_date"] = int(time.time() * 1000) + expires_in * 1000
    if tok.get("refresh_token"):
        creds["refresh_token"] = tok["refresh_token"]
        # New refresh_token issued → treat as fresh consent
        creds["consent_epoch"] = int(time.time())
        age_days = 0.0
    cfg.atomic_write_json(CREDS, creds)

    STATE.parent.mkdir(parents=True, exist_ok=True)
    warn_key = time.strftime("%Y-%m-%d")
    state = {
        "last_refresh_unix": int(time.time()),
        "expires_in": expires_in,
        "consent_epoch": int(creds["consent_epoch"]),
        "consent_age_days": round(age_days, 2),
        "last_warn_day": prev_state.get("last_warn_day"),
    }

    print(
        f"access_token refreshed; expires_in={expires_in}s; "
        f"consent_age_days={age_days:.2f}"
    )

    if age_days >= CRITICAL_DAYS and state.get("last_warn_day") != warn_key:
        msg = (
            f":rotating_light: Gmail OAuth refresh_token is {age_days:.1f}d old "
            f"(critical ≥{CRITICAL_DAYS}d). Unverified GCP clients expire ~7d — re-consent today."
        )
        print(msg, file=sys.stderr)
        _slack(msg)
        state["last_warn_day"] = warn_key
    elif age_days >= WARN_DAYS and state.get("last_warn_day") != warn_key:
        msg = (
            f":warning: Gmail OAuth refresh_token is {age_days:.1f}d old "
            f"(warn ≥{WARN_DAYS}d). Plan re-consent before the ~7d unverified expiry."
        )
        print(msg, file=sys.stderr)
        _slack(msg)
        state["last_warn_day"] = warn_key

    cfg.atomic_write_json(STATE, state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
