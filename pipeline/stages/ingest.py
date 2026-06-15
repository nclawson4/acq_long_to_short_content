"""Stage 1 — pull the source video with yt-dlp.

Bounds & guarantees this stage enforces:
    - URL has already passed the input guardrail (validate_url) — we trust it
    - Hard length cap (8 min per user spec) — refuse longer videos
    - mp4 output, video+audio merged
    - File lands inside the provided workdir (caller manages cleanup)

Cost: ~$0. We charge the ledger anyway with a near-zero entry so the trace
records the stage ran. ffprobe is used after download to validate format.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

# yt-dlp ships a console script, but on Vercel Python runtime the script's
# bin/ dir isn't on PATH inside the function. Invoking the module directly
# via `python -m yt_dlp` works both locally and on Vercel.
_YT_DLP = [sys.executable, "-m", "yt_dlp"]

from opentelemetry import trace

from ..observability.ledger import get_ledger
from ..observability.tracer import traced
from ..types import IngestResult


MAX_DURATION_S = 8 * 60 + 30  # 8 minutes with 30s grace for rounding
INGEST_FIXED_COST_USD = 0.0   # yt-dlp itself is free; CPU is metered separately

# Local cache produced by the validation pipeline. If a video_id from the
# requested URL is already present here we skip the download.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_CACHED_VIDEOS_DIR = _REPO_ROOT / "source_data" / "videos"
# Vercel runtime cache: /api/ingest predownloads the mp4 into this dir before
# /api/process kicks the harness, so the cache-hit branch below picks it up
# instead of trying yt-dlp (which YouTube blocks for datacenter IPs).
_PREFETCH_DIR = Path("/tmp/prefetched")


class IngestFailed(Exception):
    pass


def _extract_video_id(url: str) -> str | None:
    import re
    from urllib.parse import parse_qs, urlparse
    parsed = urlparse(url)
    qs = parse_qs(parsed.query or "")
    if "v" in qs and re.fullmatch(r"[A-Za-z0-9_-]{11}", qs["v"][0]):
        return qs["v"][0]
    for part in (parsed.path or "").split("/"):
        if re.fullmatch(r"[A-Za-z0-9_-]{11}", part):
            return part
    return None


@traced("ingest")
def ingest(url: str, workdir: Path, *, job_id: str) -> IngestResult:
    """Download `url` into `workdir`, return an IngestResult.

    The OTel span name is "ingest". The caller is expected to set the
    `video_id` and `job_id` attributes on the span if it wants them tagged.
    """
    span = trace.get_current_span()
    span.set_attribute("job_id", job_id)
    span.set_attribute("stage", "ingest")
    span.set_attribute("url", url)

    workdir.mkdir(parents=True, exist_ok=True)

    # ---- 0. cache check ----
    # If this URL's video_id is already on disk under source_data/videos,
    # skip the network round-trip entirely. Saves us ~30s of yt-dlp time
    # per video on re-runs and lets us iterate fast on the rest of the
    # pipeline without burning bandwidth.
    cached_id = _extract_video_id(url)
    if cached_id:
        prefetched = _PREFETCH_DIR / f"{cached_id}.mp4"
        local_cached = _CACHED_VIDEOS_DIR / f"{cached_id}.mp4"
        cached_video = (
            prefetched if prefetched.exists() and prefetched.stat().st_size > 0
            else local_cached
        )
        cached_info = _REPO_ROOT / "source_data" / "video_info" / f"{cached_id}.info.json"
        if cached_video.exists() and cached_video.stat().st_size > 0:
            duration = 0.0
            title = cached_id
            uploader = None
            width, height = 0, 0
            if cached_info.exists():
                try:
                    meta = json.loads(cached_info.read_text(encoding="utf-8"))
                    duration = float(meta.get("duration") or 0.0)
                    title = meta.get("title") or cached_id
                    uploader = meta.get("uploader")
                    width = int(meta.get("width") or 0)
                    height = int(meta.get("height") or 0)
                except Exception:
                    pass
            if duration <= 0:
                duration = _ffprobe_duration(cached_video)
            span.set_attribute("video_id", cached_id)
            span.set_attribute("duration_s", duration)
            span.set_attribute("video_path", str(cached_video))
            span.set_attribute("cache.hit", True)
            get_ledger().charge("ingest", 0.0, source="cache-hit")
            return IngestResult(
                video_id=cached_id,
                video_path=str(cached_video),
                duration_s=duration,
                width=width or 1280,
                height=height or 720,
                title=title,
                uploader=uploader,
                source_url=url,
            )

    # ---- 1. probe metadata first (cheap, no download) ----
    info = _yt_dlp_info(url)
    duration = float(info.get("duration") or 0.0)
    if duration <= 0:
        raise IngestFailed("could not determine video duration")
    if duration > MAX_DURATION_S:
        raise IngestFailed(
            f"video too long: {duration:.0f}s exceeds {MAX_DURATION_S}s cap"
        )

    video_id = info.get("id") or "unknown"
    title = info.get("title") or video_id
    uploader = info.get("uploader")
    width = int(info.get("width") or 0)
    height = int(info.get("height") or 0)
    span.set_attribute("video_id", video_id)
    span.set_attribute("duration_s", duration)
    span.set_attribute("cache.hit", False)

    # ---- 2. download ----
    out_path = workdir / f"{video_id}.mp4"
    if not out_path.exists():
        _yt_dlp_download(url, out_path)
    if not out_path.exists() or out_path.stat().st_size == 0:
        raise IngestFailed(f"download produced no file at {out_path}")
    span.set_attribute("video_path", str(out_path))
    span.set_attribute("size_bytes", out_path.stat().st_size)

    # ---- 3. cheap charge so the stage shows up in the ledger ----
    get_ledger().charge("ingest", INGEST_FIXED_COST_USD, source="yt-dlp")

    return IngestResult(
        video_id=video_id,
        video_path=str(out_path),
        duration_s=duration,
        width=width or 1280,
        height=height or 720,
        title=title,
        uploader=uploader,
        source_url=url,
    )


# ---------------- yt-dlp helpers ----------------

def _yt_dlp_info(url: str) -> dict[str, Any]:
    """Cheap metadata pull — no download. Uses subprocess so yt-dlp version
    drift doesn't break us via Python API churn."""
    proc = subprocess.run(
        [*_YT_DLP, "--dump-single-json", "--no-warnings", "--no-playlist", url],
        capture_output=True, text=True, timeout=60,
    )
    if proc.returncode != 0:
        raise IngestFailed(f"yt-dlp info failed: {proc.stderr.strip()[:300]}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise IngestFailed(f"yt-dlp returned non-JSON: {e}")


def _yt_dlp_download(url: str, out_path: Path) -> None:
    proc = subprocess.run(
        [
            *_YT_DLP,
            "--no-warnings",
            "--no-playlist",
            "-f", "bestvideo[ext=mp4][height<=1080]+bestaudio[ext=m4a]/best[ext=mp4][height<=1080]/best",
            "--merge-output-format", "mp4",
            "-o", str(out_path),
            url,
        ],
        capture_output=True, text=True, timeout=300,
    )
    if proc.returncode != 0:
        raise IngestFailed(f"yt-dlp download failed: {proc.stderr.strip()[:300]}")


def _ffprobe_duration(video_path: Path) -> float:
    try:
        out = subprocess.check_output(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(video_path)],
            timeout=15,
        ).decode().strip()
        return float(out)
    except (FileNotFoundError, subprocess.SubprocessError, ValueError):
        return 0.0
