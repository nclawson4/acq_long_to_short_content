"""Tool protocols + working stubs.

Each tool is a typed Protocol so the other agents can hand us anything that
satisfies the shape — class, function, or imported module. The orchestrator
calls the protocol; it doesn't care who implements it.

The stub implementations exist so the pipeline runs end-to-end before the
real tools ship. They produce structurally-valid outputs that pass schema
checks. They do NOT produce broadcast-quality clips — the orchestrator's
QC gate will flag them as soft/poor so the operator never confuses a stub
run with a real one.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Protocol

from ..types import (
    ClipTimestamp,
    CaptionResult,
    CropResult,
    IngestResult,
    PickTimestampsResult,
    TranscribeResult,
)


# ============================================================
# Tool 2 — pick_timestamps
# ============================================================

class PickTimestampsTool(Protocol):
    """Pick the best Q&A windows out of a transcript.

    Contract:
        Input  : TranscribeResult (full word-timed transcript)
                 IngestResult (for duration + video metadata)
        Output : PickTimestampsResult — zero or more ClipTimestamps.
                 Each clip:
                     - start_s and end_s lie inside [0, duration_s]
                     - end_s - start_s in [15.0, 60.0]
                     - clip_id is unique within the result
                     - text is the transcript text for the window
        Cost   : Must call observability.ledger.get_ledger().charge(
                     "pick_timestamps", usd, ...) before returning.

    Failure mode (per architecture pillar 2):
        Return PickTimestampsResult(clips=[]) on a recoverable failure. Raise
        only on bugs the orchestrator cannot retry past.
    """

    def __call__(
        self,
        transcript: TranscribeResult,
        ingest: IngestResult,
    ) -> PickTimestampsResult: ...


class StubPickTimestamps:
    """Naive baseline: one clip covering the first 30s of the video.

    Replaced by tool 2 when it ships. Charges a near-zero LLM-equivalent cost
    so the ledger has an entry for the stage.
    """
    name = "stub_pick_timestamps"

    def __call__(self, transcript: TranscribeResult, ingest: IngestResult) -> PickTimestampsResult:
        from ..observability.ledger import get_ledger
        get_ledger().charge("pick_timestamps", 0.0001, source="stub")
        end = min(30.0, max(15.0, ingest.duration_s))
        if ingest.duration_s < 15.0:
            return PickTimestampsResult(clips=[])
        text = " ".join(w.text for w in transcript.words if w.end_s <= end)
        return PickTimestampsResult(clips=[
            ClipTimestamp(clip_id="00", start_s=0.0, end_s=end, text=text, score=0.5,
                          reason="stub: first 30s"),
        ])


# ============================================================
# Tool 1 — crop_video
# ============================================================

class CropVideoTool(Protocol):
    """Take a source video + a clip window and produce a 9:16 cropped MP4.

    Contract:
        Input  : IngestResult (the source video on disk),
                 ClipTimestamp (which window to cut),
                 workdir (Path to write the output to)
        Output : CropResult with:
                     - cropped_video_path: an mp4 on disk
                     - output_width == 1080, output_height == 1920
                     - quality_label in {good, acceptable, soft, poor}
                     - face_centered: bool — was the subject's face inside
                       the centered band of the output frame
                     - layout in {single, split}
        Cost   : Charge "crop_video" stage in the ledger.

    The architecture's "detect, don't blind-cut" rule lives inside this tool.
    """
    def __call__(
        self,
        ingest: IngestResult,
        clip: ClipTimestamp,
        *,
        workdir: Path,
    ) -> CropResult: ...


class StubCropVideo:
    """Simple ffmpeg center-crop + scale. NOT face-aware — produces a
    structurally-valid 9:16 MP4 the QC gate will mark "soft" so the operator
    knows it came from the stub.
    """
    name = "stub_crop_video"

    def __call__(self, ingest: IngestResult, clip: ClipTimestamp, *, workdir: Path) -> CropResult:
        from ..observability.ledger import get_ledger
        workdir.mkdir(parents=True, exist_ok=True)
        out_path = workdir / f"{clip.clip_id}.crop.mp4"
        src = ingest.video_path

        # Center-crop to a vertical aspect, then scale to 1080x1920.
        # Using crop=ih*9/16:ih on a 16:9 source picks the central 9:16 column.
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-ss", f"{clip.start_s:.3f}",
            "-to", f"{clip.end_s:.3f}",
            "-i", str(src),
            "-vf", "crop=ih*9/16:ih,scale=1080:1920",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart",
            str(out_path),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if proc.returncode != 0:
            raise RuntimeError(f"stub crop ffmpeg failed: {proc.stderr.strip()[:300]}")
        # Stub charges a small ffmpeg-CPU-time proxy.
        get_ledger().charge("crop_video", 0.005, source="stub", duration_s=clip.end_s - clip.start_s)
        return CropResult(
            clip_id=clip.clip_id,
            cropped_video_path=str(out_path),
            output_width=1080,
            output_height=1920,
            quality_label="soft",  # honest signal that this came from the stub
            face_centered=True,    # nominal — stub centers by geometry, not face
            layout="single",
        )


# ============================================================
# Tool 3 — add_captions
# ============================================================

class AddCaptionsTool(Protocol):
    """Burn captions into a cropped 9:16 clip.

    Contract:
        Input  : CropResult (cropped mp4 on disk),
                 words: list[Word] (word-level timings) clipped to the window,
                 clip_start_s: float — source-video time where the cropped MP4
                                begins. The adapter MUST use this to rebase
                                word timings into clip-local time; relying on
                                words[0].start_s biases captions earlier when
                                the first word is mid-sentence.
                 workdir: Path
        Output : CaptionResult with:
                     - captioned_video_path: mp4 on disk
                     - caption_segment_count: integer
                     - max_caption_drift_ms: float | None — peak observed
                       drift between caption start and the underlying word's
                       Deepgram timing. Used by the QC gate.
        Cost   : Charge "add_captions" stage.

    Style: single-color, single-line, centered in the middle band of the
    1080x1920 frame (per user spec — diverges from acq_auto_captions which
    colors by speaker).
    """
    def __call__(
        self,
        crop: CropResult,
        words,  # list[Word] clipped to the window
        *,
        clip_start_s: float,
        workdir: Path,
    ) -> CaptionResult: ...


class StubAddCaptions:
    """No-op: copy the cropped file through unchanged, claim zero captions.

    The orchestrator's QC gate will flag clips with 0 segments — the stub is
    fine for plumbing, not for shipping.
    """
    name = "stub_add_captions"

    def __call__(self, crop: CropResult, words, *, clip_start_s: float, workdir: Path) -> CaptionResult:
        from ..observability.ledger import get_ledger
        workdir.mkdir(parents=True, exist_ok=True)
        out_path = workdir / f"{crop.clip_id}.captioned.mp4"
        shutil.copyfile(crop.cropped_video_path, out_path)
        get_ledger().charge("add_captions", 0.0, source="stub")
        return CaptionResult(
            clip_id=crop.clip_id,
            captioned_video_path=str(out_path),
            caption_segment_count=0,
            max_caption_drift_ms=0.0,
        )
