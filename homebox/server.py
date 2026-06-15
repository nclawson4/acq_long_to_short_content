"""HTTP server in front of run_one.py.

Async model so tunnels with short request timeouts (localtunnel = 120s)
don't kill long pipeline runs.

    POST /run     {url, job_id?}       -> 202 {job_id, status: "queued"}
    GET  /status/<job_id>              -> 200 {status, blob_url?, error?}
    GET  /health                       -> 200

The subprocess writes its final result JSON to jobs/<job_id>.json when it
finishes. /status reads that, or reports "running" if the marker file isn't
written yet.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parent
RUN_ONE = REPO / "run_one.py"
# venv layout differs between Windows (`Scripts\python.exe`) and POSIX
# (`bin/python`). Picking by platform here avoids needing two copies of
# this file.
if sys.platform == "win32":
    PYTHON = REPO / ".venv" / "Scripts" / "python.exe"
else:
    PYTHON = REPO / ".venv" / "bin" / "python"
JOBS_DIR = REPO / "jobs"
JOBS_DIR.mkdir(parents=True, exist_ok=True)

SHARED_SECRET = os.environ.get("ACQ_INGEST_SECRET", "")
HOST = os.environ.get("ACQ_HOST", "0.0.0.0")
PORT = int(os.environ.get("ACQ_PORT", "8787"))

# Lock so we only run one pipeline at a time — Apple Silicon mini handles
# one at a time fine, two concurrent would oversubscribe ffmpeg encoding.
_lock = threading.Lock()


def _reply(handler: BaseHTTPRequestHandler, status: int, payload: dict) -> None:
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("content-type", "application/json")
    handler.send_header("content-length", str(len(body)))
    handler.send_header("cache-control", "no-store")
    handler.send_header("access-control-allow-origin", "*")
    handler.send_header("access-control-allow-headers", "content-type, x-acq-secret")
    handler.send_header("access-control-allow-methods", "GET, POST, OPTIONS")
    handler.end_headers()
    handler.wfile.write(body)


def _write_status(job_id: str, payload: dict) -> None:
    (JOBS_DIR / f"{job_id}.json").write_text(json.dumps(payload), encoding="utf-8")


def _read_status(job_id: str) -> dict | None:
    p = JOBS_DIR / f"{job_id}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _run_job(url: str, job_id: str) -> None:
    """Background worker — owns _lock for the whole pipeline duration."""
    with _lock:
        _write_status(job_id, {"status": "running", "started_at": time.time()})
        try:
            proc = subprocess.run(
                [str(PYTHON), str(RUN_ONE), url, job_id],
                cwd=str(REPO),
                capture_output=True,
                text=True,
                timeout=1500,
            )
        except subprocess.TimeoutExpired:
            _write_status(job_id, {"status": "failed", "error": "timeout"})
            return

        stdout = proc.stdout or ""
        stderr_tail = (proc.stderr or "")[-1500:]
        result_json = None
        marker = "---RESULT---"
        if marker in stdout:
            tail = stdout.split(marker, 1)[1].strip()
            try:
                result_json = json.loads(tail)
            except json.JSONDecodeError:
                pass

        if proc.returncode != 0 or not result_json or not result_json.get("clips"):
            _write_status(job_id, {
                "status": "failed",
                "returncode": proc.returncode,
                "error": "run_failed",
                "stderr_tail": stderr_tail,
                "partial_result": result_json,
            })
            return

        first = result_json["clips"][0]
        _write_status(job_id, {
            "status": "done",
            "job_id": result_json["job_id"],
            "video_id": result_json["video_id"],
            "blob_url": first.get("blob_url"),
            "duration_s": first.get("duration_s"),
            "total_cost_usd": result_json.get("total_cost_usd"),
            "duration_ms": result_json.get("duration_ms"),
        })


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write(f"[{time.strftime('%H:%M:%S')}] {fmt % args}\n")

    def do_OPTIONS(self) -> None:
        return _reply(self, 204, {})

    def do_GET(self) -> None:
        if self.path == "/health":
            return _reply(self, 200, {
                "ok": True,
                "service": "acq-baseline-runner",
                "has_secret": bool(SHARED_SECRET),
                "busy": _lock.locked(),
            })
        if self.path.startswith("/status/"):
            job_id = self.path[len("/status/"):]
            st = _read_status(job_id)
            if st is None:
                return _reply(self, 404, {"status": "unknown", "job_id": job_id})
            return _reply(self, 200, st)
        return _reply(self, 404, {"error": "not_found"})

    def do_POST(self) -> None:
        if self.path != "/run":
            return _reply(self, 404, {"error": "not_found"})
        if SHARED_SECRET and self.headers.get("x-acq-secret") != SHARED_SECRET:
            return _reply(self, 401, {"error": "unauthorized"})

        length = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return _reply(self, 400, {"error": "invalid_json"})

        url = (body.get("url") or "").strip()
        if not url:
            return _reply(self, 400, {"error": "missing_url"})
        job_id = body.get("job_id") or f"web_{uuid.uuid4().hex[:10]}"

        if _lock.locked():
            return _reply(self, 409, {"error": "busy"})

        # Pre-write a "queued" status so the FE's first poll succeeds.
        _write_status(job_id, {"status": "queued", "queued_at": time.time()})
        threading.Thread(target=_run_job, args=(url, job_id), daemon=True).start()
        return _reply(self, 202, {"status": "queued", "job_id": job_id})


def main() -> None:
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"acq-baseline-runner listening on http://{HOST}:{PORT}  secret={'set' if SHARED_SECRET else 'MISSING'}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    httpd.server_close()


if __name__ == "__main__":
    main()
