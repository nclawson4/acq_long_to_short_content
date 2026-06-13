"""Typed tool specs the worker can call.

Each ToolSpec bundles:
    - The callable
    - A JSON-schema parameter spec (so workers can advertise tools to LLMs)
    - The stage label (for cost-by-stage + alarm attribution)
    - The matching checkpoint evaluator
    - An output_to_dict function (so the result envelope is JSON-clean)

The harness builds its tool dict from `build_default_specs()` at startup.
Adding a new tool means appending one ToolSpec — no other files change.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional

from harness.checkpoints import (
    checkpoint_captions,
    checkpoint_crop,
    checkpoint_finalize,
    checkpoint_ingest,
    checkpoint_pick,
    checkpoint_transcribe,
)


@dataclass
class ToolSpec:
    name: str
    description: str
    stage: str                                      # ledger / alarm label
    parameters_json_schema: dict[str, Any]          # JSON Schema for the args
    fn: Callable[..., Any]                          # the callable
    checkpoint: Optional[Callable[..., Any]] = None
    output_to_dict: Optional[Callable[[Any], dict]] = None

    def llm_tool_format(self) -> dict[str, Any]:
        """Shape compatible with Anthropic Messages API `tools` param."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.parameters_json_schema,
        }


def build_default_specs() -> list[ToolSpec]:
    """Construct the canonical tool list. Lazy imports keep the registry
    importable even when adapters are not yet installed."""
    from pathlib import Path as _Path
    from pipeline.stages.ingest import ingest as _ingest_raw
    from pipeline.stages.transcribe import transcribe as _transcribe_raw
    from pipeline.stages.finalize import finalize_clip as _finalize_raw
    from pipeline.tools.registry import get_tool as _get_tool

    # Adapter shims — present a clean kwargs surface to the worker, then
    # pass through to the existing pipeline signatures (which were optimized
    # for the deterministic orchestrator, not for an agent).
    def ingest_fn(*, url, workdir, job_id, **_):
        return _ingest_raw(url, _Path(workdir), job_id=job_id)

    def transcribe_fn(*, ingest, job_id, **_):
        return _transcribe_raw(ingest, job_id=job_id)

    def finalize_fn(*, clip, crop, caption, job_id, video_id, **_):
        return _finalize_raw(
            clip_timestamp=clip, crop=crop, caption=caption,
            job_id=job_id, video_id=video_id,
        )

    def _crop_tool(**kwargs):
        return _get_tool("crop_video")(**kwargs)

    def _captions_tool(**kwargs):
        return _get_tool("add_captions")(**kwargs)

    def _pick_tool(**kwargs):
        return _get_tool("pick_timestamps")(**kwargs)

    return [
        ToolSpec(
            name="ingest",
            description="Download a YouTube video and extract metadata via yt-dlp. "
                        "Returns a local mp4 path plus dimensions and duration.",
            stage="ingest",
            parameters_json_schema={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Canonical YouTube URL."},
                    "workdir": {"type": "string", "description": "Local dir for the mp4."},
                    "job_id": {"type": "string"},
                },
                "required": ["url", "workdir", "job_id"],
            },
            fn=ingest_fn,
            checkpoint=checkpoint_ingest,
        ),
        ToolSpec(
            name="transcribe",
            description="Transcribe video audio via Deepgram nova-3. Returns "
                        "word-level timings with confidences and speaker labels.",
            stage="transcribe",
            parameters_json_schema={
                "type": "object",
                "properties": {
                    "ingest": {"type": "object"},
                    "job_id": {"type": "string"},
                },
                "required": ["ingest", "job_id"],
            },
            fn=transcribe_fn,
            checkpoint=checkpoint_transcribe,
        ),
        ToolSpec(
            name="pick_timestamps",
            description="Pick one viable short-form clip from the transcript. "
                        "Returns start/end timestamps + optional kept_segments.",
            stage="pick_timestamps",
            parameters_json_schema={
                "type": "object",
                "properties": {
                    "transcript": {"type": "object"},
                    "ingest": {"type": "object"},
                },
                "required": ["transcript", "ingest"],
            },
            fn=_pick_tool,
            checkpoint=checkpoint_pick,
        ),
        ToolSpec(
            name="crop_video",
            description="Crop the source mp4 to 9:16 with the speaker centered. "
                        "Uses YOLO face detection. Returns the cropped path + quality label.",
            stage="crop_video",
            parameters_json_schema={
                "type": "object",
                "properties": {
                    "ingest": {"type": "object"},
                    "clip": {"type": "object"},
                    "workdir": {"type": "string"},
                },
                "required": ["ingest", "clip", "workdir"],
            },
            fn=_crop_tool,
            checkpoint=checkpoint_crop,
        ),
        ToolSpec(
            name="add_captions",
            description="Burn yellow-style captions onto the cropped clip using "
                        "word-level timings. Returns the captioned path + drift estimate.",
            stage="add_captions",
            parameters_json_schema={
                "type": "object",
                "properties": {
                    "crop": {"type": "object"},
                    "words": {"type": "array"},
                    "clip_start_s": {"type": "number"},
                    "workdir": {"type": "string"},
                },
                "required": ["crop", "words", "clip_start_s", "workdir"],
            },
            fn=_captions_tool,
            checkpoint=checkpoint_captions,
        ),
        ToolSpec(
            name="finalize",
            description="Upload the captioned clip to Vercel Blob and run final QC. "
                        "Returns a FinalClip with blob URL and qc verdict.",
            stage="finalize",
            parameters_json_schema={
                "type": "object",
                "properties": {
                    "caption": {"type": "object"},
                    "crop": {"type": "object"},
                    "clip": {"type": "object"},
                    "job_id": {"type": "string"},
                    "video_id": {"type": "string"},
                },
                "required": ["caption", "crop", "clip", "job_id", "video_id"],
            },
            fn=finalize_fn,
            checkpoint=checkpoint_finalize,
        ),
    ]
