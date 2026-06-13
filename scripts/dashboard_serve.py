"""Local review dashboard for pipeline runs.

Serves on http://localhost:3002/eval. Reads pipeline_runs/manifest.json and
exposes per-job artifacts (result.json, trace.json, ledger.json, clips/*.mp4)
so the operator can:

    - see every clip side-by-side with its source URL
    - play the burned MP4 in-page
    - inspect QC failures + per-stage cost + per-stage timing
    - drill into the OTel span trace for any run

Routing:
    GET  /                        302 -> /eval
    GET  /eval                    dashboard HTML
    GET  /eval/manifest.json      pipeline_runs/manifest.json
    GET  /eval/runs/<job_id>/...  pipeline_runs/<job_id>/...   (any file)

This server is read-only and bound to 127.0.0.1 by default.
"""
from __future__ import annotations

import argparse
import mimetypes
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

_REPO_ROOT = Path(__file__).resolve().parent.parent
RUNS_DIR = _REPO_ROOT / "pipeline_runs"
# Fall-through root for everything that isn't an /eval route — preserves the
# old `python -m http.server 3002 --directory processing` behavior the other
# agents' dashboards rely on.
FALLTHROUGH_DIR = _REPO_ROOT / "processing"


DASHBOARD_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>ACQ Clipper · Eval</title>
<style>
:root {
  color-scheme: dark;
  --bg: #0a0a0a; --fg: #fafafa; --muted: #8a8a8a;
  --accent: #ff6a3d; --ok: #4ade80; --err: #f87171; --warn: #fbbf24;
  --card: #141414; --border: #262626; --hairline: #1f1f1f;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
html, body {
  background: var(--bg); color: var(--fg);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  -webkit-font-smoothing: antialiased; min-height: 100vh;
}
header {
  border-bottom: 1px solid var(--border);
  padding: 1.5rem 2rem; display: flex; align-items: baseline; gap: 1.25rem;
}
header h1 { font-size: 1.25rem; letter-spacing: -0.01em; }
header .muted { color: var(--muted); font-size: 0.875rem; }
main { padding: 1.5rem 2rem; max-width: 1600px; margin: 0 auto; }
.summary { display: grid; grid-template-columns: repeat(5, 1fr); gap: 1rem; margin-bottom: 2rem; }
.summary .stat {
  background: var(--card); border: 1px solid var(--border); border-radius: 0.5rem;
  padding: 1rem 1.25rem;
}
.summary .stat .lbl { color: var(--muted); font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.04em; }
.summary .stat .val { font-size: 1.5rem; margin-top: 0.25rem; font-variant-numeric: tabular-nums; }
.runs { display: grid; grid-template-columns: repeat(auto-fill, minmax(420px, 1fr)); gap: 1.25rem; }
.run {
  background: var(--card); border: 1px solid var(--border); border-radius: 0.6rem;
  overflow: hidden; display: flex; flex-direction: column;
}
.run header {
  border-bottom: 1px solid var(--hairline); padding: 0.75rem 1rem;
  display: flex; align-items: center; justify-content: space-between; gap: 0.5rem;
}
.run header .title { font-size: 0.875rem; }
.run header .title a { color: var(--accent); text-decoration: none; }
.run header .title a:hover { text-decoration: underline; }
.badge {
  font-size: 0.7rem; padding: 0.1rem 0.5rem; border-radius: 0.25rem; border: 1px solid var(--border);
  letter-spacing: 0.02em;
}
.badge.ok { color: var(--ok); border-color: var(--ok); }
.badge.err { color: var(--err); border-color: var(--err); }
.badge.warn { color: var(--warn); border-color: var(--warn); }
.body { display: grid; grid-template-columns: 240px 1fr; gap: 1rem; padding: 0.875rem 1rem; }
.body video {
  width: 100%; max-width: 240px; aspect-ratio: 9/16; background: #000;
  border: 1px solid var(--border); border-radius: 0.4rem;
}
.body .none { font-size: 0.8rem; color: var(--muted); padding: 0.75rem; border: 1px dashed var(--border); border-radius: 0.4rem; }
.body .meta dl {
  display: grid; grid-template-columns: max-content 1fr; gap: 0.25rem 0.75rem;
  font-size: 0.8rem; font-variant-numeric: tabular-nums;
}
.body .meta dt { color: var(--muted); }
.body .meta dd a { color: var(--accent); text-decoration: none; word-break: break-all; }
.body .meta dd a:hover { text-decoration: underline; }
.stages, .qc, .ledger {
  border-top: 1px solid var(--hairline); padding: 0.75rem 1rem;
}
.stages h3, .qc h3, .ledger h3 {
  font-size: 0.7rem; color: var(--muted); text-transform: uppercase; letter-spacing: 0.04em;
  margin-bottom: 0.4rem;
}
.stages table, .ledger table {
  width: 100%; font-size: 0.78rem; font-variant-numeric: tabular-nums; border-collapse: collapse;
}
.stages td, .ledger td { padding: 0.15rem 0.5rem 0.15rem 0; }
.stages td:first-child, .ledger td:first-child { color: var(--muted); }
.qc ul { list-style: none; font-size: 0.78rem; }
.qc li::before { content: "× "; color: var(--err); }
.qc.ok p { color: var(--ok); font-size: 0.78rem; }
.trace details { font-size: 0.8rem; border-top: 1px solid var(--hairline); padding: 0.5rem 1rem; }
.trace summary { color: var(--muted); cursor: pointer; }
.trace pre {
  background: #000; padding: 0.5rem; border-radius: 0.3rem; font-size: 0.7rem;
  margin-top: 0.5rem; overflow-x: auto; max-height: 320px;
}
.empty { padding: 4rem 2rem; text-align: center; color: var(--muted); }
.run.failed header { background: rgba(248, 113, 113, 0.07); }
</style>
</head>
<body>
<header>
  <h1>ACQ Clipper <span style="color: var(--accent)">/eval</span></h1>
  <span class="muted" id="hdr-meta"></span>
</header>
<main>
  <section class="summary" id="summary"></section>
  <section class="runs" id="runs">
    <div class="empty">loading…</div>
  </section>
</main>

<script>
const $ = (sel, root=document) => root.querySelector(sel);
const fmtUsd = (v) => "$" + Number(v || 0).toFixed(4);
const fmtMs  = (v) => `${Math.round(Number(v||0))}ms`;
const fmtPct = (n, d) => d ? `${Math.round(100*n/d)}%` : "0%";

const ytLink = (vid) => vid ? `https://www.youtube.com/watch?v=${vid}` : null;

async function load() {
  const r = await fetch("/eval/manifest.json", { cache: "no-store" });
  if (!r.ok) { renderEmpty("no manifest yet — run scripts/local_run.py"); return; }
  const m = await r.json();
  if (!m.runs || !m.runs.length) { renderEmpty("manifest is empty"); return; }
  $("#hdr-meta").textContent = `generated ${new Date((m.generated_at||0)*1000).toLocaleString()}`;

  renderSummary(m.runs);
  const root = $("#runs"); root.innerHTML = "";
  // Pull per-job details in parallel.
  const cards = await Promise.all(m.runs.map(buildRunCard));
  cards.forEach(c => root.appendChild(c));
}

function renderEmpty(msg) {
  $("#runs").innerHTML = `<div class="empty">${msg}</div>`;
}

function renderSummary(runs) {
  const totalCost = runs.reduce((s, r) => s + (r.total_cost_usd||0), 0);
  const totalClips = runs.reduce((s, r) => s + (r.n_clips||0), 0);
  const totalPassed = runs.reduce((s, r) => s + (r.n_qc_passed||0), 0);
  const okRuns = runs.filter(r => r.status === "done").length;
  const stats = [
    ["runs", `${runs.length}`],
    ["ok",   `${okRuns} / ${runs.length}`],
    ["clips", `${totalClips}`],
    ["qc passed", `${totalPassed} (${fmtPct(totalPassed, totalClips)})`],
    ["avg $/run", fmtUsd(runs.length ? totalCost / runs.length : 0)],
  ];
  $("#summary").innerHTML = stats.map(([lbl, val]) =>
    `<div class="stat"><div class="lbl">${lbl}</div><div class="val">${val}</div></div>`
  ).join("");
}

async function buildRunCard(row) {
  const card = document.createElement("article");
  card.className = "run" + (row.status === "done" ? "" : " failed");
  const badge = row.status === "done" ? "ok" : (row.status === "failed" ? "err" : "warn");

  const head = document.createElement("header");
  const link = ytLink(row.video_id) || row.url;
  head.innerHTML = `
    <span class="title">
      <a href="${link}" target="_blank" rel="noreferrer">
        ${row.video_id || (row.url || "").slice(0, 40)}
      </a>
    </span>
    <span class="badge ${badge}">${row.status}</span>`;
  card.appendChild(head);

  // Body — left: clip player(s). right: meta.
  const body = document.createElement("div");
  body.className = "body";
  const left = document.createElement("div");
  if (row.clips && row.clips.length) {
    row.clips.forEach(c => {
      const v = document.createElement("video");
      const safeUrl = `/eval/runs/${row.job_id}/clips/${row.video_id}__${c.clip_id}.mp4`;
      v.src = safeUrl;
      v.controls = true; v.preload = "metadata";
      left.appendChild(v);
    });
  } else {
    const empty = document.createElement("div");
    empty.className = "none";
    empty.textContent = row.error ? `error: ${row.error}` : "no clips produced";
    left.appendChild(empty);
  }
  body.appendChild(left);

  const meta = document.createElement("div");
  meta.className = "meta";
  meta.innerHTML = `<dl>
    <dt>job</dt><dd>${row.job_id}</dd>
    <dt>cost</dt><dd>${fmtUsd(row.total_cost_usd)}</dd>
    <dt>elapsed</dt><dd>${(row.elapsed_s||0).toFixed(1)}s</dd>
    <dt>clips</dt><dd>${row.n_clips} (${row.n_qc_passed} pass)</dd>
  </dl>`;
  body.appendChild(meta);
  card.appendChild(body);

  // QC failures (per clip)
  const qcWrap = document.createElement("div");
  qcWrap.className = "qc" + (row.clips && row.clips.every(c => c.qc_passed) ? " ok" : "");
  qcWrap.innerHTML = "<h3>qc</h3>";
  if (!row.clips || !row.clips.length) {
    qcWrap.innerHTML += `<p style="font-size:0.78rem;color:var(--err)">no clips to qc</p>`;
  } else if (row.clips.every(c => c.qc_passed)) {
    qcWrap.innerHTML += `<p>all clips passed</p>`;
  } else {
    const ul = document.createElement("ul");
    row.clips.forEach(c => {
      if (c.qc_passed) return;
      (c.qc_failures || []).forEach(f => {
        const li = document.createElement("li");
        li.textContent = `${c.clip_id}: ${f}`;
        ul.appendChild(li);
      });
    });
    qcWrap.appendChild(ul);
  }
  card.appendChild(qcWrap);

  // Stage breakdown — fetch state.json
  try {
    const sr = await fetch(`/eval/runs/${row.job_id}/state.json`, { cache: "no-store" });
    if (sr.ok) {
      const state = await sr.json();
      const stages = Object.values(state.stages || {});
      if (stages.length) {
        const wrap = document.createElement("div");
        wrap.className = "stages";
        const rows = stages.map(s => `
          <tr>
            <td>${s.name}</td>
            <td><span class="badge ${s.status === 'completed' ? 'ok' : 'err'}">${s.status}</span></td>
            <td>${fmtMs(s.duration_ms)}</td>
            <td>${fmtUsd(s.cost_usd)}</td>
            <td>${s.attempts}×</td>
          </tr>`).join("");
        wrap.innerHTML = `<h3>stages</h3><table>${rows}</table>`;
        card.appendChild(wrap);
      }
    }
  } catch {}

  // Ledger
  try {
    const lr = await fetch(`/eval/runs/${row.job_id}/ledger.json`, { cache: "no-store" });
    if (lr.ok) {
      const entries = await lr.json();
      if (entries.length) {
        const wrap = document.createElement("div");
        wrap.className = "ledger";
        const rows = entries.map(e => `
          <tr>
            <td>${e.stage}</td>
            <td>${fmtUsd(e.usd)}</td>
            <td style="color:var(--muted)">${(e.detail && e.detail.source) || ''}</td>
          </tr>`).join("");
        wrap.innerHTML = `<h3>ledger</h3><table>${rows}</table>`;
        card.appendChild(wrap);
      }
    }
  } catch {}

  // Trace (lazy expand)
  const trace = document.createElement("div");
  trace.className = "trace";
  trace.innerHTML = `<details><summary>trace ▾</summary><pre data-job="${row.job_id}">loading…</pre></details>`;
  trace.querySelector("details").addEventListener("toggle", async (e) => {
    if (!e.target.open) return;
    const pre = trace.querySelector("pre");
    if (pre.dataset.loaded) return;
    try {
      const r = await fetch(`/eval/runs/${row.job_id}/trace.json`, { cache: "no-store" });
      const spans = await r.json();
      pre.textContent = spans.map(s =>
        `${(s.name||'').padEnd(28)}  ${String(s.duration_ms||0).padStart(6)}ms  ${(s.attributes && s.attributes.status) || ''}`
      ).join("\n");
      pre.dataset.loaded = "1";
    } catch (err) {
      pre.textContent = "trace load failed: " + err;
    }
  });
  card.appendChild(trace);

  return card;
}

load();
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        sys.stderr.write(f"[serve] {self.address_string()} {format % args}\n")

    def do_GET(self):
        path = urlparse(self.path).path

        # 1) root -> /eval
        if path == "/" or path == "":
            self.send_response(302)
            self.send_header("location", "/eval")
            self.end_headers()
            return

        # 2) /eval = dashboard HTML
        if path == "/eval" or path == "/eval/":
            self._reply_text(200, DASHBOARD_HTML, "text/html; charset=utf-8")
            return

        # 3) /eval/manifest.json -> pipeline_runs/manifest.json
        if path == "/eval/manifest.json":
            mp = RUNS_DIR / "manifest.json"
            if not mp.exists():
                self._reply_text(404, '{"error":"no manifest"}', "application/json")
                return
            self._reply_file(mp)
            return

        # 4) /eval/runs/<job_id>/<rest...>  ->  pipeline_runs/<job_id>/<rest...>
        if path.startswith("/eval/runs/"):
            rel = unquote(path[len("/eval/runs/"):])
            # Defense in depth: no .., no absolute paths
            if ".." in rel.split("/") or rel.startswith("/") or rel.startswith("\\"):
                self._reply_text(400, '{"error":"bad path"}', "application/json")
                return
            target = (RUNS_DIR / rel).resolve()
            try:
                target.relative_to(RUNS_DIR.resolve())
            except ValueError:
                self._reply_text(400, '{"error":"escapes runs_dir"}', "application/json")
                return
            if not target.exists() or not target.is_file():
                self._reply_text(404, f'{{"error":"not found: {rel}"}}', "application/json")
                return
            self._reply_file(target)
            return

        # Fall through to processing/ — preserves the existing
        # `python -m http.server --directory processing` behavior so the
        # other agents' clip_dashboard / dashboard / inspect pages keep
        # working alongside /eval.
        rel = unquote(path.lstrip("/"))
        if ".." in rel.split("/"):
            self._reply_text(400, "bad path", "text/plain")
            return
        candidate = (FALLTHROUGH_DIR / rel).resolve()
        try:
            candidate.relative_to(FALLTHROUGH_DIR.resolve())
        except ValueError:
            self._reply_text(400, "bad path", "text/plain")
            return
        if candidate.is_dir():
            index = candidate / "index.html"
            if index.exists():
                self._reply_file(index)
                return
            self._reply_dir_listing(candidate, path)
            return
        if candidate.is_file():
            self._reply_file(candidate)
            return

        self._reply_text(404, f"not found: {path}", "text/plain")

    def _reply_dir_listing(self, dir_path: Path, url_path: str):
        rows = []
        for child in sorted(dir_path.iterdir()):
            slash = "/" if child.is_dir() else ""
            rows.append(f'<li><a href="{child.name}{slash}">{child.name}{slash}</a></li>')
        body = (
            f"<!doctype html><meta charset='utf-8'>"
            f"<title>{url_path}</title>"
            f"<h1>{url_path}</h1><ul>{''.join(rows)}</ul>"
        )
        self._reply_text(200, body, "text/html; charset=utf-8")

    def _reply_text(self, status, body, content_type):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(body)))
        self.send_header("cache-control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _reply_file(self, path: Path):
        ctype, _ = mimetypes.guess_type(path.name)
        ctype = ctype or "application/octet-stream"
        size = path.stat().st_size
        self.send_response(200)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(size))
        self.send_header("cache-control", "no-store")
        self.end_headers()
        with path.open("rb") as f:
            while True:
                chunk = f.read(64 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=3002,
                        help="port to bind (default 3002)")
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    if not RUNS_DIR.exists():
        print(f"NOTE: {RUNS_DIR} does not exist yet — run scripts/local_run.py first")

    try:
        srv = ThreadingHTTPServer((args.host, args.port), Handler)
    except OSError as e:
        print(f"could not bind {args.host}:{args.port} — {e}", file=sys.stderr)
        print(
            "Port is in use. Free it (taskkill on the listening PID) or "
            "pass --port=<other>.",
            file=sys.stderr,
        )
        return 1

    print(f"serving http://{args.host}:{args.port}/eval  (runs={RUNS_DIR})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
