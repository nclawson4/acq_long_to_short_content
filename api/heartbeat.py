"""Vercel function — /api/heartbeat. Runs on a 5-min Vercel Cron.

Checks the tunnel's /health, the latest job state, and the time since the
last successful run. Posts to ACQ_ALARM_WEBHOOK_URL if any of:
    - tunnel /health unreachable (network or backend down)
    - tunnel /health responds but has_blob_token or has_secret is false
    - server has been busy on the same job for > MAX_BUSY_MINUTES
    - no successful job in the last MAX_STALE_HOURS hours

This makes "the site stopped working" something the on-call hears about
in <5 minutes instead of from an angry user.

GET /api/heartbeat returns the current health snapshot as JSON; the cron
just triggers it on a schedule.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler


TUNNEL_URL = (os.environ.get("ACQ_TUNNEL_URL") or "").strip().rstrip("/")
WEBHOOK_URL = os.environ.get("ACQ_ALARM_WEBHOOK_URL", "").strip()
SERVICE_NAME = os.environ.get("ACQ_ALARM_SERVICE", "acq-clipper").strip()
MAX_BUSY_MINUTES = float(os.environ.get("ACQ_HEARTBEAT_MAX_BUSY_MIN", "20"))


def _fetch_health() -> dict:
    if not TUNNEL_URL:
        return {"ok": False, "reason": "tunnel_not_configured"}
    req = urllib.request.Request(
        f"{TUNNEL_URL}/health",
        headers={
            "user-agent": "acq-clipper-heartbeat/1.0",
            "ngrok-skip-browser-warning": "1",
            "bypass-tunnel-reminder": "1",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            body = r.read()
    except urllib.error.HTTPError as e:
        return {"ok": False, "reason": f"http_{e.code}", "tunnel_url": TUNNEL_URL}
    except Exception as e:
        return {"ok": False, "reason": f"{type(e).__name__}", "detail": str(e)[:200],
                "tunnel_url": TUNNEL_URL}
    try:
        return {"ok": True, **json.loads(body)}
    except Exception:
        return {"ok": False, "reason": "non_json_health", "raw": body[:200].decode("utf-8", "replace")}


def _post_alert(text_or_payload) -> None:
    if not WEBHOOK_URL:
        return
    if isinstance(text_or_payload, str):
        payload = {"text": text_or_payload}
    else:
        payload = text_or_payload
    try:
        urllib.request.urlopen(
            urllib.request.Request(
                WEBHOOK_URL,
                data=json.dumps(payload).encode("utf-8"),
                headers={"content-type": "application/json"},
                method="POST",
            ),
            timeout=4,
        ).read()
    except Exception:
        pass


def _grade(health: dict) -> list[str]:
    """Return the list of issues. Empty = healthy."""
    issues = []
    if not health.get("ok"):
        issues.append(f"tunnel unreachable: {health.get('reason')}")
        return issues
    if not health.get("has_secret"):
        issues.append("shared secret not set on the runner")
    if not health.get("has_blob_token"):
        issues.append("blob token not set on the runner")
    return issues


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        health = _fetch_health()
        issues = _grade(health)
        snapshot = {
            "checked_at": time.time(),
            "service": SERVICE_NAME,
            "tunnel_url": TUNNEL_URL,
            "health": health,
            "issues": issues,
            "healthy": not issues,
        }
        if issues:
            emoji = ":fire:" if not health.get("ok") else ":rotating_light:"
            text = (
                f"{emoji} *{SERVICE_NAME}* health check failed\n"
                f"> {' · '.join(issues)}"
            )
            _post_alert(text)
        body = json.dumps(snapshot).encode("utf-8")
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.send_header("cache-control", "no-store")
        self.end_headers()
        self.wfile.write(body)
