"""The orchestrator — state machine that walks the pipeline stages, runs QC
between each, persists state after each, and enforces hard limits + budget.

Maps to the harness deck's "Build context → Call model/tool → Run → Append →
Repeat/stop" loop (slide 4). Our "model" here is the tool 2 LLM call; the
rest are deterministic tools. The loop drives the same shape:

    for stage in plan:
        check hard limits (turn cap, wall-clock)
        try:
            output = run_stage(stage, prior_outputs)
            verdict = qc(output)
            if verdict.failed:
                retry or drop (per policy)
        except BudgetExceeded:
            mark job budget_exceeded, stop
        persist state

The orchestrator does NOT swallow exceptions silently. It either retries,
drops a clip, or marks the job failed — each path leaves a trace + ledger
record.
"""
from __future__ import annotations

import tempfile
import time
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from opentelemetry import trace

from .config import Config, load_config
from .guardrails.input import InputGuardrailFailed, validate_url
from .guardrails.limits import HardLimits, TurnCapExceeded, WallClockExceeded
from .guardrails.output import qc_clip, qc_run
from .observability.exporters import dump_run_trace
from .observability.ledger import BudgetExceeded, CostLedger, set_ledger, reset_ledger
from .observability.tracer import get_tracer, init_tracing
from .stages.correct_captions import correct_captions
from .stages.finalize import finalize_clip
from .stages.ingest import ingest as ingest_stage
from .stages.transcribe import transcribe as transcribe_stage
from .state import get_store
from .tools.dispatch import dispatch
from .tools.registry import install_default_stubs, list_tools
from .types import (
    FinalClip,
    JobState,
    JobStatus,
    PipelineInput,
    PipelineResult,
    StageRecord,
    StageStatus,
)


