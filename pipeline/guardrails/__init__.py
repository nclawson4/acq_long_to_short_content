"""Guardrails — layered checks on the way in, the way out, and around the loop.

From the harness deck (pillar 3): the model will eventually do the wrong thing,
so design so it can't do damage. We enforce that with three layers + hard
limits:

    input   — URL allow-list, transcript injection strip
    action  — tool registry (allow-list) lives under pipeline.tools.registry
    output  — QC gate per clip and at the end of the run
    limits  — spend ceiling (ledger), turn cap, wall-clock timeout

The spend ceiling is in observability.ledger because it's coupled to the cost
ledger, not because guardrails don't own it.
"""
from .input import validate_url, sanitize_transcript_text, InputGuardrailFailed  # noqa: F401
from .output import qc_clip, qc_run, QCFailed  # noqa: F401
from .limits import HardLimits, TurnCapExceeded, WallClockExceeded  # noqa: F401
