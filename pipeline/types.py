"""Stage I/O contracts. Every value that crosses a stage boundary is one of
these — that is what makes guardrail enforcement and replay possible.

Tool authors (tools 1, 2, 3) implement the protocols in pipeline.tools.adapters
against these types.
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field, HttpUrl


# ---------- input ----------

class PipelineInput(BaseModel):
    """What the API endpoint hands the orchestrator."""
    job_id: str
    url: HttpUrl


# ---------- stage 1: ingest ----------

class IngestResult(BaseModel):
    """yt-dlp output: a local mp4 + extracted metadata."""
    video_id: str
    video_path: str  # local fs path inside the function tmp dir
    audio_path: Optional[str] = None  # extracted mono 16kHz wav if we split it
    duration_s: float
    width: int
    height: int
    title: str
    uploader: Optional[str] = None
    source_url: str


# ---------- stage 2: transcribe ----------

class Word(BaseModel):
    text: str
    # Deepgram returns BOTH a plain `word` (lowercase, no punctuation) and a
    # `punctuated_word` (capitalized + trailing comma/period). Captions need
    # the punctuated form to read correctly; LLM cost estimation and
    # matching can use the plain form. We carry both. When the upstream
    # source doesn't distinguish, `punctuated` falls back to `text` at
    # consumption time.
    punctuated: Optional[str] = None
    start_s: float
    end_s: float
    confidence: float = 1.0
    speaker: Optional[int] = None


class TranscribeResult(BaseModel):
    words: list[Word]
    full_text: str
    language: str = "en"
    duration_s: float


# ---------- stage 3: pick_timestamps (tool 2) ----------

class ClipTimestamp(BaseModel):
    """Where to cut a single short out of the source video.

    `start_s`..`end_s` is the envelope on the SOURCE video.
    `kept_segments`, when present, is a non-empty list of [start, end] pairs
    (in source-video time) that the orchestrator should concat together
    before reframing — the picker uses this to drop low-value middle
    content while preserving the speaker's intro + payoff.

    Contract: when `kept_segments` is set, the orchestrator validates length
    against the SUM of the segment durations, not against `end_s - start_s`.
    """
    clip_id: str
    start_s: float
    end_s: float
    text: str  # transcript text for the window
    score: Optional[float] = None
    reason: Optional[str] = None  # why the tool picked it
    kept_segments: Optional[list[list[float]]] = None  # [[start, end], ...]


class PickTimestampsResult(BaseModel):
    clips: list[ClipTimestamp]


# ---------- stage 4: crop_video (tool 1) — per clip ----------

class CropResult(BaseModel):
    clip_id: str
    cropped_video_path: str
    output_width: int  # expected 1080
    output_height: int  # expected 1920
    quality_label: str  # good / acceptable / soft / poor
    face_centered: bool
    layout: str  # "single" or "split"


# ---------- stage 5: add_captions (tool 3) — per clip ----------

class CaptionResult(BaseModel):
    clip_id: str
    captioned_video_path: str
    caption_segment_count: int
    max_caption_drift_ms: Optional[float] = None


# ---------- stage 6: finalize (Blob upload) — per clip ----------

class FinalClip(BaseModel):
    clip_id: str
    blob_url: str
    duration_s: float
    size_bytes: int
    width: int
    height: int
    qc_passed: bool
    qc_failures: list[str] = Field(default_factory=list)


# ---------- whole-pipeline result ----------

class PipelineResult(BaseModel):
    job_id: str
    video_id: str
    clips: list[FinalClip]
    total_cost_usd: float
    cost_breakdown: dict[str, float]
    duration_ms: int
    trace_id: Optional[str] = None


# ---------- per-stage durable record ----------

class StageStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class StageRecord(BaseModel):
    name: str
    status: StageStatus = StageStatus.PENDING
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    attempts: int = 0
    cost_usd: float = 0.0
    duration_ms: int = 0
    error: Optional[str] = None
    span_id: Optional[str] = None
    # Stage output, schema depends on the stage. Stored as dict so we can
    # serialize without knowing the concrete type at read time.
    output: Optional[dict[str, Any]] = None


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    BUDGET_EXCEEDED = "budget_exceeded"
    TIMEOUT = "timeout"


class JobState(BaseModel):
    job_id: str
    url: str
    status: JobStatus = JobStatus.QUEUED
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: Optional[datetime] = None
    stages: dict[str, StageRecord] = Field(default_factory=dict)
    total_cost_usd: float = 0.0
    result: Optional[PipelineResult] = None
    error: Optional[str] = None


# ---------- QC gate ----------

class QCVerdict(BaseModel):
    """Output of a QC check at a stage boundary."""
    passed: bool
    failures: list[str] = Field(default_factory=list)
    metrics: dict[str, float] = Field(default_factory=dict)
