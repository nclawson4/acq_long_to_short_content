import { NextRequest, NextResponse } from "next/server";
import { Sandbox } from "@vercel/sandbox";
import { put } from "@vercel/blob";

export const runtime = "nodejs";
export const maxDuration = 300;

// Bash blob that prepares the sandbox. Done at startup once per cold sandbox
// (~25-35s). Switch to an INGEST_SANDBOX_SNAPSHOT_ID later if cold-start
// latency starts mattering.
const YTDLP_INSTALL = `
set -e
sudo dnf install -y --skip-broken python3 python3-pip ffmpeg 2>&1 || sudo dnf install -y --skip-broken python3 python3-pip 2>&1
python3 -m pip install --quiet --upgrade yt-dlp
which yt-dlp || ls -la /usr/local/bin/yt-dlp || echo no-ytdlp
`;

function sandboxCreds() {
  const t = process.env.VERCEL_TOKEN;
  const team = process.env.VERCEL_TEAM_ID;
  const proj = process.env.VERCEL_PROJECT_ID;
  if (t && team && proj) return { token: t, teamId: team, projectId: proj };
  return {};
}

// Decodo residential proxy — what gets us past YouTube's bot wall. Each port
// is an independent sticky session; rotating per request keeps any single IP
// from accumulating suspicious traffic.
function pickProxyUrl(): string | null {
  const user = process.env.DECODO_PROXY_USER;
  const pass = process.env.DECODO_PROXY_PASS;
  const portsCsv = process.env.DECODO_PROXY_PORTS;
  if (!user || !pass || !portsCsv) return null;
  const ports = portsCsv.split(",").map((s) => s.trim()).filter(Boolean);
  if (ports.length === 0) return null;
  const port = ports[Math.floor(Math.random() * ports.length)];
  return `http://${encodeURIComponent(user)}:${encodeURIComponent(pass)}@gate.decodo.com:${port}`;
}

function extractVideoId(url: string): string | null {
  const m =
    url.match(/[?&]v=([A-Za-z0-9_-]{11})/) ??
    url.match(/youtu\.be\/([A-Za-z0-9_-]{11})/) ??
    url.match(/\/shorts\/([A-Za-z0-9_-]{11})/);
  return m ? m[1] : null;
}

export async function POST(req: NextRequest) {
  let body: { url?: string };
  try {
    body = await req.json();
  } catch {
    return NextResponse.json({ error: "invalid_json" }, { status: 400 });
  }
  const url = (body.url ?? "").trim();
  if (!url) {
    return NextResponse.json({ error: "missing_url" }, { status: 400 });
  }
  const videoId = extractVideoId(url);
  if (!videoId) {
    return NextResponse.json(
      { error: "invalid_url", detail: "Could not extract YouTube video id" },
      { status: 400 },
    );
  }

  const sandbox = await Sandbox.create({
    ...sandboxCreds(),
    runtime: "node24",
    timeout: 280_000,
  });

  try {
    await sandbox.runCommand("sh", ["-c", YTDLP_INSTALL]);

    const proxyUrl = pickProxyUrl();
    const proxyArgs = proxyUrl ? ["--proxy", proxyUrl] : [];

    // Probe: does the proxy even auth from inside this sandbox? If this
    // returns a US residential IP, the proxy works and the yt-dlp 407 is
    // a yt-dlp/urllib problem. If this 407s too, it's a sandbox-side
    // CONNECT issue. We write the URL to a tmpfile so it doesn't show up
    // in the shell command line / process list.
    let proxyProbe = "no-proxy";
    if (proxyUrl) {
      // Sandbox runCommand sends argv unparsed, so the proxy URL in argv
      // is fine — but `ps` would still show it; the tmpfile sidesteps that.
      const b64Url = Buffer.from(proxyUrl, "utf8").toString("base64");
      const probe = await sandbox.runCommand("sh", [
        "-c",
        `echo "${b64Url}" | base64 -d > /tmp/proxy.url && chmod 600 /tmp/proxy.url && curl -sS --max-time 12 --proxy "$(cat /tmp/proxy.url)" -o /tmp/probe.json -w "HTTP:%{http_code} TIME:%{time_total}s\\n" "https://api.ipify.org?format=json" 2>&1 ; echo "BODY: $(cat /tmp/probe.json 2>/dev/null)"`,
      ]);
      proxyProbe = (await probe.stdout()).trim();
      const probeErr = (await probe.stderr?.())?.toString().trim();
      if (probeErr) proxyProbe += " || stderr: " + probeErr;
    }

    // videoUrl is passed as a direct argv entry — no shell interpolation —
    // so quote/backtick/semicolon payloads can't escape into the sandbox shell.
    const ytdlp = await sandbox.runCommand("yt-dlp", [
      "--no-warnings",
      "--no-playlist",
      ...proxyArgs,
      "-f",
      "bestvideo[ext=mp4][height<=1080]+bestaudio[ext=m4a]/best[ext=mp4][height<=1080]/best",
      "--merge-output-format",
      "mp4",
      "-o",
      "/tmp/video.mp4",
      "--print",
      "after_move:%(title)s\t%(duration)s\t%(width)s\t%(height)s\t%(uploader)s",
      url,
    ]);
    const ytdlpStdout = (await ytdlp.stdout()).trim();
    const ytdlpStderr = (await ytdlp.stderr?.())?.toString() ?? "";

    const sizeCheck = await sandbox.runCommand("sh", [
      "-c",
      "stat -c %s /tmp/video.mp4 2>/dev/null || echo 0",
    ]);
    const size = Number((await sizeCheck.stdout()).trim()) || 0;
    const clientUsed = proxyUrl ? "proxy" : "direct";
    if (size < 100_000) {
      return NextResponse.json(
        {
          error: "ytdlp_failed",
          detail: `Downloaded ${size} bytes via ${clientUsed}. ${proxyUrl ? "Proxy may be unhealthy or quota-exhausted." : "No DECODO_* env vars set — running without proxy."}`,
          stderr_tail: ytdlpStderr.slice(-500),
          proxy_probe: proxyProbe,
        },
        { status: 502 },
      );
    }

    // Stream the file out of the sandbox to Vercel Blob. The sandbox base64
    // pipe is the documented exfil path; Blob `put` then publishes it on
    // the project's blob domain.
    const b64 = await sandbox.runCommand("base64", ["-w", "0", "/tmp/video.mp4"]);
    const bytes = Buffer.from((await b64.stdout()).trim(), "base64");

    const blob = await put(`prefetched/${videoId}.mp4`, bytes, {
      access: "public",
      contentType: "video/mp4",
      allowOverwrite: true,
    });

    // Parse the metadata line yt-dlp printed.
    const [title = videoId, durationStr = "0", width = "0", height = "0", uploader = ""] =
      ytdlpStdout.split("\n").pop()?.split("\t") ?? [];
    const duration = Number(durationStr) || 0;

    return NextResponse.json({
      ok: true,
      video_id: videoId,
      title,
      duration_s: duration,
      width: Number(width) || 0,
      height: Number(height) || 0,
      uploader,
      blob_url: blob.url,
      size_bytes: size,
      ytdlp_client: clientUsed,
    });
  } catch (err: unknown) {
    const msg = err instanceof Error ? err.message : String(err);
    return NextResponse.json(
      { error: "sandbox_failed", detail: msg },
      { status: 500 },
    );
  } finally {
    await sandbox.stop().catch(() => {});
  }
}

export async function GET() {
  return NextResponse.json({ ok: true, endpoint: "ingest" });
}
