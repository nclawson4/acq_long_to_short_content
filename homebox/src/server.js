// Home-box ingest server.
//
// Vercel's `/api/ingest` route POSTs `{url}` here over Tailscale Funnel. We run
// yt-dlp locally (residential IP → no YouTube bot wall), then upload the mp4 to
// Vercel Blob and return the blob URL. The Vercel harness picks it up from
// there as a cache-hit.
//
// Everything lives under C:\acq-ingest:
//   yt-dlp.exe, ffmpeg.exe, ffprobe.exe   — binaries
//   src/server.js                          — this file
//   downloads/                             — scratch mp4s (cleaned after upload)
//   server.log                             — append-only log

import { spawn } from "node:child_process";
import { createServer } from "node:http";
import { mkdirSync, readFileSync, unlinkSync, statSync, appendFileSync } from "node:fs";
import { resolve, join } from "node:path";
import { randomBytes } from "node:crypto";
import { put } from "@vercel/blob";

const BASE_DIR = resolve(process.env.ACQ_BASE_DIR ?? "C:\\acq-ingest");
const DOWNLOADS_DIR = join(BASE_DIR, "downloads");
const LOG_FILE = join(BASE_DIR, "server.log");
const YT_DLP = join(BASE_DIR, "yt-dlp.exe");
const FFPROBE = join(BASE_DIR, "ffprobe.exe");

const PORT = Number(process.env.ACQ_PORT ?? 8787);
const HOST = process.env.ACQ_HOST ?? "127.0.0.1";
const SHARED_SECRET = process.env.ACQ_INGEST_SECRET ?? "";
const BLOB_TOKEN = process.env.BLOB_READ_WRITE_TOKEN ?? "";

mkdirSync(DOWNLOADS_DIR, { recursive: true });

function log(...parts) {
  const line = `[${new Date().toISOString()}] ${parts.join(" ")}\n`;
  process.stdout.write(line);
  try { appendFileSync(LOG_FILE, line); } catch {}
}

function extractVideoId(url) {
  const m =
    url.match(/[?&]v=([A-Za-z0-9_-]{11})/) ||
    url.match(/youtu\.be\/([A-Za-z0-9_-]{11})/) ||
    url.match(/\/shorts\/([A-Za-z0-9_-]{11})/);
  return m ? m[1] : null;
}

function readJsonBody(req, maxBytes = 64 * 1024) {
  return new Promise((res, rej) => {
    const chunks = [];
    let total = 0;
    req.on("data", (c) => {
      total += c.length;
      if (total > maxBytes) { rej(new Error("body_too_large")); req.destroy(); return; }
      chunks.push(c);
    });
    req.on("end", () => {
      try { res(JSON.parse(Buffer.concat(chunks).toString("utf8") || "{}")); }
      catch { rej(new Error("invalid_json")); }
    });
    req.on("error", rej);
  });
}

function reply(res, status, payload) {
  const body = Buffer.from(JSON.stringify(payload));
  res.writeHead(status, {
    "content-type": "application/json",
    "content-length": body.length,
    "cache-control": "no-store",
  });
  res.end(body);
}

// Run yt-dlp with one of several player_client fallbacks. Even on a clean
// residential IP, individual clients occasionally throw transient errors
// (PO token, age-gate). Walking the list catches those without a retry from
// the caller's side.
const CLIENT_ATTEMPTS = ["", "ios", "web_safari", "tv_embedded", "android"];

