"""Checkpoint contract types.

A checkpoint is a function that takes a stage output and returns a
CheckpointVerdict. Implementations live in stage-specific modules
(checkpoint_ingest.py, etc.) — this file owns the shape.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class CriterionResult(BaseModel):
    """One pass/fail check inside a checkpoint."""
    name: str           # e.g. "duration_in_window"
    passed: bool
    actual: Any = None  # observed value (numeric, string, bool)
    expected: Any = None  # what we wanted (range, label, etc.)
    detail: str | None = None


class Criterion(BaseModel):
    """A criterion specification — what we will check.

    Used by HARNESS.md generation and by tests that want to inspect what a
    given checkpoint checks for, without running it.
    """
    name: str
    description: str


class CheckpointVerdict(BaseModel):
    """Result of running a checkpoint.

    `passed` is the AND of every criterion.
    `alarm_names` lists alarms fired during this checkpoint — written to disk
        and read back during replay so the worker sees the same signals.
    """
    stage: str
    passed: bool
    criteria: list[CriterionResult] = Field(default_factory=list)
    metrics: dict[str, float] = Field(default_factory=dict)
    alarm_names: list[str] = Field(default_factory=list)
    # Optional summary for the worker's prompt. Short.
    summary: str | None = None

    @classmethod
    def from_criteria(
        cls,
        *,
        stage: str,
        results: list[CriterionResult],
        metrics: dict[str, float] | None = None,
        alarm_names: list[str] | None = None,
        summary: str | None = None,
    ) -> "CheckpointVerdict":
        return cls(
            stage=stage,
            passed=all(r.passed for r in results),
            criteria=results,
            metrics=metrics or {},
            alarm_names=alarm_names or [],
            summary=summary,
        )

    def failed_criteria(self) -> list[CriterionResult]:
        return [c for c in self.criteria if not c.passed]