class Orchestrator:
    def __init__(self, config: Optional[Config] = None) -> None:
        self.config = config or load_config()
        init_tracing(service_name="acq-clipper")
        # Make sure something is registered. Real implementations override.
        if not list_tools():
            install_default_stubs()

    # ----------------------------------------------------------------

    def run(self, *, url: str, job_id: Optional[str] = None) -> PipelineResult:
        """Run the full pipeline for one URL. Returns a PipelineResult.

        Persists state continuously; if this function dies mid-run, the
        last-saved state is recoverable from Redis.
        """
        job_id = job_id or f"job_{uuid.uuid4().hex[:12]}"
        store = get_store()

        ledger = CostLedger(
            ceiling_usd=self.config.cost_ceiling_usd,
            target_usd=self.config.cost_target_usd,
        )
        set_ledger(ledger)
        limits = HardLimits.start(
            max_turns=self.config.max_turns,
            max_wall_seconds=self.config.max_wall_seconds,
            max_retries_per_stage=self.config.max_retries_per_stage,
        )

        # Validate URL up front (input guardrail) — this can raise before any state.
        try:
            canonical_url = validate_url(url)
        except InputGuardrailFailed as e:
            reset_ledger()
            raise

        state = JobState(job_id=job_id, url=canonical_url, status=JobStatus.RUNNING)
        store.save_job(state)

        tracer = get_tracer()
        try:
            with tracer.start_as_current_span("pipeline.run") as root:
                root.set_attribute("job_id", job_id)
                root.set_attribute("url", canonical_url)

                try:
                    with tempfile.TemporaryDirectory(prefix=f"acq_{job_id}_") as tmp:
                        workdir = Path(tmp)
                        result = self._run_inner(canonical_url, job_id, workdir, state, limits, ledger)
                    root.set_attribute("status", "ok")
                    root.set_attribute("cost_usd", ledger.total_usd)
                    return result

                except BudgetExceeded as e:
                    state.status = JobStatus.BUDGET_EXCEEDED
                    state.error = str(e)
                    store.save_job(state)
                    root.set_attribute("status", "budget_exceeded")
                    raise
                except (TurnCapExceeded, WallClockExceeded) as e:
                    state.status = JobStatus.TIMEOUT
                    state.error = str(e)
                    store.save_job(state)
                    root.set_attribute("status", "timeout")
                    raise
                except Exception as e:
                    state.status = JobStatus.FAILED
                    state.error = f"{type(e).__name__}: {e}\n{traceback.format_exc()[-1000:]}"
                    store.save_job(state)
                    root.set_attribute("status", "error")
                    root.set_attribute("error.class", type(e).__name__)
                    raise
        finally:
            # Run the dump AFTER the root span has closed so it's flushed
            # to the in-memory recorder. dump_run_trace persists the trace
            # + ledger to Redis keyed by job_id.
            dump_run_trace(job_id, ledger)
            reset_ledger()
            state.updated_at = datetime.now(timezone.utc)
            state.total_cost_usd = ledger.total_usd
            store.save_job(state)

    # ----------------------------------------------------------------

    def _run_inner(
        self,
        url: str,
        job_id: str,
        workdir: Path,
        state: JobState,
        limits: HardLimits,
        ledger: CostLedger,
    ) -> PipelineResult:
        store = get_store()
        turn = 0

        # ---- Stage: ingest ----
        limits.check(turn); turn += 1
        ingest_result = self._run_stage(
            state, "ingest",
            lambda: ingest_stage(url, workdir / "src", job_id=job_id),
        )
        store.save_job(state)

        # ---- Stage: transcribe ----
        limits.check(turn); turn += 1
        transcribe_result = self._run_stage(
            state, "transcribe",
            lambda: transcribe_stage(ingest_result, job_id=job_id),
        )
        if not transcribe_result.words:
            raise RuntimeError("transcribe returned no words — cannot continue")
        store.save_job(state)

        # ---- Stage: pick_timestamps (tool 2) ----
        limits.check(turn); turn += 1
        picked = self._run_stage(
            state, "pick_timestamps",
            lambda: dispatch(
                "pick_timestamps", transcribe_result, ingest_result,
                _span_attrs={"job_id": job_id, "video_id": ingest_result.video_id},
            ),
        )
        valid_clips = self._filter_invalid_clips(picked.clips, ingest_result.duration_s)
        if not valid_clips:
            raise RuntimeError("pick_timestamps produced no valid clip windows")
        store.save_job(state)

        # ---- Per-clip: crop -> caption -> finalize ----
        final_clips: list[FinalClip] = []
        for clip in valid_clips:
            limits.check(turn); turn += 1
            try:
                final = self._process_one_clip(
                    state=state,
                    clip=clip,
                    ingest_result=ingest_result,
                    transcribe_result=transcribe_result,
                    workdir=workdir / "clips" / clip.clip_id,
                    job_id=job_id,
                )
                final_clips.append(final)
            except BudgetExceeded:
                # Stop processing further clips — the run has spent its budget.
                raise
            except Exception as e:
                # Drop this clip, keep going for the others. The trace + ledger
                # already recorded the failure inside _run_stage.
                self._record_dropped_clip(state, clip.clip_id, e)
            store.save_job(state)

        # ---- Whole-run QC ----
        run_verdict = qc_run(
            final_clips,
            total_cost_usd=ledger.total_usd,
            ceiling_usd=ledger.ceiling_usd,
        )
        span = trace.get_current_span()
        span.set_attribute("run.qc.passed", run_verdict.passed)
        if run_verdict.failures:
            span.set_attribute("run.qc.failures", ",".join(run_verdict.failures))

        result = PipelineResult(
            job_id=job_id,
            video_id=ingest_result.video_id,
            clips=final_clips,
            total_cost_usd=ledger.total_usd,
            cost_breakdown=ledger.by_stage(),
            duration_ms=int(limits.elapsed_seconds() * 1000),
        )
        state.result = result
        state.status = JobStatus.DONE if run_verdict.passed else JobStatus.FAILED
        state.total_cost_usd = ledger.total_usd
        state.completed_at = datetime.now(timezone.utc)
        store.save_job(state)
        return result

    # ----------------------------------------------------------------

    def _process_one_clip(
        self,
        *,
        state: JobState,
        clip,
        ingest_result,
        transcribe_result,
        workdir: Path,
        job_id: str,
    ) -> FinalClip:
        # When the picker emitted kept_segments, concat them into a temp
        # source mp4 and rebase the clip + words to that local timeline.
        # Downstream stages then see a clean (start_s=0, end_s=concat_dur)
        # clip and don't need to know kept_segments exists.
        effective_clip = clip
        effective_ingest = ingest_result
        if clip.kept_segments and len(clip.kept_segments) > 1:
            concat_path = workdir / "concat" / f"{clip.clip_id}.concat.mp4"
            concat_duration = _concat_segments_with_ffmpeg(
                source_path=ingest_result.video_path,
                segments=[(float(a), float(b)) for a, b in clip.kept_segments],
                out_path=concat_path,
            )
            effective_clip = clip.model_copy(update={
                "start_s": 0.0,
                "end_s": concat_duration,
                "kept_segments": None,
            })
            effective_ingest = ingest_result.model_copy(update={
                "video_path": str(concat_path),
                "duration_s": concat_duration,
            })
            clip_words = _rebase_words_to_segments(
                transcribe_result.words, clip.kept_segments,
            )
        else:
            # Subset word timings to the clip window
            clip_words = [
                w for w in transcribe_result.words
                if w.end_s >= clip.start_s and w.start_s <= clip.end_s
            ]

        # Crop
        crop = self._run_stage(
            state, f"crop_video[{clip.clip_id}]",
            lambda: dispatch(
                "crop_video", effective_ingest, effective_clip,
                workdir=workdir / "crop",
                _span_attrs={"job_id": job_id, "clip_id": clip.clip_id},
            ),
        )

        # Inter-stage QC: refuse to caption a clip whose crop already failed
        # acceptance — caption + render cost would be wasted.
        if crop.output_width != 1080 or crop.output_height != 1920:
            raise RuntimeError(
                f"crop produced wrong resolution {crop.output_width}x{crop.output_height}"
            )

        # Caption-text correction (Claude Haiku 4.5). Falls back to the
        # original words on any failure so a flaky LLM never blocks the run.
        corrected_words = self._run_stage(
            state, f"correct_captions[{clip.clip_id}]",
            lambda: correct_captions(
                clip_words,
                clip_id=clip.clip_id,
                video_title=ingest_result.title,
            ),
        )

        # Captions — clip_start_s is from the effective (concat-local)
        # timeline so word timings stay aligned with the rendered video.
        caption = self._run_stage(
            state, f"add_captions[{clip.clip_id}]",
            lambda: dispatch(
                "add_captions", crop, corrected_words,
                clip_start_s=effective_clip.start_s,
                workdir=workdir / "caption",
                _span_attrs={"job_id": job_id, "clip_id": clip.clip_id},
            ),
        )

        # Finalize (upload + QC)
        final = self._run_stage(
            state, f"finalize[{clip.clip_id}]",
            lambda: finalize_clip(
                clip_timestamp=clip,
                crop=crop,
                caption=caption,
                job_id=job_id,
                video_id=ingest_result.video_id,
            ),
        )
        return final

    # ----------------------------------------------------------------

    def _run_stage(self, state: JobState, name: str, fn) -> any:
        """Run a stage with retry-on-error up to max_retries_per_stage.

        Records a StageRecord in JobState on every attempt. The retry loop
        catches generic Exception but lets BudgetExceeded and limit
        exceptions propagate — those are non-retriable by definition.
        """
        rec = state.stages.get(name) or StageRecord(name=name)
        state.stages[name] = rec
        rec.status = StageStatus.IN_PROGRESS
        rec.started_at = datetime.now(timezone.utc)

        last_exc: Optional[Exception] = None
        max_attempts = self.config.max_retries_per_stage + 1
        for attempt in range(1, max_attempts + 1):
            rec.attempts = attempt
            t0 = time.perf_counter()
            try:
                out = fn()
                rec.status = StageStatus.COMPLETED
                rec.completed_at = datetime.now(timezone.utc)
                rec.duration_ms = int((time.perf_counter() - t0) * 1000)
                try:
                    rec.output = out.model_dump() if hasattr(out, "model_dump") else None
                except Exception:
                    rec.output = None
                return out
            except (BudgetExceeded, TurnCapExceeded, WallClockExceeded):
                rec.status = StageStatus.FAILED
                rec.completed_at = datetime.now(timezone.utc)
                rec.duration_ms = int((time.perf_counter() - t0) * 1000)
                raise
            except Exception as e:
                last_exc = e
                rec.duration_ms = int((time.perf_counter() - t0) * 1000)
                rec.error = f"attempt {attempt}: {type(e).__name__}: {e}"
                if attempt >= max_attempts:
                    rec.status = StageStatus.FAILED
                    rec.completed_at = datetime.now(timezone.utc)
                    raise
                # else: loop and retry

        # Should be unreachable.
        if last_exc is not None:
            raise last_exc
        raise RuntimeError(f"stage {name} exited retry loop unexpectedly")

    # ----------------------------------------------------------------

    def _filter_invalid_clips(self, clips, source_duration_s: float):
        """Drop clips that violate the basic window contract before we waste
        crop+caption compute on them.

        When `kept_segments` is present (find_moments uses these to drop
        boring middle content), validate length against the sum of segment
        durations. Otherwise use the envelope.

        Length window: 15s minimum, 150s maximum. The picker targets ≤90s
        with a 150s hard cap; we trust its judgment and cap at the same.
        """
        MIN_S, MAX_S = 15.0, 150.0
        ok = []
        for c in clips:
            if c.start_s < 0 or c.end_s > source_duration_s + 0.5:
                continue
            if c.start_s >= c.end_s:
                continue
            if c.kept_segments:
                effective = sum(max(0.0, b - a) for a, b in c.kept_segments)
            else:
                effective = c.end_s - c.start_s
            if effective < MIN_S or effective > MAX_S:
                continue
            ok.append(c)
        return ok

    def _record_dropped_clip(self, state: JobState, clip_id: str, exc: Exception) -> None:
        key = f"dropped[{clip_id}]"
        state.stages[key] = StageRecord(
            name=key,
            status=StageStatus.FAILED,
            error=f"{type(exc).__name__}: {exc}",
            completed_at=datetime.now(timezone.utc),
        )


