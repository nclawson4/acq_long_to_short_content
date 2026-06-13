"""Observability — OTel spans + cost ledger + JSON trace dump.

The four signals from the harness deck mapped to this build:

    p95 latency  -> per-stage span.duration_ms
    $/run        -> ledger.total_usd, ledger.by_stage
    err%         -> count(spans where status='error') / count(spans)
    eval         -> QC verdict pass rate (verdicts emitted as span attributes)

Span attribute conventions (compatible with OpenLLMetry / Langfuse semantics):
    stage           — the pipeline stage name
    video_id        — YouTube id
    clip_id         — per-clip operations
    cost_usd        — dollars attributed to this span (model+tool cost)
    tokens_in/out   — LLM only
    qc.passed       — output guardrail verdict
    qc.failures     — list[str]
"""
from .tracer import get_tracer, init_tracing, traced  # noqa: F401
from .ledger import CostLedger, BudgetExceeded, get_ledger  # noqa: F401
from .exporters import dump_run_trace  # noqa: F401
