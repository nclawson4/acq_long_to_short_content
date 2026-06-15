"""Alarm dataclasses + per-run bus.

Hackathon "Must": alarms are structured — named, severity, context,
recommended_action. This file owns that contract.

Usage from anywhere in the harness:

    from harness.alarms import Alarm, Severity, RecommendedAction, get_bus
    get_bus().fire(Alarm(
        name="NoFacesDetected",
        severity=Severity.ERROR,
        context={"video_id": vid, "frames_sampled": 30},
        recommended_action=RecommendedAction.DROP_CLIP,
        stage="crop_video",
    ))

The bus is per-run (set by the harness loop at start, cleared at end).
Components that fire alarms don't know whether anyone is listening — the
worker reads them off the bus between turns.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class Severity(str, Enum):
    """Standard four-level severity. Persisted as the string value."""
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class RecommendedAction(str, Enum):
    """A closed set of actions the worker can take in response to an alarm.

    Closed by design: an open-ended `recommended_action` string lets agents
    invent unsafe actions. Keeping the set small makes the worker's behavior
    auditable.
    """
    CONTINUE = "continue"                 # No-op; alarm is informational
    RETRY_STAGE = "retry_stage"           # Re-run the producing stage
    RETRY_WITH_ADJUSTED_ARGS = "retry_with_adjusted_args"  # Loop hint
    DROP_CLIP = "drop_clip"               # Abandon this clip, continue run
    ESCALATE_HUMAN = "escalate_human"     # Pause; write awaiting_human.json
    ABORT_RUN = "abort_run"               # Stop the whole run


@dataclass
class Alarm:
    name: str                                # e.g. "NoFacesDetected"
    severity: Severity
    context: dict[str, Any]
    recommended_action: RecommendedAction
    stage: Optional[str] = None              # which stage emitted it
    clip_id: Optional[str] = None            # if scoped to one clip
    fired_at: float = field(default_factory=time.time)
    message: Optional[str] = None            # human-readable one-liner

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "severity": self.severity.value,
            "context": dict(self.context),
            "recommended_action": self.recommended_action.value,
            "stage": self.stage,
            "clip_id": self.clip_id,
            "fired_at": self.fired_at,
            "message": self.message,
        }


class AlarmBus:
    """Per-run alarm collector. Thread-safe."""

    def __init__(self) -> None:
        self._alarms: list[Alarm] = []
        self._lock = threading.Lock()
        self._cursor = 0  # for drain() — next index to return

    def fire(self, alarm: Alarm) -> Alarm:
        with self._lock:
            self._alarms.append(alarm)
        # Push high-severity alarms to the external incident webhook (no-op
        # if ACQ_ALARM_WEBHOOK_URL is unset). Best-effort, non-blocking — a
        # webhook failure must never break the pipeline.
        try:
            from .notifier import notify_async
            notify_async(alarm)
        except Exception:
            pass
        return alarm

    def all(self) -> list[Alarm]:
        with self._lock:
            return list(self._alarms)

    def by_severity(self, severity: Severity) -> list[Alarm]:
        with self._lock:
            return [a for a in self._alarms if a.severity == severity]

    def drain(self) -> list[Alarm]:
        """Return alarms fired since the last drain. Worker calls this between
        turns to read what new things happened during the last tool call."""
        with self._lock:
            new = self._alarms[self._cursor:]
            self._cursor = len(self._alarms)
            return new

    def to_list(self) -> list[dict[str, Any]]:
        with self._lock:
            return [a.to_dict() for a in self._alarms]

    def clear(self) -> None:
        with self._lock:
            self._alarms.clear()
            self._cursor = 0


_current: Optional[AlarmBus] = None


def get_bus() -> AlarmBus:
    """Per-process current bus. Auto-creates a fresh one if missing — useful
    for tests + direct stage calls. The harness loop sets a real bus at the
    start of each run."""
    global _current
    if _current is None:
        _current = AlarmBus()
    return _current


def set_bus(bus: AlarmBus) -> None:
    global _current
    _current = bus


def reset_bus() -> None:
    global _current
    _current = None
