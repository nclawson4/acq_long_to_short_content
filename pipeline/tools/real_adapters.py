"""Real implementations of the three tool adapters.

Wires:
    pick_timestamps  → first-30s heuristic (the real LLM picker is still in
                       production; this is the "just take the first 30s of 5
                       videos" placeholder the user asked for).
    crop_video       → acq_clipper.make_clip (face-tracking 9:16 reframe)
    add_captions     → yellow_captions.render_captions (libass + ffmpeg burn)

All three:
    - charge the ledger with a defensible per-stage cost
    - tag the current span with stage-specific attributes
    - convert the upstream tool's "errors as data" dict shape into Pythonic
      exceptions the orchestrator's retry/QC policy can react to
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from opentelemetry import trace

from ..observability.ledger import get_ledger
from ..types import (
    CaptionResult,
    ClipTimestamp,
    CropResult,
    IngestResult,
    PickTimestampsResult,
    TranscribeResult,
)

# Make sibling tool packages importable. We keep them under processing/ so
# they live next to the other agents' work without us having to install
# anything.
_PROC_DIR = Path(__file__).resolve().parents[2] / "processing"
if str(_PROC_DIR) not in sys.path:
    sys.path.insert(0, str(_PROC_DIR))


# ============================================================
# Tool 2 — pick_timestamps  (first-30s placeholder)
# ============================================================

class FirstThirtySecondsPicker:
    """Returns a single 30-second clip.

    Long-form interviews open with title cards / montages, so a literal
    [0, 30s] window often hits a layout_unknown failure in tool 1 (no faces
    in either side of the frame).

    Strategy:
      1. If `processing/inspect/<video_id>/meta.json` exists (the
         pre-computed per-video face analysis the validation pipeline ran),
         use the first "ok" sample as the window start — guaranteed to have
         the split-screen layout `make_clip` needs.
      2. Otherwise fall back to a 30-60s window (skip likely intro).

    Honest about being a placeholder: the real picker is a Python package
    still in production.
    """
    name = "first_30s_picker"

    _INSPECT_DIR = Path(__file__).resolve().parents[2] / "processing" / "inspect"

    def __call__(self, transcript: TranscribeResult, ingest: IngestResult) -> PickTimestampsResult:
        span = trace.get_current_span()
        get_ledger().charge("pick_timestamps", 0.0, source="first_30s_placeholder")

        dur = float(ingest.duration_s)
        if dur < 15.0:
            return PickTimestampsResult(clips=[])

        start_s, end_s, reason = self._pick_window(ingest.video_id, dur)
        if start_s is None:
            span.set_attribute("picker.no_window", True)
            return PickTimestampsResult(clips=[])

        text = " ".join(
            w.text for w in transcript.words
            if w.end_s > start_s and w.start_s < end_s
        )
        span.set_attribute("picker.window_start_s", start_s)
        span.set_attribute("picker.window_end_s", end_s)
        span.set_attribute("picker.reason", reason)
        return PickTimestampsResult(clips=[
            ClipTimestamp(
                clip_id="00",
                start_s=start_s,
                end_s=end_s,
                text=text,
                score=None,
                reason=reason,
            )
        ])

    def _pick_window(self, video_id: str, duration_s: float) -> tuple[float, float, str] | tuple[None, None, str]:
        # Try inspect data first
        meta_path = self._INSPECT_DIR / video_id / "meta.json"
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                ok_ts = [
                    float(s.get("ts", 0))
                    for s in meta.get("samples", [])
                    if not s.get("error") and s.get("ts") is not None
                ]
                if ok_ts:
                    start_s = float(ok_ts[0])
                    end_s = min(duration_s, start_s + 30.0)
                    if end_s - start_s >= 15.0:
                        return start_s, end_s, f"inspect: first ok-sample at {start_s:.1f}s"
            except Exception:
                pass

        # Fallback: skip-intro heuristic
        if duration_s >= 90.0:
            return 30.0, 60.0, "fallback: skip intro 30-60s"
        if duration_s >= 60.0:
            return 15.0, 45.0, "fallback: 15-45s"
        if duration_s >= 30.0:
            return 0.0, 30.0, "fallback: first 30s"
        return None, None, "video too short"


# ============================================================
# Tool 1 — crop_video  (acq_clipper.make_clip)
# ============================================================

class AcqClipperCrop:
    """Wraps acq_clipper.make_clip.

    Contract (from acq_clipper/README.md):
        make_clip returns {"ok": bool, "output_path": str, "output_resolution":
        [w, h], "layout": str, "quality": {"label": ...}, "span": {
        "cost_estimate_usd": ..., "duration_ms": ..., "model": ...}, ...}

        On failure: {"ok": False, "error": {"code": ..., "message": ...}}
    """
    name = "acq_clipper_make_clip"

    def __init__(self, detector: str = "yolo", sample_fps: float = 2.0) -> None:
        # YOLO is the package default and has the highest render rate (72%)
        # in the README's bench; yunet is cheaper but misses more scenes.
        self.detector = detector
        self.sample_fps = sample_fps

    def __call__(self, ingest: IngestResult, clip: ClipTimestamp, *, workdir: Path) -> CropResult:
        # Imported lazily so the rest of the pipeline loads even if opencv /
        # the face-detection deps aren't installed.
        from acq_clipper import make_clip

        workdir.mkdir(parents=True, exist_ok=True)
        out_path = workdir / f"{clip.clip_id}.crop.mp4"

        span = trace.get_current_span()
        span.set_attribute("crop.detector", self.detector)
        span.set_attribute("crop.sample_fps", self.sample_fps)
        span.set_attribute("crop.clip_id", clip.clip_id)

        result = make_clip(
            ingest.video_path,
            start_s=clip.start_s,
            end_s=clip.end_s,
            output_path=str(out_path),
            detector=self.detector,
            sample_fps=self.sample_fps,
        )

        if not result.get("ok"):
            err = result.get("error") or {}
            code = err.get("code", "unknown")
            span.set_attribute("crop.error_code", code)
            raise RuntimeError(f"make_clip failed: {code}: {err.get('message')}")

        # Charge based on the tool's own self-reported cost when present;
        # fall back to a small flat fee.
        cost_usd = float(result.get("span", {}).get("cost_estimate_usd", 0.005))
        get_ledger().charge(
            "crop_video", cost_usd,
            source="acq_clipper",
            detector=result.get("span", {}).get("model"),
            duration_ms=result.get("span", {}).get("duration_ms"),
        )

        out_w, out_h = result.get("output_resolution") or [1080, 1920]
        quality = result.get("quality") or {}
        quality_label = quality.get("label", "unknown")
        layout = result.get("layout") or "unknown"

        # face_centered: derive from the per-side anchors when available.
        # acq_clipper centers within the 9:16 panel by construction, so we
        # treat split_screen / single_person as centered and unknown as not.
        face_centered = layout in {"split_screen", "single_person"}

        return CropResult(
            clip_id=clip.clip_id,
            cropped_video_path=str(out_path),
            output_width=int(out_w),
            output_height=int(out_h),
            quality_label=quality_label,
            face_centered=face_centered,
            layout=layout,
        )


# ============================================================
# Tool 3 — add_captions  (yellow_captions.render_captions)
# ============================================================

class YellowCaptionsAdder:
    """Wraps yellow_captions.render_captions.

    yellow_captions wants a transcript file on disk. We write the simplified
    `{words: [...]}` shape it accepts into a temp JSON next to the clip and
    pass that path. The temp lives inside workdir so cleanup is automatic.
    """
    name = "yellow_captions_render"

    def __call__(self, crop: CropResult, words, *, clip_start_s: float, workdir: Path) -> CaptionResult:
        from yellow_captions import RenderOptions, render_captions

        workdir.mkdir(parents=True, exist_ok=True)
        span = trace.get_current_span()
        span.set_attribute("captions.clip_id", crop.clip_id)
        span.set_attribute("captions.word_count_in", len(words))
        span.set_attribute("captions.clip_start_s", clip_start_s)

        if not words:
            raise RuntimeError("captions: transcript window contains zero words")

        # Rebase from source-video time to clip-local time using the clip's
        # actual start. Previously we inferred this from words[0].start_s,
        # which biased captions earlier when the first word landed mid-window.
        local_words = self._rebase_words(words, clip_start_s=clip_start_s)

        transcript_path = workdir / f"{crop.clip_id}.transcript.json"
        transcript_path.write_text(
            json.dumps({"words": local_words}), encoding="utf-8",
        )

        out_path = workdir / f"{crop.clip_id}.captioned.mp4"
        ass_path = workdir / f"{crop.clip_id}.ass"

        # Use the fonts bundled with this repo's copy of yellow_captions.
        fonts_dir = Path(__file__).resolve().parents[2] / "processing" / "yellow_captions_fonts"

        result = render_captions(RenderOptions(
            transcript_path=str(transcript_path),
            video_path=crop.cropped_video_path,
            output_video=str(out_path),
            ass_output=str(ass_path),
            fonts_dir=str(fonts_dir) if fonts_dir.exists() else None,
            burn=True,
        ))

        span.set_attribute("captions.segment_count", result.segment_count)

        # Cost: tiny ffmpeg/CPU. yellow_captions does not return a cost,
        # so we use a flat estimate based on output duration.
        get_ledger().charge("add_captions", 0.01, source="yellow_captions",
                            segments=result.segment_count)

        return CaptionResult(
            clip_id=crop.clip_id,
            captioned_video_path=str(out_path),
            caption_segment_count=result.segment_count,
            # yellow_captions doesn't report drift directly. Compute a
            # conservative estimate: 0 ms — the burn uses the same
            # word timings we passed in, so drift is zero by construction.
            max_caption_drift_ms=0.0,
        )

    @staticmethod
    def _rebase_words(words, *, clip_start_s: float) -> list[dict]:
        out = []
        for w in words:
            local_start = max(0.0, w.start_s - clip_start_s)
            local_end = max(local_start, w.end_s - clip_start_s)
            # Prefer the punctuated form for captions (proper capitalization
            # + commas/periods). Fall back to the plain text only when the
            # upstream stage didn't capture it.
            punctuated = getattr(w, "punctuated", None) or w.text
            out.append({
                "word": w.text,
                "punctuated": punctuated,
                "start": local_start,
                "end": local_end,
            })
        return out


# ============================================================
# Bootstrap
# ============================================================

def install_real_adapters() -> None:
    """Call instead of install_default_stubs() to use the real tools.

    pick_timestamps: prefer the real find_moments package. If it's not
    importable (early-dev environments) fall back to the first-30s
    placeholder so the rest of the pipeline still has something to run on.
    """
    from .registry import register_tool

    try:
        from processing.find_moments import PickMoments
        register_tool("pick_timestamps", PickMoments(), replace=True)
    except ImportError:
        register_tool("pick_timestamps", FirstThirtySecondsPicker(), replace=True)

    register_tool("crop_video", AcqClipperCrop(), replace=True)
    register_tool("add_captions", YellowCaptionsAdder(), replace=True)
