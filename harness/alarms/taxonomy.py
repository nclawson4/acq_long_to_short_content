"""Alarm taxonomy — the named alarms this harness can fire.

Registered up-front so:
    1. Judges can read the alarm taxonomy in one place (this file).
    2. HARNESS.md can table-render the alarm catalog from TAXONOMY.
    3. Tests can assert that fired alarms use known names (no typos).

Adding a new alarm: append an AlarmSpec to TAXONOMY. The actual firing happens
in checkpoints, guardrails, stages, or the worker loop.
"""
from __future__ import annotations

from dataclasses import dataclass

from .types import RecommendedAction, Severity


@dataclass(frozen=True)
class AlarmSpec:
    name: str
    severity: Severity
    fired_by: str             # which component fires it ("checkpoint:crop", "guardrail:input", "loop")
    default_action: RecommendedAction
    description: str


TAXONOMY: tuple[AlarmSpec, ...] = (
    # ---------- guardrail-fired ----------
    AlarmSpec(
        name="UrlRejected",
        severity=Severity.ERROR,
        fired_by="guardrail:input",
        default_action=RecommendedAction.ABORT_RUN,
        description="URL failed input guardrail (host not allowed, playlist, "
                    "livestream, missing video id).",
    ),
    AlarmSpec(
        name="TranscriptInjectionStripped",
        severity=Severity.WARNING,
        fired_by="guardrail:input",
        default_action=RecommendedAction.CONTINUE,
        description="Prompt-injection markers were found and replaced in the "
                    "transcript before any LLM call.",
    ),
    AlarmSpec(
        name="UnknownToolCalled",
        severity=Severity.ERROR,
        fired_by="guardrail:action",
        default_action=RecommendedAction.RETRY_WITH_ADJUSTED_ARGS,
        description="Worker tried to call a tool not on the allow-list.",
    ),
    # ---------- checkpoint-fired ----------
    AlarmSpec(
        name="IngestNoVideo",
        severity=Severity.CRITICAL,
        fired_by="checkpoint:ingest",
        default_action=RecommendedAction.ABORT_RUN,
        description="yt-dlp did not produce a usable video file.",
    ),
    AlarmSpec(
        name="TranscriptLowConfidence",
        severity=Severity.WARNING,
        fired_by="checkpoint:transcribe",
        default_action=RecommendedAction.CONTINUE,
        description="Average word confidence below threshold. Captions may "
                    "still ship; correct_captions stage may help.",
    ),
    AlarmSpec(
        name="TranscriptMalformed",
        severity=Severity.CRITICAL,
        fired_by="checkpoint:transcribe",
        default_action=RecommendedAction.ABORT_RUN,
        description="Transcript has no words or no speaker labels — cannot "
                    "pick a moment.",
    ),
    AlarmSpec(
        name="NoScoreableMoments",
        severity=Severity.ERROR,
        fired_by="checkpoint:pick",
        default_action=RecommendedAction.RETRY_WITH_ADJUSTED_ARGS,
        description="Picker found no viable Q&A clip. Worker may retry with "
                    "a different window or relaxed thresholds.",
    ),
    AlarmSpec(
        name="ClipTooLong",
        severity=Severity.ERROR,
        fired_by="checkpoint:pick",
        default_action=RecommendedAction.RETRY_WITH_ADJUSTED_ARGS,
        description="Picked clip exceeds MAX_DURATION_S. Worker should "
                    "request a tighter window or kept_segments split.",
    ),
    AlarmSpec(
        name="NoFacesDetected",
        severity=Severity.ERROR,
        fired_by="checkpoint:crop",
        default_action=RecommendedAction.DROP_CLIP,
        description="Crop tool found no usable faces in the source window. "
                    "Drop the clip; do not ship a black frame.",
    ),
    AlarmSpec(
        name="CropQualityPoor",
        severity=Severity.WARNING,
        fired_by="checkpoint:crop",
        default_action=RecommendedAction.CONTINUE,
        description="Crop produced 'soft' or 'poor' quality. Continue but "
                    "flag in manifest.",
    ),
    AlarmSpec(
        name="CaptionDriftHigh",
        severity=Severity.WARNING,
        fired_by="checkpoint:captions",
        default_action=RecommendedAction.RETRY_STAGE,
        description="Caption-to-audio drift exceeds tolerance. Re-run "
                    "correct_captions or fall back to raw transcript.",
    ),
    AlarmSpec(
        name="ResolutionMismatch",
        severity=Severity.CRITICAL,
        fired_by="checkpoint:finalize",
        default_action=RecommendedAction.DROP_CLIP,
        description="Final clip not 1080x1920. Must not ship.",
    ),
    AlarmSpec(
        name="DurationOutOfRange",
        severity=Severity.ERROR,
        fired_by="checkpoint:finalize",
        default_action=RecommendedAction.DROP_CLIP,
        description="Final clip outside [MIN_DURATION_S, MAX_DURATION_S].",
    ),
    AlarmSpec(
        name="LoudnessOutOfSpec",
        severity=Severity.WARNING,
        fired_by="checkpoint:finalize",
        default_action=RecommendedAction.CONTINUE,
        description="Loudness > 3 LU off the -16 LUFS target. Visible in "
                    "manifest; would normally re-encode but ship for now.",
    ),
    # ---------- limit-fired ----------
    AlarmSpec(
        name="BudgetExceeded",
        severity=Severity.CRITICAL,
        fired_by="limit:ledger",
        default_action=RecommendedAction.ABORT_RUN,
        description="A charge would push spend past the ceiling. Run halts.",
    ),
    AlarmSpec(
        name="TurnCapApproaching",
        severity=Severity.WARNING,
        fired_by="loop",
        default_action=RecommendedAction.CONTINUE,
        description="Worker has consumed >80% of MAX_TURNS. Wrap up.",
    ),
    AlarmSpec(
        name="TurnCapExceeded",
        severity=Severity.CRITICAL,
        fired_by="loop",
        default_action=RecommendedAction.ABORT_RUN,
        description="Worker exceeded MAX_TURNS. Possible infinite-spin.",
    ),
    AlarmSpec(
        name="WallClockExceeded",
        severity=Severity.CRITICAL,
        fired_by="loop",
        default_action=RecommendedAction.ABORT_RUN,
        description="Total run time exceeded wall-clock limit.",
    ),
    # ---------- escalation ----------
    AlarmSpec(
        name="HumanReviewRequested",
        severity=Severity.INFO,
        fired_by="worker",
        default_action=RecommendedAction.ESCALATE_HUMAN,
        description="Worker is uncertain and asks for human input. Harness "
                    "writes awaiting_human.json and pauses.",
    ),
)


def by_name(name: str) -> AlarmSpec | None:
    for spec in TAXONOMY:
        if spec.name == name:
            return spec
    return None
