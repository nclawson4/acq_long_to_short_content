"""Checkpoint persistence — write verdicts to disk for replay.

Layout under a run directory:

    pipeline_runs/<job_id>/
        checkpoints/
            01_ingest.json
            02_transcribe.json
            03_pick.json
            04_crop_<clip_id>.json
            05_captions_<clip_id>.json
            06_finalize_<clip_id>.json

Replay flow: a runner can read these JSONs back, skip stages whose verdict
passed, and resume from the first failing checkpoint. Stage outputs are
already persisted by orchestrator state.RunState; checkpoint verdicts just
add the pass/fail layer on top.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from .types import CheckpointVerdict


_STAGE_ORDER = {
    "ingest": 1,
    "transcribe": 2,
    "pick_timestamps": 3,
    "pick": 3,
    "crop_video": 4,
    "crop": 4,
    "add_captions": 5,
    "captions": 5,
    "finalize": 6,
}


def verdict_path(run_dir: str | Path, stage: str, clip_id: Optional[str] = None) -> Path:
    """Return the canonical on-disk path for a checkpoint verdict.

    Naming preserves stage ORDER as a numeric prefix so a directory listing
    reads top-to-bottom in pipeline order — useful when scanning a run.
    """
    n = _STAGE_ORDER.get(stage, 99)
    base = f"{n:02d}_{stage}"
    if clip_id:
        base = f"{base}_{clip_id}"
    return Path(run_dir) / "checkpoints" / f"{base}.json"


def save_verdict(
    run_dir: str | Path,
    verdict: CheckpointVerdict,
    clip_id: Optional[str] = None,
) -> Path:
    """Persist a verdict as JSON. Returns the written path."""
    path = verdict_path(run_dir, verdict.stage, clip_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(verdict.model_dump_json(indent=2), encoding="utf-8")
    return path


def load_verdict(
    run_dir: str | Path,
    stage: str,
    clip_id: Optional[str] = None,
) -> Optional[CheckpointVerdict]:
    """Read a verdict back, or None if missing/malformed."""
    path = verdict_path(run_dir, stage, clip_id)
    if not path.exists():
        return None
    try:
        return CheckpointVerdict.model_validate_json(path.read_text(encoding="utf-8"))
    except Exception:
        return None