# ------------- helpers for kept_segments handling (module-level) -------------


def _concat_segments_with_ffmpeg(*, source_path, segments, out_path) -> float:
    """Extract `segments` from `source_path`, concat them into `out_path`.
    Returns the total duration of the resulting mp4 in seconds.

    Uses a single ffmpeg invocation with the concat filter — re-encodes once
    so we don't depend on keyframe alignment for clean cuts.
    """
    import subprocess
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if not segments:
        raise RuntimeError("concat called with empty segments")

    # Build the filter_complex graph.
    filter_parts: list[str] = []
    map_labels: list[str] = []
    for i, (a, b) in enumerate(segments):
        filter_parts.append(
            f"[0:v]trim=start={a:.3f}:end={b:.3f},setpts=PTS-STARTPTS[v{i}]"
        )
        filter_parts.append(
            f"[0:a]atrim=start={a:.3f}:end={b:.3f},asetpts=PTS-STARTPTS[a{i}]"
        )
        map_labels.append(f"[v{i}][a{i}]")
    filter_parts.append(
        f"{''.join(map_labels)}concat=n={len(segments)}:v=1:a=1[outv][outa]"
    )
    filter_complex = ";".join(filter_parts)

    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(source_path),
        "-filter_complex", filter_complex,
        "-map", "[outv]", "-map", "[outa]",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        str(out_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg concat failed: {proc.stderr.strip()[-300:]}")
    return sum(max(0.0, float(b) - float(a)) for a, b in segments)


def _rebase_words_to_segments(words, segments):
    """Filter `words` to those falling inside `segments` and remap their
    timings to the concat-local timeline (segments are stitched back-to-back
    starting at t=0).
    """
    out = []
    cumulative = 0.0
    for seg_start, seg_end in segments:
        seg_start = float(seg_start); seg_end = float(seg_end)
        seg_len = max(0.0, seg_end - seg_start)
        for w in words:
            if w.end_s <= seg_start or w.start_s >= seg_end:
                continue
            local_start = max(0.0, w.start_s - seg_start) + cumulative
            local_end = min(seg_len, w.end_s - seg_start) + cumulative
            out.append(w.model_copy(update={
                "start_s": local_start,
                "end_s": local_end,
            }))
        cumulative += seg_len
    return out


