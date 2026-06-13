"""Material handling — the typed in/out box.

Spec requirement (hackathon "Must" — the four components):
    "Clean interfaces for passing material in and out."

The pipeline's stage I/O types already exist in `pipeline.types` (Pydantic
models for every cross-stage value). This module re-exports them under the
harness namespace and adds the run-level envelopes (Inbox + Outbox) the
worker reads and writes.

The deck's pillar-2 (Tools) language: "Return predictable, parseable output.
Errors come back as data the model can react to, not crashes." Material
handling is that contract.
"""
from .envelopes import Inbox, Outbox, ToolCallResult  # noqa: F401

# Re-export the stage types so the worker imports them from one place.
from pipeline.types import (  # noqa: F401
    CaptionResult,
    ClipTimestamp,
    CropResult,
    FinalClip,
    IngestResult,
    JobState,
    JobStatus,
    PickTimestampsResult,
    PipelineInput,
    PipelineResult,
    QCVerdict,
    StageRecord,
    StageStatus,
    TranscribeResult,
    Word,
)

__all__ = [
    "CaptionResult",
    "ClipTimestamp",
    "CropResult",
    "FinalClip",
    "Inbox",
    "IngestResult",
    "JobState",
    "JobStatus",
    "Outbox",
    "PickTimestampsResult",
    "PipelineInput",
    "PipelineResult",
    "QCVerdict",
    "StageRecord",
    "StageStatus",
    "ToolCallResult",
    "TranscribeResult",
    "Word",
]
