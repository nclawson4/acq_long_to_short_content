"""Vercel Python function — GET /api/status?job_id=...

Returns the current JobState from Redis. Used by the frontend to poll
progress during a long run.
"""
from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

_repo_root = Path(__file__).resolve().parent.parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

from pipeline.state import get_store  # noqa: E402

import json as _json  # noqa: E402


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        qs = parse_qs(urlparse(self.path).query or "")
        job_id = (qs.get("job_id") or [""])[0].strip()
        if not job_id:
            return self._reply(400, {"error": "missing_job_id"})

        store = get_store()
        state = store.load_job(job_id)
        if state is None:
            return self._reply(404, {"error": "job_not_found", "job_id": job_id})

        # Include the spans and ledger entries — small (≤ a few KB) so this
        # is fine to ship on a poll. The frontend gets live cost + per-stage
        # status without a second round trip.
        trace_entries = store.read_trace(job_id)
        ledger_entries = store.read_ledger(job_id)
        # Decisions live in their own list so the speech-bubble UI can poll
        # them. Both store backends store with RPUSH semantics under the hood,
        # so lrange returns them in chronological order — no flip needed.
        try:
            raw_decisions = store._b.lrange(f"job:{job_id}:decisions")
            decisions = [_json.loads(s) for s in raw_decisions]
        except Exception:
            decisions = []

        payload = state.model_dump(mode="json")
        payload["trace"] = trace_entries
        payload["ledger"] = ledger_entries
        payload["decisions"] = decisions
        return self._reply(200, payload)

    def _reply(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.send_header("cache-control", "no-store")
        self.end_headers()
        self.wfile.write(body)
