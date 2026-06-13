"""Exception -> Alarm translation.

The deterministic pipeline raises typed exceptions. The agent worker doesn't
catch those — it consumes alarms. This module bridges the two.

Used by `agents.tool_wrapper.invoke_tool`: every tool call is wrapped, and
if the call raises one of the known exception types, the wrapper fires the
matching alarm on the bus and returns ToolCallResult(ok=False, ...) so the
worker keeps running.

If an exception type isn't mapped, we still fire a generic UnknownTool/
UnexpectedFailure alarm so nothing fails silently — but the worker can tell
the difference between expected-failure (mapped) and bug (unmapped).
"""
from __future__ import annotations

from typing import Optional, Type

from .types import Alarm, RecommendedAction, Severity


# ---------- registry: exception class name -> alarm template ----------
# Indexed by class NAME (not type) so we can map exceptions from packages
# that may not be importable in every context (e.g. find_moments package).
_EXC_NAME_TO_ALARM: dict[str, tuple[str, Severity, RecommendedAction]] = {
    # ingest
    "IngestFailed":         ("IngestNoVideo",           Severity.CRITICAL, RecommendedAction.ABORT_RUN),
    # transcribe
    "TranscribeFailed":     ("TranscriptMalformed",     Severity.CRITICAL, RecommendedAction.ABORT_RUN),
    # picker (from processing.find_moments.errors)
    "TranscriptMalformed":  ("TranscriptMalformed",     Severity.CRITICAL, RecommendedAction.ABORT_RUN),
    "NoScoreableMoments":   ("NoScoreableMoments",      Severity.ERROR,    RecommendedAction.RETRY_WITH_ADJUSTED_ARGS),
    # crop (raised inside processing.acq_clipper via adapter)
    "NoFacesDetected":      ("NoFacesDetected",         Severity.ERROR,    RecommendedAction.DROP_CLIP),
    # captions (raised inside processing.yellow_captions via adapter)
    "CaptionRenderFailed":  ("CaptionDriftHigh",        Severity.WARNING,  RecommendedAction.RETRY_STAGE),
    # finalize
    "FinalizeFailed":       ("ResolutionMismatch",      Severity.CRITICAL, RecommendedAction.DROP_CLIP),
    # input
    "InputGuardrailFailed": ("UrlRejected",             Severity.ERROR,    RecommendedAction.ABORT_RUN),
    # limits
    "TurnCapExceeded":      ("TurnCapExceeded",         Severity.CRITICAL, RecommendedAction.ABORT_RUN),
    "WallClockExceeded":    ("WallClockExceeded",       Severity.CRITICAL, RecommendedAction.ABORT_RUN),
    # ledger
    "BudgetExceeded":       ("BudgetExceeded",          Severity.CRITICAL, RecommendedAction.ABORT_RUN),
}


def translate(
    exc: BaseException,
    *,
    stage: str,
    clip_id: Optional[str] = None,
    extra_context: Optional[dict] = None,
) -> Alarm:
    """Return the matching Alarm for an exception.

    Unknown exception types map to a generic UnexpectedFailure alarm with
    severity=ERROR so the worker can decide whether to retry.
    """
    cls_name = type(exc).__name__
    mapping = _EXC_NAME_TO_ALARM.get(cls_name)
    if mapping is None:
        return Alarm(
            name="UnexpectedFailure",
            severity=Severity.ERROR,
            context={
                "exception_class": cls_name,
                "stage": stage,
                **(extra_context or {}),
            },
            recommended_action=RecommendedAction.RETRY_STAGE,
            stage=stage,
            clip_id=clip_id,
            message=str(exc)[:300],
        )
    name, severity, action = mapping
    context = {"exception_class": cls_name, **(extra_context or {})}
    # Pull structured fields off BudgetExceeded for richer context.
    if cls_name == "BudgetExceeded":
        for k in ("stage", "attempted_usd", "total_usd", "ceiling"):
            v = getattr(exc, k, None)
            if v is not None:
                context[k] = v
    return Alarm(
        name=name,
        severity=severity,
        context=context,
        recommended_action=action,
        stage=stage,
        clip_id=clip_id,
        message=str(exc)[:300],
    )


def register_exception(
    exc_class_name: str,
    *,
    alarm_name: str,
    severity: Severity,
    action: RecommendedAction,
) -> None:
    """Extend the translation table at runtime — used by adapter tests."""
    _EXC_NAME_TO_ALARM[exc_class_name] = (alarm_name, severity, action)
