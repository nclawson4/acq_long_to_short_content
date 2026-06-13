"""Output guardrails — the QC gate.

Two scopes:
    qc_clip(...)  — per-clip checks: true 9:16, duration in window, subject
                    centered, caption sync drift under tolerance, loudness in
                    spec. The orchestrator runs this between render and finalize.
    qc_run(...)   — whole-run checks: at least one clip accepted, total cost
                    inside ceiling, no stage left in error.

A failing check does NOT throw by default. The orchestrator decides whether
a failure means retry the producing stage, drop the clip, or fail the run.
That decision is in orchestrator.py — guardrails just report.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Optional

from ..types import FinalClip, QCVerdict

# --- target spec -----------------------------------------------------------

TARGET_WIDTH = 1080
TARGET_HEIGHT = 1920
TARGET_ASPECT = TARGET_WIDTH / TARGET_HEIGHT  # 0.5625

MIN_DURATION_S = 15.0
MAX_DURATION_S = 150.0  # Picker targets ≤90s, hard cap 150s (see find_moments/algorithm.py)

MAX_CAPTION_DRIFT_MS = 250.0   # half a typical Deepgram word — comfortable
TARGET_LUFS = -16.0            # YouTube Shorts / TikTok target
LUFS_TOLERANCE = 3.0           # ±3 LU is "in spec"

ACCEPTABLE_QUALITY_LABELS = {"good", "acceptable", "soft"}  # "soft" is 720p-source territory; ship with note. "poor" still fails.


class QCFailed(Exception):
    """Raised only when the orchestrator decides QC is a hard stop."""


def qc_clip(
    *,
    video_path: str,
    expected_width: int = TARGET_WIDTH,
    expected_height: int = TARGET_HEIGHT,
    expected_duration_s: Optional[float] = None,
    caption_drift_ms: Optional[float] = None,
    crop_quality_label: Optional[str] = None,
    face_centered: Optional[bool] = None,
) -> QCVerdict:
    """Inspect a finished clip MP4 and produce a verdict.

    Truth sources:
        - ffprobe on the file for width/height/duration/loudness (best-effort)
        - the crop tool's self-reported quality_label / face_centered
        - the caption tool's self-reported max_caption_drift_ms
    """
    failures: list[str] = []
    metrics: dict[str, float] = {}

    probe = _ffprobe(video_path)
    if probe is None:
        failures.append("ffprobe_unavailable")
        return QCVerdict(passed=False, failures=failures, metrics=metrics)

    w = probe.get("width") or 0
    h = probe.get("height") or 0
    dur = probe.get("duration_s") or 0.0
    metrics["width"] = float(w)
    metrics["height"] = float(h)
    metrics["duration_s"] = dur

    if w != expected_width or h != expected_height:
        failures.append(f"resolution_mismatch:{w}x{h}_vs_{expected_width}x{expected_height}")

    if dur < MIN_DURATION_S:
        failures.append(f"too_short:{dur:.1f}s")
    elif dur > MAX_DURATION_S:
        failures.append(f"too_long:{dur:.1f}s")

    if expected_duration_s is not None and abs(dur - expected_duration_s) > 0.5:
        failures.append(f"duration_drift:{dur:.2f}_vs_{expected_duration_s:.2f}")

    if caption_drift_ms is not None:
        metrics["caption_drift_ms"] = caption_drift_ms
        if caption_drift_ms > MAX_CAPTION_DRIFT_MS:
            failures.append(f"caption_drift:{caption_drift_ms:.0f}ms")

    if crop_quality_label is not None and crop_quality_label not in ACCEPTABLE_QUALITY_LABELS:
        failures.append(f"crop_quality:{crop_quality_label}")

    if face_centered is False:
        failures.append("face_not_centered")

    # Loudness — best-effort; the encode pipeline might already normalize.
    lufs = _ffprobe_loudness(video_path)
    if lufs is not None:
        metrics["lufs"] = lufs
        if abs(lufs - TARGET_LUFS) > LUFS_TOLERANCE:
            failures.append(f"loudness:{lufs:.1f}_vs_{TARGET_LUFS:.0f}")

    return QCVerdict(passed=not failures, failures=failures, metrics=metrics)


def qc_run(clips: list[FinalClip], *, total_cost_usd: float, ceiling_usd: float) -> QCVerdict:
    failures: list[str] = []
    metrics = {
        "total_cost_usd": total_cost_usd,
        "ceiling_usd": ceiling_usd,
        "n_clips": float(len(clips)),
        "n_accepted": float(sum(1 for c in clips if c.qc_passed)),
    }
    if not clips:
        failures.append("no_clips_produced")
    elif not any(c.qc_passed for c in clips):
        failures.append("no_clip_passed_qc")
    if total_cost_usd > ceiling_usd:
        failures.append(f"over_ceiling:{total_cost_usd:.2f}_vs_{ceiling_usd:.2f}")
    return QCVerdict(passed=not failures, failures=failures, metrics=metrics)


# ---------------- ffprobe helpers ----------------

def _ffprobe(video_path: str) -> Optional[dict]:
    if not Path(video_path).exists():
        return None
    try:
        out = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=width,height,r_frame_rate:format=duration",
                "-of", "json",
                video_path,
            ],
            capture_output=True, text=True, timeout=20,
        )
        if out.returncode != 0:
            return None
        data = json.loads(out.stdout or "{}")
        streams = data.get("streams") or [{}]
        fmt = data.get("format") or {}
        return {
            "width": int(streams[0].get("width") or 0),
            "height": int(streams[0].get("height") or 0),
            "duration_s": float(fmt.get("duration") or 0.0),
        }
    except (FileNotFoundError, subprocess.TimeoutExpired, ValueError, json.JSONDecodeError):
        return None


def _ffprobe_loudness(video_path: str) -> Optional[float]:
    """Integrated LUFS via ffmpeg ebur128. Best-effort; returns None on failure."""
    try:
        proc = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-nostats", "-i", video_path,
                "-filter_complex", "ebur128=peak=true", "-f", "null", "-",
            ],
            capture_output=True, text=True, timeout=60,
        )
        # The summary block on stderr ends with "Integrated loudness: -X.X LUFS"
        for line in (proc.stderr or "").splitlines()[::-1]:
            if "Integrated loudness" in line or "I:" in line and "LUFS" in line:
                # Try both formats
                for token in line.replace("I:", "").split():
                    try:
                        v = float(token)
                        if -70.0 < v < 0.0:
                            return v
                    except ValueError:
                        continue
        return None
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
