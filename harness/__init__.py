"""The Harness — the runtime scaffolding around the AI worker.

From the deck (4 pillars):

    Loop          — agents.claude_worker drives reasoning across turns
    Tools         — pipeline.tools.* (typed adapters, allow-listed registry)
    Guardrails    — harness.guardrails (input/action/output) + harness.limits
    Observability — harness.observability (OTel spans + cost ledger)

From the hackathon spec (4 components, must be distinct + identifiable):

    Guardrails        — declared, not implicit. See harness.guardrails.DECLARED.
    Checkpoints       — explicit pass/fail per stage. See harness.checkpoints.
    Material handling — typed I/O envelopes. See harness.material.
    Alarms            — structured (name, severity, context, recommended_action).
                        See harness.alarms.

The harness governs the worker. A worker (agents.Worker) is swappable behind a
single Protocol; the harness itself doesn't know which model is driving.
"""
from . import alarms, checkpoints, guardrails, limits, material, observability  # noqa: F401

__all__ = [
    "alarms",
    "checkpoints",
    "guardrails",
    "limits",
    "material",
    "observability",
]
