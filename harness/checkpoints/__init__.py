"""Checkpoints — explicit pass/fail evaluators at every stage boundary.

Spec requirement (hackathon "Must"):
    "Checkpoints with explicit pass/fail criteria."
    "Checkpoint results are persisted — you can replay a run from any
     checkpoint forward without re-running prior stages."

Contract: a checkpoint takes the stage's output and returns a
CheckpointVerdict (passed, criteria, metrics, alarms). It NEVER raises.
The worker reads the verdict and decides what to do — that's how
"agent behavior changes based on checkpoint feedback."

One checkpoint per stage:
    checkpoint_ingest       — ingest produced a usable video
    checkpoint_transcribe   — transcript has words, language, speakers
    checkpoint_pick         — clip is in spec; faces likely present
    checkpoint_crop         — crop is 9:16, quality acceptable, faces centered
    checkpoint_captions     — caption drift in tolerance
    checkpoint_finalize     — final mp4 passes qc_clip + uploaded
"""
from .types import CheckpointVerdict, Criterion, CriterionResult  # noqa: F401
from .persistence import load_verdict, save_verdict, verdict_path  # noqa: F401
from .evaluators import (  # noqa: F401
    checkpoint_captions,
    checkpoint_crop,
    checkpoint_finalize,
    checkpoint_ingest,
    checkpoint_pick,
    checkpoint_transcribe,
)

__all__ = [
    "CheckpointVerdict",
    "Criterion",
    "CriterionResult",
    "checkpoint_captions",
    "checkpoint_crop",
    "checkpoint_finalize",
    "checkpoint_ingest",
    "checkpoint_pick",
    "checkpoint_transcribe",
    "load_verdict",
    "save_verdict",
    "verdict_path",
]
