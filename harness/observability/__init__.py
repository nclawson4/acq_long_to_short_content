"""Observability — OTel spans + cost ledger.

Pillar 4 of the harness. Re-exports from pipeline.observability so the
harness package is the canonical surface area. Workers should not import
from pipeline.observability directly; they go through harness.observability.
"""
from pipeline.observability.ledger import (  # noqa: F401
    BudgetExceeded,
    CostEntry,
    CostLedger,
    get_ledger,
    reset_ledger,
    set_ledger,
)
from pipeline.observability.tracer import (  # noqa: F401
    clear_in_memory_spans,
    get_in_memory_spans,
    get_tracer,
    init_tracing,
    span,
    traced,
)

__all__ = [
    "BudgetExceeded",
    "CostEntry",
    "CostLedger",
    "clear_in_memory_spans",
    "get_in_memory_spans",
    "get_ledger",
    "get_tracer",
    "init_tracing",
    "reset_ledger",
    "set_ledger",
    "span",
    "traced",
]
