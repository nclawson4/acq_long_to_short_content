"""Per-job trace + ledger dump to the state store.

This is what powers replay. After a run, the operator (or the dashboard)
reads `state_store.read_trace(job_id)` to see every span with its cost +
duration + attributes, and `state_store.read_ledger(job_id)` to see the
cost breakdown.

Kept separate from tracer.py so observability backend choice (OTel collector,
Langfuse, file-only) is orthogonal to the replayable per-job record.
"""
from __future__ import annotations

from typing import Any

from ..state import get_store
from .ledger import CostLedger
from .tracer import get_in_memory_spans


def dump_run_trace(job_id: str, ledger: CostLedger) -> dict[str, Any]:
    """Persist the run's spans + cost ledger to Redis keyed by job_id.

    Returns the summary that gets attached to the pipeline result so the
    frontend can show cost/latency/qc at a glance without an extra round
    trip.
    """
    store = get_store()
    spans = get_in_memory_spans()

    # Trim to the spans for THIS job. We tag every stage span with
    # `job_id` so warm-instance cross-job pollution is detectable.
    job_spans = [s for s in spans if s.get("attributes", {}).get("job_id") == job_id]

    for s in job_spans:
        store.append_span(job_id, s)
    for e in ledger.entries():
        store.append_cost(job_id, e)

    return {
        "span_count": len(job_spans),
        "total_usd": ledger.total_usd,
        "ceiling_usd": ledger.ceiling_usd,
        "target_usd": ledger.target_usd,
        "by_stage_usd": ledger.by_stage(),
        "over_target": ledger.over_target,
    }
