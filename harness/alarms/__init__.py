"""Structured alarms — fired by guardrails, checkpoints, tools, or the loop.

Spec requirement (hackathon "Must"):
    "Alarms produce structured output — named alarm types with context,
     severity, and a recommended action."

Distinct from exceptions: an Exception unwinds the stack and kills the call.
An Alarm is **data** that flows back to the worker, which can read its
recommended_action and decide. Exceptions remain for fatal/non-recoverable
states (e.g., BudgetExceeded, TurnCapExceeded).

Implementation lives in `types.py` and `taxonomy.py`. See:
    harness.alarms.Alarm                  — the dataclass
    harness.alarms.Severity               — info/warning/error/critical
    harness.alarms.RecommendedAction      — string enum the worker reads
    harness.alarms.TAXONOMY               — registered alarm names
"""
from .types import (  # noqa: F401
    Alarm,
    AlarmBus,
    RecommendedAction,
    Severity,
    get_bus,
    reset_bus,
    set_bus,
)
from .taxonomy import TAXONOMY, AlarmSpec  # noqa: F401
from .translation import register_exception, translate  # noqa: F401

__all__ = [
    "Alarm",
    "AlarmBus",
    "AlarmSpec",
    "RecommendedAction",
    "Severity",
    "TAXONOMY",
    "get_bus",
    "register_exception",
    "reset_bus",
    "set_bus",
    "translate",
]
