"""Stage 6 — upload captioned 9:16 clip to Vercel Blob and run final QC.

Vercel Blob REST API: PUT https://blob.vercel-storage.com/<pathname>
with Authorization: Bearer <token>. Response includes the public URL.

QC happens AFTER upload so that even rejected clips are inspectable in Blob —
that's helpful for debugging why something failed without re-running. The
returned FinalClip carries the verdict; the orchestrator decides what to
ship to the user.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import httpx
from opentelemetry import trace

from ..guardrails.output import qc_clip
from ..observability.ledger import get_ledger
from ..observability.tracer import traced
from ..types import CaptionResult, CropResult, FinalClip, ClipTimestamp


BLOB_PUT_URL = "https://blob.vercel-storage.com"
# Conservative estimate: Vercel Blob storage + egress for ≤5MB shorts ≈ $0.001-$0.003.
BLOB_FIXED_COST_USD = 0.003


class FinalizeFailed(Exception):
    pass


@traced("finalize")
def finalize_clip(
    *,
    clip_timestamp: ClipTimestamp,
    crop: CropResult,
    caption: CaptionResult,
    job_id: str,
    video_id: str,
) -> FinalClip:
    span = trace.get_current_span()
    span.set_attribute("job_id", job_id)
    span.set_attribute("stage", "finalize")
    span.set_attribute("video_id", video_id)
    span.set_attribute("clip_id", clip_timestamp.clip_id)

    src_path = Path(caption.captioned_video_path)
    if not src_path.exists():
        raise FinalizeFailed(f"captioned file missing: {src_path}")

    # 0) Persist the MP4 to a stable location (the workdir is usually a
    # TemporaryDirectory that's about to be deleted). For local runs we use
    # CLIPS_OUTPUT_DIR; for Vercel deployments this dir is ephemeral too,
    # so the Blob URL is what callers should use.
    persistent_path = _copy_to_persistent_dir(
        src_path, job_id=job_id, video_id=video_id, clip_id=clip_timestamp.clip_id,
    )
    final_path = persistent_path or src_path

    # 1) Upload to Blob (or fall back to a local file URL for dev/test)
    blob_url = _upload_blob(
        final_path,
        remote_name=f"{job_id}/{video_id}__{clip_timestamp.clip_id}.mp4",
    )
    span.set_attribute("blob_url", blob_url)
    get_ledger().charge("finalize", BLOB_FIXED_COST_USD, source="vercel-blob")

    # 2) QC the file we just uploaded — last line of defense.
    verdict = qc_clip(
        video_path=str(final_path),
        expected_duration_s=clip_timestamp.end_s - clip_timestamp.start_s,
        caption_drift_ms=caption.max_caption_drift_ms,
        crop_quality_label=crop.quality_label,
        face_centered=crop.face_centered,
    )
    span.set_attribute("qc.passed", verdict.passed)
    if verdict.failures:
        span.set_attribute("qc.failures", ",".join(verdict.failures))

    size_bytes = final_path.stat().st_size
    return FinalClip(
        clip_id=clip_timestamp.clip_id,
        blob_url=blob_url,
        duration_s=clip_timestamp.end_s - clip_timestamp.start_s,
        size_bytes=size_bytes,
        width=crop.output_width,
        height=crop.output_height,
        qc_passed=verdict.passed,
        qc_failures=verdict.failures,
    )


def _copy_to_persistent_dir(src: Path, *, job_id: str, video_id: str, clip_id: str) -> Path | None:
    """If CLIPS_OUTPUT_DIR is set, copy the captioned mp4 there and return the
    new path. Otherwise return None and let the caller fall back to whatever
    the source path was.
    """
    base = os.environ.get("CLIPS_OUTPUT_DIR", "").strip()
    if not base:
        return None
    out_dir = Path(base) / job_id / "clips"
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / f"{video_id}__{clip_id}.mp4"
    shutil.copyfile(src, dest)
    return dest


def _upload_blob(local_path: Path, *, remote_name: str) -> str:
    """PUT the file to Vercel Blob and return the public URL.

    Falls back to a file:// URL when BLOB_READ_WRITE_TOKEN is not set, so
    the smoke test runs without needing a Blob store provisioned.
    """
    token = os.environ.get("BLOB_READ_WRITE_TOKEN", "")
    if not token:
        return f"file://{local_path.as_posix()}"

    with local_path.open("rb") as f:
        resp = httpx.put(
            f"{BLOB_PUT_URL}/{remote_name}",
            content=f.read(),
            headers={
                "Authorization": f"Bearer {token}",
                "x-content-type": "video/mp4",
            },
            timeout=120.0,
        )
    if resp.status_code >= 300:
        raise FinalizeFailed(f"blob upload {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    url = data.get("url")
    if not url:
        raise FinalizeFailed(f"blob upload returned no url: {data}")
    return url
