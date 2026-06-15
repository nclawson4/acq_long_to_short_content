"""Vercel Python function — GET /api/status?job_id=...

Proxies through to the mac-mini baseline runner's /status/<job_id>.

Response shape the FE knows about:
    {status: "queued"|"running"|"done"|"failed", blob_url?, error?, ...}

The FE polls this until status is done|failed, then either shows the
download button (status=done) or surfaces the error in a speech bubble.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse


TUNNEL_URL = (os.environ.get("ACQ_TUNNEL_URL") or "").strip().rstrip("/")


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        qs = parse_qs(urlparse(self.path).query or "")
        job_id = (qs.get("job_id") or [""])[0].strip()
        if not job_id:
            return self._reply(400, {"error": "missing_job_id"})
        if not TUNNEL_URL:
            return self._reply(503, {"error": "tunnel_not_configured"})

        req = urllib.request.Request(
            f"{TUNNEL_URL}/status/{job_id}",
            headers={
                "user-agent": "acq-clipper-proxy/1.0",
                "bypass-tunnel-reminder": "1",
                "ngrok-skip-browser-warning": "1",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                upstream_body = r.read()
                upstream_status = r.status
        except urllib.error.HTTPError as e:
            try:
                upstream_body = e.read()
                upstream_status = e.code
            except Exception:
                upstream_body = b"{}"
                upstream_status = e.code
        except Exception as e:
            return self._reply(
                502,
                {"error": "tunnel_unreachable", "detail": f"{type(e).__name__}: {e}"},
            )

        try:
            obj = json.loads(upstream_body)
        except json.JSONDecodeError:
            obj = {"status": "unknown", "raw": upstream_body.decode("utf-8", "replace")}
        return self._reply(upstream_status, obj)

    def _reply(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.send_header("cache-control", "no-store")
        self.end_headers()
        self.wfile.write(body)
