#!/usr/bin/env python3
"""HTTP endpoint for digest Approve-unsub buttons.

Two modes:
  1) Signed link (preferred when PUBLIC_BASE + LINK_SECRET set):
       GET/POST /slack/approve?b=&e=&s=  — confirm page then approve
  2) Slack Interactivity (optional):
       POST /slack/interact — verify X-Slack-Signature + allowlist + channel

Does NOT enable approve on the triage agent. Does NOT open a second Socket Mode client.

Bind: GMAIL_SLACK_INTERACT_HOST / GMAIL_SLACK_INTERACT_PORT (default 127.0.0.1:8787)
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

import _config as cfg

ACTION_ID = "gmail_unsub_approve"
MAX_SKEW_SECONDS = 60 * 5


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def load_signing_secret() -> str:
    env = _env("GMAIL_SLACK_SIGNING_SECRET")
    if env:
        return env
    path = cfg.secrets_path()
    if not path.exists():
        return ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return ""
    providers = data.get("providers") or {}
    slack = providers.get("slack") or data.get("slack") or {}
    if isinstance(slack, dict):
        for key in ("signingSecret", "signing_secret", "SigningSecret"):
            if slack.get(key):
                return str(slack[key]).strip()
    for key in ("GMAIL_SLACK_SIGNING_SECRET", "SLACK_SIGNING_SECRET", "signingSecret"):
        if data.get(key):
            return str(data[key]).strip()
    return ""


def approve_user_allowlist() -> set[str]:
    raw = _env("GMAIL_SLACK_APPROVE_USERS")
    if not raw:
        return set()
    return {p.strip() for p in raw.replace("\n", ",").split(",") if p.strip()}


def expected_channel() -> str:
    return cfg.slack_channel()


def verify_slack_signature(
    signing_secret: str,
    timestamp: str,
    body: bytes,
    signature: str,
    *,
    now: float | None = None,
) -> bool:
    """HMAC-SHA256 Slack request verification (v0)."""
    if not signing_secret or not timestamp or not signature:
        return False
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return False
    now = time.time() if now is None else now
    if abs(now - ts) > MAX_SKEW_SECONDS:
        return False
    basestring = b"v0:" + str(ts).encode() + b":" + body
    digest = hmac.new(signing_secret.encode(), basestring, hashlib.sha256).hexdigest()
    expected = "v0=" + digest
    return hmac.compare_digest(expected, signature.strip())


def post_response_url(response_url: str, payload: dict, timeout: float = 20.0) -> dict:
    if not response_url:
        return {"ok": False, "error": "response_url_missing"}
    req = urllib.request.Request(
        response_url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
            if not raw:
                return {"ok": True}
            try:
                return json.loads(raw)
            except Exception:
                return {"ok": True, "raw": raw[:200]}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace") if exc.fp else ""
        return {"ok": False, "error": f"http_{exc.code}", "detail": body[:300]}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def summarize_approve(result: dict) -> str:
    """Human reply without secrets/targets."""
    if not result:
        return "Approve failed (empty result)."
    if result.get("ok") is False and result.get("error") and not result.get("results"):
        return f"Approve failed: {result.get('error')}"
    rows = result.get("results") or []
    ok_n = sum(1 for r in rows if r.get("ok"))
    fail_n = len(rows) - ok_n
    parts = [f"Unsub approve: {ok_n} ok, {fail_n} failed (of {len(rows)})."]
    # Short per-id status only — never include target URLs.
    for r in rows[:20]:
        pid = r.get("pending_id") or r.get("id") or "?"
        if r.get("ok"):
            parts.append(f"• `{pid}` ok")
        else:
            err = str(r.get("error") or "failed")[:80]
            parts.append(f"• `{pid}` fail — {err}")
    if len(rows) > 20:
        parts.append(f"_…{len(rows) - 20} more_")
    return "\n".join(parts)


def _html_page(title: str, body_html: str, *, status: int = 200) -> tuple[int, bytes, str]:
    page = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{title}</title>"
        "<style>body{font-family:system-ui,sans-serif;max-width:40rem;margin:2rem auto;padding:0 1rem;line-height:1.45}"
        "button{font-size:1.05rem;padding:.6rem 1rem;cursor:pointer}"
        "code{background:#f2f2f2;padding:.1rem .3rem;border-radius:4px}</style>"
        f"</head><body><h1>{title}</h1>{body_html}</body></html>"
    )
    return status, page.encode(), "text/html; charset=utf-8"


def run_link_approve(
    batch_id: str,
    *,
    approve_fn: Callable[[list[str]], dict] | None = None,
) -> tuple[str, dict]:
    """Execute approve for a signed-link batch. Returns (summary_text, result)."""
    import list_unsubscribe_mcp as unsub  # noqa: WPS433

    batch = unsub.load_unsub_draft_batch(batch_id)
    if not batch:
        return "Approve failed: batch missing or expired.", {"ok": False, "error": "batch_missing"}
    ids = list(batch.get("ids") or [])
    if not ids:
        return "Approve failed: batch has no pending ids.", {"ok": False, "error": "empty_batch"}
    do_approve = approve_fn or unsub.approve_unsubscribe
    try:
        result = do_approve(ids)
    except Exception as exc:
        result = {"ok": False, "error": str(exc), "results": []}
    text = summarize_approve(result if isinstance(result, dict) else {"ok": False, "error": "bad result"})
    text = f"*Unsub confirmation* (approved via Slack button)\n{text}"
    channel = str(batch.get("channel") or expected_channel() or "").strip()
    if channel:
        try:
            import gmail_slack_post as sp  # noqa: WPS433

            sp.post_message(channel, text)
        except Exception:
            pass
    return text, result if isinstance(result, dict) else {"ok": False}


def handle_block_actions(
    payload: dict,
    *,
    approve_fn: Callable[[list[str]], dict] | None = None,
) -> tuple[int, dict | None, Callable[[], None] | None]:
    """Return (status, immediate_json_or_None, background_fn_or_None).

    immediate_json is returned in the HTTP body (ephemeral deny).
    background_fn runs after a fast 200 ack (approve + response_url).
    """
    user = (payload.get("user") or {}) if isinstance(payload.get("user"), dict) else {}
    user_id = str(user.get("id") or "").strip()
    channel_obj = payload.get("channel") or {}
    if isinstance(channel_obj, dict):
        channel_id = str(channel_obj.get("id") or "").strip()
    else:
        channel_id = str(channel_obj or "").strip()
    # Some payloads nest channel under container / message
    if not channel_id:
        container = payload.get("container") or {}
        if isinstance(container, dict):
            channel_id = str(container.get("channel_id") or "").strip()

    allow = approve_user_allowlist()
    want_channel = expected_channel()
    response_url = str(payload.get("response_url") or "").strip()
    message = payload.get("message") if isinstance(payload.get("message"), dict) else {}
    thread_ts = str(message.get("ts") or "").strip()
    container = payload.get("container") if isinstance(payload.get("container"), dict) else {}
    if not thread_ts:
        thread_ts = str(container.get("message_ts") or "").strip()

    def deny(msg: str) -> tuple[int, dict, None]:
        body = {"response_type": "ephemeral", "text": msg}
        return 200, body, None

    if not allow:
        return deny("Approve denied: GMAIL_SLACK_APPROVE_USERS is empty (fail-closed).")
    if user_id not in allow:
        return deny("Approve denied: your Slack user is not allowlisted.")
    if not want_channel:
        return deny("Approve denied: GMAIL_SLACK_CHANNEL is not configured.")
    if channel_id != want_channel:
        return deny("Approve denied: wrong channel.")

    actions = payload.get("actions") or []
    action = None
    for a in actions:
        if isinstance(a, dict) and a.get("action_id") == ACTION_ID:
            action = a
            break
    if not action:
        return deny("Approve denied: unknown action.")

    batch_id = str(action.get("value") or "").strip()
    if not batch_id:
        return deny("Approve denied: missing batch id.")

    import list_unsubscribe_mcp as unsub  # noqa: WPS433

    batch = unsub.load_unsub_draft_batch(batch_id)
    if not batch:
        return deny("Approve denied: batch missing or expired (re-run triage for a fresh button).")

    # Optional: batch recorded channel must match when present
    batch_ch = str(batch.get("channel") or "").strip()
    if batch_ch and batch_ch != channel_id:
        return deny("Approve denied: batch channel mismatch.")

    ids = list(batch.get("ids") or [])
    if not ids:
        return deny("Approve denied: batch has no pending ids.")

    do_approve = approve_fn or unsub.approve_unsubscribe

    def background() -> None:
        try:
            result = do_approve(ids)
        except Exception as exc:
            result = {"ok": False, "error": str(exc), "results": []}
        text = summarize_approve(result if isinstance(result, dict) else {"ok": False, "error": "bad result"})
        who = f"<@{user_id}>" if user_id else "operator"
        text = f"*Unsub confirmation* (approved by {who})\n{text}"
        # Visible channel reply (thread under the digest) via bot token — primary.
        try:
            import gmail_slack_post as sp  # noqa: WPS433

            sp.post_message(channel_id or want_channel, text, thread_ts=thread_ts)
        except Exception:
            pass
        # Also ack via response_url when Slack provided one.
        post_response_url(
            response_url,
            {
                "response_type": "in_channel",
                "replace_original": False,
                "text": text,
            },
        )

    # Ack with empty body; Slack allows up to 3s — we finish immediately.
    return 200, None, background


def process_payload(
    payload: dict,
    *,
    approve_fn: Callable[[list[str]], dict] | None = None,
) -> tuple[int, dict | None, Callable[[], None] | None]:
    ptype = str(payload.get("type") or "")
    if ptype == "url_verification":
        # Not required for Interactivity Request URL, but harmless if Slack probes.
        return 200, {"challenge": payload.get("challenge")}, None
    if ptype == "block_actions":
        return handle_block_actions(payload, approve_fn=approve_fn)
    return 200, {"text": "ignored"}, None


class InteractHandler(BaseHTTPRequestHandler):
    server_version = "gmail-slack-interact/1.0"

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _read_body(self) -> bytes:
        try:
            n = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            n = 0
        if n <= 0:
            return b""
        # Cap body size (Slack payloads are small)
        n = min(n, 256 * 1024)
        return self.rfile.read(n)

    def _write_json(self, status: int, obj: dict | None) -> None:
        raw = b"" if obj is None else json.dumps(obj).encode()
        self.send_response(status)
        if obj is not None:
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
        else:
            self.send_header("Content-Length", "0")
        self.end_headers()
        if raw:
            self.wfile.write(raw)

    def _write_bytes(self, status: int, raw: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _handle_approve_get(self, qs: dict[str, list[str]]) -> None:
        import gmail_slack_post as sp  # noqa: WPS433

        batch_id = (qs.get("b") or [""])[0].strip()
        exp = (qs.get("e") or [""])[0].strip()
        sig = (qs.get("s") or [""])[0].strip()
        if not sp.verify_approve_link(batch_id, exp, sig):
            status, raw, ctype = _html_page(
                "Approve link invalid",
                "<p>This approve link is invalid or expired. Re-run triage for a fresh button.</p>",
                status=400,
            )
            self._write_bytes(status, raw, ctype)
            return
        import list_unsubscribe_mcp as unsub  # noqa: WPS433

        batch = unsub.load_unsub_draft_batch(batch_id)
        if not batch:
            status, raw, ctype = _html_page(
                "Batch expired",
                "<p>This approve batch is missing or expired. Re-run triage for a fresh button.</p>",
                status=410,
            )
            self._write_bytes(status, raw, ctype)
            return
        ids = list(batch.get("ids") or [])
        items = "".join(f"<li><code>{html_escape(str(i))}</code></li>" for i in ids[:30])
        form = (
            f"<p>Confirm unsubscribe for <strong>{len(ids)}</strong> pending id(s):</p>"
            f"<ul>{items}</ul>"
            "<form method='POST'>"
            f"<input type='hidden' name='b' value='{html_escape(batch_id)}'/>"
            f"<input type='hidden' name='e' value='{html_escape(exp)}'/>"
            f"<input type='hidden' name='s' value='{html_escape(sig)}'/>"
            "<button type='submit'>Confirm unsubscribe</button>"
            "</form>"
            "<p><small>A confirmation will be posted in Slack after this.</small></p>"
        )
        status, raw, ctype = _html_page("Confirm unsub approve", form)
        self._write_bytes(status, raw, ctype)

    def _handle_approve_post(self) -> None:
        import gmail_slack_post as sp  # noqa: WPS433

        body = self._read_body()
        form = urllib.parse.parse_qs(body.decode(errors="replace"), keep_blank_values=True)
        batch_id = (form.get("b") or [""])[0].strip()
        exp = (form.get("e") or [""])[0].strip()
        sig = (form.get("s") or [""])[0].strip()
        if not sp.verify_approve_link(batch_id, exp, sig):
            status, raw, ctype = _html_page(
                "Approve link invalid",
                "<p>This approve link is invalid or expired.</p>",
                status=400,
            )
            self._write_bytes(status, raw, ctype)
            return
        text, result = run_link_approve(batch_id)
        rows = (result or {}).get("results") or []
        ok = bool((result or {}).get("ok")) if "ok" in (result or {}) else bool(rows) and all(
            r.get("ok") for r in rows
        )
        if rows:
            ok = all(r.get("ok") for r in rows)
        title = "Unsub approved" if ok else "Unsub approve finished with errors"
        status, raw, ctype = _html_page(
            title,
            f"<p>{html_escape(text).replace(chr(10), '<br>')}</p>"
            "<p>You can close this tab and check Slack for the confirmation.</p>",
            status=200 if ok else 502,
        )
        self._write_bytes(status, raw, ctype)

    def do_GET(self) -> None:  # noqa: N802
        path, _, query = self.path.partition("?")
        if path in {"/", "/healthz", "/slack/interact"}:
            self._write_json(200, {"ok": True, "service": "gmail-slack-interact"})
            return
        if path == "/slack/approve":
            qs = urllib.parse.parse_qs(query, keep_blank_values=True)
            self._handle_approve_get(qs)
            return
        self._write_json(404, {"ok": False, "error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/slack/approve":
            self._handle_approve_post()
            return
        if path != "/slack/interact":
            self._write_json(404, {"ok": False, "error": "not_found"})
            return

        body = self._read_body()
        secret = load_signing_secret()
        ts = self.headers.get("X-Slack-Request-Timestamp") or ""
        sig = self.headers.get("X-Slack-Signature") or ""
        if not verify_slack_signature(secret, ts, body, sig):
            self._write_json(401, {"ok": False, "error": "invalid_signature"})
            return

        # application/x-www-form-urlencoded: payload=<json>
        ctype = (self.headers.get("Content-Type") or "").lower()
        payload: dict
        try:
            if "application/json" in ctype:
                payload = json.loads(body.decode() or "{}")
            else:
                form = urllib.parse.parse_qs(body.decode(), keep_blank_values=True)
                raw = (form.get("payload") or [""])[0]
                payload = json.loads(raw or "{}")
        except Exception:
            self._write_json(400, {"ok": False, "error": "bad_payload"})
            return

        status, immediate, background = process_payload(payload)
        self._write_json(status, immediate)
        if background is not None:
            threading.Thread(target=background, name="slack-approve", daemon=True).start()


def html_escape(s: str) -> str:
    return (
        (s or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def serve(host: str | None = None, port: int | None = None) -> None:
    host = host or _env("GMAIL_SLACK_INTERACT_HOST", "127.0.0.1") or "127.0.0.1"
    try:
        port = int(port if port is not None else (_env("GMAIL_SLACK_INTERACT_PORT", "8787") or "8787"))
    except ValueError:
        port = 8787
    import gmail_slack_post as sp  # noqa: WPS433

    if sp.link_mode_enabled():
        print(
            f"link approve enabled base={sp.approve_public_base()}/slack/approve",
            flush=True,
        )
    elif not load_signing_secret():
        print(
            "WARN: neither link approve (PUBLIC_BASE+LINK_SECRET) nor Slack signing secret configured",
            file=sys.stderr,
        )
    if not approve_user_allowlist():
        print(
            "WARN: GMAIL_SLACK_APPROVE_USERS empty — Slack Interactivity approve denied for all users",
            file=sys.stderr,
        )
    httpd = ThreadingHTTPServer((host, port), InteractHandler)
    print(
        f"gmail_slack_interact listening on http://{host}:{port} "
        "(/slack/approve, /slack/interact)",
        flush=True,
    )
    httpd.serve_forever()


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # Optional: source-friendly — runners may set OPENCLAW_HOME before exec.
    if "--help" in argv or "-h" in argv:
        print(
            "Usage: gmail_slack_interact.py\n"
            "  GMAIL_SLACK_INTERACT_HOST (default 127.0.0.1)\n"
            "  GMAIL_SLACK_INTERACT_PORT (default 8787)\n"
            "  Link mode: GMAIL_SLACK_APPROVE_PUBLIC_BASE + GMAIL_SLACK_APPROVE_LINK_SECRET\n"
            "  Slack mode: GMAIL_SLACK_SIGNING_SECRET + GMAIL_SLACK_APPROVE_USERS\n"
            "  GMAIL_SLACK_CHANNEL\n"
        )
        return 0
    # Load gmail.env if present (same convention as shell runners).
    env_file = Path(
        os.environ.get("GMAIL_ENV_FILE")
        or (cfg.openclaw_home() / "gmail.env")
    )
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k = k.strip()
            if not k or k in os.environ:
                continue
            os.environ[k] = v.strip().strip("'").strip('"')
    serve()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
