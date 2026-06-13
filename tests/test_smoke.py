"""End-to-end smoke test for the orchestrator.

Strategy: monkeypatch `ingest` and `transcribe` to return fixture data (so we
don't hit yt-dlp / Deepgram), install the stub tools for 1/2/3, run the
orchestrator. Assert:

    - the pipeline finishes (status=done or failed-due-to-known-stub-fault)
    - every stage we expected has a record in state.stages
    - the ledger has entries for every stage that should have charged
    - total cost is well under the $1 ceiling
    - the trace dump has spans tagged with our job_id

The stub for tool 1 (crop) needs ffmpeg + a real source file. To stay
self-contained we also stub `finalize_clip` so the test does not require
ffmpeg or a real source.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_root = Path(__file__).resolve().parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

# Ensure required env vars are present before importing config.
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-test")
os.environ.setdefault("DEEPGRAM_API_KEY", "deepgram-test")

from pipeline.orchestrator import Orchestrator  # noqa: E402
import pipeline.orchestrator as orch_mod  # noqa: E402
from pipeline.state.store import reset_store_for_tests  # noqa: E402
from pipeline.tools import dispatch, list_tools  # noqa: F401, E402
from pipeline.tools.registry import install_default_stubs, register_tool, reset_registry_for_tests  # noqa: E402
from pipeline.types import (  # noqa: E402
    CaptionResult,
    ClipTimestamp,
    CropResult,
    FinalClip,
    IngestResult,
    PickTimestampsResult,
    TranscribeResult,
    Word,
)


# ---------------- fixtures ----------------

def fake_ingest(url, workdir, *, job_id):
    from pipeline.observability.ledger import get_ledger
    workdir.mkdir(parents=True, exist_ok=True)
    get_ledger().charge("ingest", 0.0, source="test-stub")
    return IngestResult(
        video_id="testvid_abc",
        video_path=str(workdir / "testvid.mp4"),
        duration_s=180.0,
        width=1280,
        height=720,
        title="A Test Video",
        uploader="Acquisition.com",
        source_url=str(url),
    )


def fake_transcribe(ingest_result, *, job_id):
    from pipeline.observability.ledger import get_ledger
    get_ledger().charge("transcribe", 0.013, source="test-stub",
                        minutes=ingest_result.duration_s / 60.0)
    words = [
        Word(text=w, start_s=i * 0.5, end_s=(i + 1) * 0.5, confidence=0.95, speaker=0)
        for i, w in enumerate(["hello", "this", "is", "a", "test", "transcript"] * 20)
    ]
    return TranscribeResult(
        words=words,
        full_text=" ".join(w.text for w in words),
        language="en",
        duration_s=180.0,
    )


def fake_finalize(*, clip_timestamp, crop, caption, job_id, video_id):
    from pipeline.observability.ledger import get_ledger
    get_ledger().charge("finalize", 0.003, source="test-stub")
    return FinalClip(
        clip_id=clip_timestamp.clip_id,
        blob_url=f"file://{caption.captioned_video_path}",
        duration_s=clip_timestamp.end_s - clip_timestamp.start_s,
        size_bytes=12345,
        width=crop.output_width,
        height=crop.output_height,
        qc_passed=True,
        qc_failures=[],
    )


# Tool stubs that don't need ffmpeg --------------------------------

class NoFfmpegPickTimestamps:
    def __call__(self, transcript: TranscribeResult, ingest):
        from pipeline.observability.ledger import get_ledger
        get_ledger().charge("pick_timestamps", 0.01, source="test-stub")
        return PickTimestampsResult(clips=[
            ClipTimestamp(clip_id="00", start_s=0.0, end_s=30.0,
                          text="hello this is a test", score=0.8),
            ClipTimestamp(clip_id="01", start_s=60.0, end_s=90.0,
                          text="test transcript continues", score=0.7),
        ])


class NoFfmpegCropVideo:
    def __call__(self, ingest, clip, *, workdir):
        from pipeline.observability.ledger import get_ledger
        workdir.mkdir(parents=True, exist_ok=True)
        out = workdir / f"{clip.clip_id}.mp4"
        out.write_bytes(b"fake-mp4-bytes")
        get_ledger().charge("crop_video", 0.005, source="test-stub")
        return CropResult(
            clip_id=clip.clip_id,
            cropped_video_path=str(out),
            output_width=1080, output_height=1920,
            quality_label="good", face_centered=True, layout="single",
        )


class NoFfmpegAddCaptions:
    def __call__(self, crop, words, *, clip_start_s: float, workdir):
        from pipeline.observability.ledger import get_ledger
        workdir.mkdir(parents=True, exist_ok=True)
        out = workdir / f"{crop.clip_id}.captioned.mp4"
        out.write_bytes(b"fake-mp4-bytes")
        get_ledger().charge("add_captions", 0.0, source="test-stub")
        return CaptionResult(
            clip_id=crop.clip_id,
            captioned_video_path=str(out),
            caption_segment_count=12,
            max_caption_drift_ms=40.0,
        )


# ---------------- the test ----------------

def test_orchestrator_runs_end_to_end():
    reset_store_for_tests()
    reset_registry_for_tests()

    # swap the I/O-bound stages on the orchestrator's local binding —
    # that is the binding the orchestrator actually calls.
    original_ingest = orch_mod.ingest_stage
    original_transcribe = orch_mod.transcribe_stage
    original_finalize = orch_mod.finalize_clip
    orch_mod.ingest_stage = fake_ingest
    orch_mod.transcribe_stage = fake_transcribe
    orch_mod.finalize_clip = fake_finalize

    install_default_stubs()
    register_tool("pick_timestamps", NoFfmpegPickTimestamps(), replace=True)
    register_tool("crop_video", NoFfmpegCropVideo(), replace=True)
    register_tool("add_captions", NoFfmpegAddCaptions(), replace=True)

    try:
        orch = Orchestrator()
        result = orch.run(url="https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    finally:
        orch_mod.ingest_stage = original_ingest
        orch_mod.transcribe_stage = original_transcribe
        orch_mod.finalize_clip = original_finalize

    # ----- assertions -----
    assert result.video_id == "testvid_abc", result.video_id
    assert len(result.clips) == 2, f"expected 2 clips, got {len(result.clips)}"
    for c in result.clips:
        assert c.qc_passed, f"clip {c.clip_id} failed qc"
        assert c.width == 1080 and c.height == 1920, f"resolution {c.width}x{c.height}"

    assert result.total_cost_usd > 0, "ledger should have charges"
    assert result.total_cost_usd < 1.00, f"cost ${result.total_cost_usd:.4f} > $1.00 ceiling"

    breakdown = result.cost_breakdown
    for expected_stage in ("ingest", "transcribe", "pick_timestamps", "crop_video",
                           "correct_captions", "add_captions", "finalize"):
        assert expected_stage in breakdown, f"missing cost entry: {expected_stage} (have {list(breakdown)})"

    # State store should have the job
    from pipeline.state import get_store
    state = get_store().load_job(result.job_id)
    assert state is not None, "job state missing from store"
    assert state.status.value == "done", state.status

    # Trace has spans. We expect: pipeline.run (root) + a tool.* span per
    # dispatched tool. The fake_* stage substitutes are plain functions and
    # don't emit their own spans — the @traced decorator on the real stages
    # produces those in production. We assert what the test setup actually
    # produces.
    spans = get_store().read_trace(result.job_id)
    span_names = {s.get("name") for s in spans}
    assert "pipeline.run" in span_names, f"no root span (have {span_names})"
    for expected in ("tool.pick_timestamps", "tool.crop_video", "tool.add_captions"):
        assert expected in span_names, f"missing {expected} (have {span_names})"


def main() -> int:
    try:
        test_orchestrator_runs_end_to_end()
    except BaseException as e:
        import traceback
        traceback.print_exc()
        print(f"\nFAIL: {type(e).__name__}: {e}")
        return 1
    print("\nPASS: orchestrator smoke test")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
