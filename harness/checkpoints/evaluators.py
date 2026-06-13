"""One checkpoint per stage. Each is a pure function:

    (stage_output, **context) -> CheckpointVerdict

Never raises. Failures show up as Criterion(passed=False) + alarm names in
the returned verdict. The tool wrapper fires the matching alarms and
persists the verdict.

These check that the harness's contract held — they do NOT re-do the
tool's work. e.g. checkpoint_crop trusts the crop tool's self-reported
quality_label, it does not re-run face detection. That keeps checkpoint
cost negligible and the truth-source attribution clean.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from harness.guardrails import (
    MAX_CAPTION_DRIFT_MS,
    MAX_DURATION_S,
    MIN_DURATION_S,
    TARGET_HEIGHT,
    TARGET_WIDTH,
    qc_clip,
)
from pipeline.types import (
    CaptionResult,
    CropResult,
    FinalClip,
    IngestResult,
    PickTimestampsResult,
    TranscribeResult,
)

from .types import CheckpointVerdict, CriterionResult


# ---------- stage 1: ingest ----------

def checkpoint_ingest(result: IngestResult) -> CheckpointVerdict:
    crits: list[CriterionResult] = []
    alarms: list[str] = []

    crits.append(CriterionResult(
        name="video_path_exists",
        passed=bool(result.video_path) and Path(result.video_path).exists(),
        actual=result.video_path,
        expected="file on disk",
    ))
    if not crits[-1].passed:
        alarms.append("IngestNoVideo")

    crits.append(CriterionResult(
        name="duration_positive",
        passed=result.duration_s > 0,
        actual=result.duration_s,
        expected="> 0",
    ))
    crits.append(CriterionResult(
        name="dimensions_positive",
        passed=result.width > 0 and result.height > 0,
        actual=f"{result.width}x{result.height}",
        expected="> 0",
    ))
    crits.append(CriterionResult(
        name="video_id_set",
        passed=bool(result.video_id),
        actual=result.video_id,
        expected="non-empty",
    ))

    return CheckpointVerdict.from_criteria(
        stage="ingest",
        results=crits,
        metrics={
            "duration_s": float(result.duration_s),
            "width": float(result.width),
            "height": float(result.height),
        },
        alarm_names=alarms,
        summary=f"ingest {'OK' if all(c.passed for c in crits) else 'FAILED'}: "
                f"{result.duration_s:.0f}s, {result.width}x{result.height}",
    )


# ---------- stage 2: transcribe ----------

MIN_WORDS = 20            # less than 20 words for an 8-min interview = broken
MIN_AVG_CONFIDENCE = 0.55  # below this is probably wrong language or empty audio
MIN_LANG_CONF_FOR_NO_WARN = 0.85


def checkpoint_transcribe(
    result: TranscribeResult,
    *,
    expected_language: str = "en",
) -> CheckpointVerdict:
    crits: list[CriterionResult] = []
    alarms: list[str] = []

    has_words = bool(result.words)
    crits.append(CriterionResult(
        name="has_words",
        passed=has_words,
        actual=len(result.words),
        expected=f">= {MIN_WORDS}",
    ))
    if not has_words:
        alarms.append("TranscriptMalformed")

    enough = len(result.words) >= MIN_WORDS
    crits.append(CriterionResult(
        name="enough_words",
        passed=enough,
        actual=len(result.words),
        expected=f">= {MIN_WORDS}",
    ))

    has_speakers = any(w.speaker is not None for w in result.words[:200])
    crits.append(CriterionResult(
        name="speaker_labels_present",
        passed=has_speakers,
        actual=has_speakers,
        expected=True,
        detail=("Deepgram diarization required for the picker to split "
                "paragraphs by speaker."),
    ))
    if not has_speakers:
        alarms.append("TranscriptMalformed")

    avg_conf = (
        sum(w.confidence for w in result.words) / len(result.words)
        if result.words else 0.0
    )
    high_conf = avg_conf >= MIN_AVG_CONFIDENCE
    crits.append(CriterionResult(
        name="avg_confidence_acceptable",
        passed=high_conf,
        actual=round(avg_conf, 3),
        expected=f">= {MIN_AVG_CONFIDENCE}",
    ))
    if has_words and not high_conf:
        alarms.append("TranscriptLowConfidence")

    crits.append(CriterionResult(
        name="language_matches",
        passed=(result.language or "").lower().startswith(expected_language),
        actual=result.language,
        expected=expected_language,
    ))

    return CheckpointVerdict.from_criteria(
        stage="transcribe",
        results=crits,
        metrics={"word_count": float(len(result.words)), "avg_confidence": avg_conf},
        alarm_names=alarms,
        summary=f"transcript {len(result.words)} words, avg_conf={avg_conf:.2f}, "
                f"lang={result.language}",
    )


# ---------- stage 3: pick ----------

def checkpoint_pick(
    result: PickTimestampsResult,
    *,
    source_duration_s: Optional[float] = None,
) -> CheckpointVerdict:
    crits: list[CriterionResult] = []
    alarms: list[str] = []

    has_clip = bool(result.clips)
    crits.append(CriterionResult(
        name="at_least_one_clip",
        passed=has_clip,
        actual=len(result.clips),
        expected=">= 1",
    ))
    if not has_clip:
        alarms.append("NoScoreableMoments")
        return CheckpointVerdict.from_criteria(
            stage="pick_timestamps",
            results=crits,
            alarm_names=alarms,
            summary="picker returned no clips",
        )

    clip = result.clips[0]
    # Use kept_segments duration sum when present (picker may have dropped
    # middle content); otherwise use envelope duration.
    if clip.kept_segments:
        dur = sum(max(0.0, b - a) for a, b in clip.kept_segments)
    else:
        dur = clip.end_s - clip.start_s

    crits.append(CriterionResult(
        name="duration_in_window",
        passed=MIN_DURATION_S <= dur <= MAX_DURATION_S,
        actual=round(dur, 2),
        expected=f"[{MIN_DURATION_S:.0f}, {MAX_DURATION_S:.0f}]",
    ))
    if dur > MAX_DURATION_S:
        alarms.append("ClipTooLong")

    crits.append(CriterionResult(
        name="positive_duration",
        passed=dur > 0,
        actual=round(dur, 2),
        expected="> 0",
    ))

    crits.append(CriterionResult(
        name="within_source",
        passed=clip.start_s >= 0 and (
            source_duration_s is None or clip.end_s <= source_duration_s + 0.5
        ),
        actual=f"{clip.start_s:.1f}->{clip.end_s:.1f}",
        expected=f"in [0, {source_duration_s}]" if source_duration_s else "non-negative",
    ))

    crits.append(CriterionResult(
        name="text_present",
        passed=bool(clip.text and clip.text.strip()),
        actual=len(clip.text or ""),
        expected="> 0",
    ))

    return CheckpointVerdict.from_criteria(
        stage="pick_timestamps",
        results=crits,
        metrics={
            "n_clips": float(len(result.clips)),
            "duration_s": dur,
            "score": float(clip.score) if clip.score is not None else 0.0,
        },
        alarm_names=alarms,
        summary=f"picked 1 clip, dur={dur:.1f}s, score={clip.score}",
    )


# ---------- stage 4: crop ----------

ACCEPTABLE_QUALITY = {"good", "acceptable", "soft"}  # "poor" still fails — that's a real-detection no-faces situation.


def checkpoint_crop(result: CropResult) -> CheckpointVerdict:
    crits: list[CriterionResult] = []
    alarms: list[str] = []

    crits.append(CriterionResult(
        name="output_file_exists",
        passed=bool(result.cropped_video_path) and Path(result.cropped_video_path).exists(),
        actual=result.cropped_video_path,
        expected="file on disk",
    ))

    crits.append(CriterionResult(
        name="dimensions_9_16",
        passed=result.output_width == TARGET_WIDTH and result.output_height == TARGET_HEIGHT,
        actual=f"{result.output_width}x{result.output_height}",
        expected=f"{TARGET_WIDTH}x{TARGET_HEIGHT}",
    ))
    if not crits[-1].passed:
        alarms.append("ResolutionMismatch")

    crits.append(CriterionResult(
        name="face_centered",
        passed=bool(result.face_centered),
        actual=result.face_centered,
        expected=True,
        detail="Tool's self-reported face-centering result.",
    ))
    if not result.face_centered:
        alarms.append("NoFacesDetected")

    crits.append(CriterionResult(
        name="quality_acceptable",
        passed=result.quality_label in ACCEPTABLE_QUALITY,
        actual=result.quality_label,
        expected=f"in {sorted(ACCEPTABLE_QUALITY)}",
    ))
    if result.quality_label not in ACCEPTABLE_QUALITY:
        alarms.append("CropQualityPoor")

    return CheckpointVerdict.from_criteria(
        stage="crop_video",
        results=crits,
        metrics={
            "width": float(result.output_width),
            "height": float(result.output_height),
        },
        alarm_names=alarms,
        summary=f"crop {result.output_width}x{result.output_height} "
                f"quality={result.quality_label} centered={result.face_centered}",
    )


# ---------- stage 5: captions ----------

def checkpoint_captions(
    result: CaptionResult,
    *,
    expected_segment_count_min: int = 1,
) -> CheckpointVerdict:
    crits: list[CriterionResult] = []
    alarms: list[str] = []

    crits.append(CriterionResult(
        name="output_file_exists",
        passed=bool(result.captioned_video_path) and Path(result.captioned_video_path).exists(),
        actual=result.captioned_video_path,
        expected="file on disk",
    ))

    crits.append(CriterionResult(
        name="segments_present",
        passed=result.caption_segment_count >= expected_segment_count_min,
        actual=result.caption_segment_count,
        expected=f">= {expected_segment_count_min}",
    ))

    drift = result.max_caption_drift_ms
    drift_ok = drift is None or drift <= MAX_CAPTION_DRIFT_MS
    crits.append(CriterionResult(
        name="drift_within_tolerance",
        passed=drift_ok,
        actual=drift,
        expected=f"<= {MAX_CAPTION_DRIFT_MS:.0f}ms",
    ))
    if not drift_ok:
        alarms.append("CaptionDriftHigh")

    return CheckpointVerdict.from_criteria(
        stage="add_captions",
        results=crits,
        metrics={
            "segments": float(result.caption_segment_count),
            "max_drift_ms": float(drift or 0.0),
        },
        alarm_names=alarms,
        summary=f"captions {result.caption_segment_count} segs, "
                f"drift={drift}ms",
    )


# ---------- stage 6: finalize ----------

def checkpoint_finalize(
    result: FinalClip,
    *,
    expected_duration_s: Optional[float] = None,
    local_path: Optional[str] = None,
) -> CheckpointVerdict:
    """The finalize checkpoint reaches into ffprobe via qc_clip for the most
    authoritative answer — by this point we have a finished mp4 and should
    trust the file, not the upstream tool's self-report."""
    crits: list[CriterionResult] = []
    alarms: list[str] = []

    crits.append(CriterionResult(
        name="blob_uploaded",
        passed=bool(result.blob_url and result.blob_url.startswith("http")),
        actual=result.blob_url,
        expected="https URL",
    ))

    crits.append(CriterionResult(
        name="reported_dimensions_9_16",
        passed=result.width == TARGET_WIDTH and result.height == TARGET_HEIGHT,
        actual=f"{result.width}x{result.height}",
        expected=f"{TARGET_WIDTH}x{TARGET_HEIGHT}",
    ))
    if not crits[-1].passed:
        alarms.append("ResolutionMismatch")

    in_window = MIN_DURATION_S <= result.duration_s <= MAX_DURATION_S
    crits.append(CriterionResult(
        name="duration_in_window",
        passed=in_window,
        actual=round(result.duration_s, 2),
        expected=f"[{MIN_DURATION_S:.0f}, {MAX_DURATION_S:.0f}]",
    ))
    if not in_window:
        alarms.append("DurationOutOfRange")

    crits.append(CriterionResult(
        name="qc_passed",
        passed=bool(result.qc_passed),
        actual=result.qc_passed,
        expected=True,
        detail=(", ".join(result.qc_failures) if result.qc_failures else None),
    ))

    metrics: dict[str, float] = {
        "duration_s": float(result.duration_s),
        "size_bytes": float(result.size_bytes),
        "width": float(result.width),
        "height": float(result.height),
    }

    # If we have the local file, run qc_clip too (more authoritative).
    if local_path and Path(local_path).exists():
        verdict = qc_clip(
            video_path=local_path,
            expected_duration_s=expected_duration_s,
        )
        metrics.update(verdict.metrics)
        for f in verdict.failures:
            crits.append(CriterionResult(name=f"ffprobe:{f}", passed=False, actual=f))
        if not verdict.passed:
            for f in verdict.failures:
                if f.startswith("loudness"):
                    alarms.append("LoudnessOutOfSpec")
                if f.startswith("too_long") or f.startswith("too_short"):
                    alarms.append("DurationOutOfRange")
                if f.startswith("resolution_mismatch"):
                    alarms.append("ResolutionMismatch")
                if f.startswith("caption_drift"):
                    alarms.append("CaptionDriftHigh")

    # Dedupe alarms (multiple ffprobe failures can map to the same one)
    alarms = list(dict.fromkeys(alarms))

    return CheckpointVerdict.from_criteria(
        stage="finalize",
        results=crits,
        metrics=metrics,
        alarm_names=alarms,
        summary=f"final {result.width}x{result.height} {result.duration_s:.1f}s "
                f"qc={'pass' if result.qc_passed else 'fail'}",
    )