function runYtDlp(url, outPath) {
  return new Promise((resolve) => {
    const tryOne = (idx) => {
      if (idx >= CLIENT_ATTEMPTS.length) return resolve({ ok: false, stderr: "all clients failed" });
      const client = CLIENT_ATTEMPTS[idx];
      const args = [
        "--no-warnings", "--no-playlist",
        "-f", "bestvideo[ext=mp4][height<=1080]+bestaudio[ext=m4a]/best[ext=mp4][height<=1080]/best",
        "--merge-output-format", "mp4",
        "-o", outPath,
        "--print", "after_move:%(title)s\t%(duration)s\t%(width)s\t%(height)s\t%(uploader)s",
      ];
      if (client) args.push("--extractor-args", `youtube:player_client=${client}`);
      args.push(url);

      const proc = spawn(YT_DLP, args, { stdio: ["ignore", "pipe", "pipe"] });
      let stdout = "", stderr = "";
      proc.stdout.on("data", (c) => stdout += c.toString());
      proc.stderr.on("data", (c) => stderr += c.toString());
      proc.on("close", (code) => {
        let size = 0;
        try { size = statSync(outPath).size; } catch {}
        if (code === 0 && size > 100_000) {
          const meta = stdout.trim().split("\n").pop()?.split("\t") ?? [];
          resolve({
            ok: true,
            client: client || "default",
            title: meta[0] || "",
            duration_s: Number(meta[1]) || 0,
            width: Number(meta[2]) || 0,
            height: Number(meta[3]) || 0,
            uploader: meta[4] || "",
            size,
            stderr_tail: stderr.slice(-500),
          });
        } else {
          log(`yt-dlp client=${client||"default"} exit=${code} size=${size} stderr_tail=${stderr.slice(-200).replace(/\n/g," ")}`);
          try { unlinkSync(outPath); } catch {}
          tryOne(idx + 1);
        }
      });
    };
    tryOne(0);
  });
}

const server = createServer(async (req, res) => {
  const t0 = Date.now();
  const url = new URL(req.url || "/", `http://${req.headers.host}`);

  if (req.method === "GET" && url.pathname === "/health") {
    return reply(res, 200, {
      ok: true,
      service: "acq-homebox-ingest",
      has_secret: !!SHARED_SECRET,
      has_blob_token: !!BLOB_TOKEN,
    });
  }

  if (req.method !== "POST" || url.pathname !== "/ingest") {
    return reply(res, 404, { error: "not_found" });
  }

  // Shared-secret check — Tailscale Funnel exposes us to the open internet,
  // so this is the only thing keeping randoms from burning our bandwidth.
  if (!SHARED_SECRET || req.headers["x-acq-secret"] !== SHARED_SECRET) {
    return reply(res, 401, { error: "unauthorized" });
  }

  let body;
  try { body = await readJsonBody(req); }
  catch (e) { return reply(res, 400, { error: String(e.message || e) }); }
  const videoUrl = String(body.url ?? "").trim();
  if (!videoUrl) return reply(res, 400, { error: "missing_url" });
  const videoId = extractVideoId(videoUrl);
  if (!videoId) return reply(res, 400, { error: "invalid_url" });

  const jobId = randomBytes(4).toString("hex");
  const outPath = join(DOWNLOADS_DIR, `${videoId}-${jobId}.mp4`);
  log(`ingest start id=${videoId} job=${jobId}`);

  const dl = await runYtDlp(videoUrl, outPath);
  if (!dl.ok) {
    return reply(res, 502, { error: "ytdlp_failed", detail: dl.stderr });
  }

  let blobUrl = "";
  try {
    const bytes = readFileSync(outPath);
    const blob = await put(`prefetched/${videoId}.mp4`, bytes, {
      access: "public",
      contentType: "video/mp4",
      allowOverwrite: true,
      token: BLOB_TOKEN || undefined,
    });
    blobUrl = blob.url;
  } catch (e) {
    log(`blob upload failed: ${e.message || e}`);
    return reply(res, 500, { error: "blob_upload_failed", detail: String(e.message || e) });
  } finally {
    try { unlinkSync(outPath); } catch {}
  }

  const ms = Date.now() - t0;
  log(`ingest done id=${videoId} job=${jobId} client=${dl.client} ms=${ms} size=${dl.size}`);
  return reply(res, 200, {
    ok: true,
    video_id: videoId,
    title: dl.title,
    duration_s: dl.duration_s,
    width: dl.width,
    height: dl.height,
    uploader: dl.uploader,
    blob_url: blobUrl,
    size_bytes: dl.size,
    ytdlp_client: dl.client,
    duration_ms: ms,
  });
});

server.listen(PORT, HOST, () => {
  log(`acq-homebox-ingest listening on http://${HOST}:${PORT} (secret=${SHARED_SECRET ? "set" : "MISSING"}, blob=${BLOB_TOKEN ? "set" : "MISSING"})`);
});

process.on("SIGINT", () => { log("SIGINT — shutting down"); server.close(() => process.exit(0)); });
process.on("SIGTERM", () => { log("SIGTERM — shutting down"); server.close(() => process.exit(0)); });
