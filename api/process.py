"""Vercel Python function — POST /api/process.

Thin proxy in front of the home-box baseline runner. Forwards `{url}` to
ACQ_TUNNEL_URL/run, returns the {job_id, status} the runner echoes back.

The runner is async: this returns immediately with a job id and the FE
polls /api/status?job_id=... until the runner reports status=done with a
blob_url.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler


TUNNEL_URL = (os.environ.get("ACQ_TUNNEL_URL") or "").strip().rstrip("/")
SHARED_SECRET = (os.environ.get("ACQ_INGEST_SECRET") or "").strip()


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return self._reply(400, {"error": "invalid_json"})

        url = (body.get("url") or "").strip()
        if not url:
            return self._reply(400, {"error": "missing_url"})

        if not TUNNEL_URL:
            return self._reply(503, {"error": "tunnel_not_configured"})

        job_id = body.get("job_id") or f"web_{uuid.uuid4().hex[:10]}"
        payload = json.dumps({"url": url, "job_id": job_id}).encode("utf-8")

        req = urllib.request.Request(
            f"{TUNNEL_URL}/run",
            data=payload,
            headers={
                "content-type": "application/json",
                "x-acq-secret": SHARED_SECRET,
                # Skips the localtunnel interstitial AND ngrok's free-tier
                # browser warning page — without these headers either tunnel
                # serves HTML instead of forwarding the request.
                "user-agent": "acq-clipper-proxy/1.0",
                "bypass-tunnel-reminder": "1",
                "ngrok-skip-browser-warning": "1",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                upstream_body = r.read()
                upstream_status = r.status
        except urllib.error.HTTPError as e:
            try:
                detail = json.loads(e.read())
            except Exception:
                detail = {"raw": str(e)}
            return self._reply(e.code, {"error": "upstream_error", "detail": detail, "job_id": job_id})
        except Exception as e:
            return self._reply(
                502,
                {"error": "tunnel_unreachable", "detail": f"{type(e).__name__}: {e}", "job_id": job_id},
            )

        # Pass through the upstream payload but ensure the FE always sees
        # the job_id it can poll on.
        try:
            obj = json.loads(upstream_body)
        except json.JSONDecodeError:
            obj = {"raw": upstream_body.decode("utf-8", "replace")}
        obj.setdefault("job_id", job_id)
        return self._reply(upstream_status, obj)

    def do_GET(self):
        return self._reply(200, {
            "ok": True,
            "endpoint": "process",
            "tunnel": bool(TUNNEL_URL),
            "secret": bool(SHARED_SECRET),
        })

    def _reply(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.send_header("cache-control", "no-store")
        self.end_headers()
        self.wfile.write(body)
