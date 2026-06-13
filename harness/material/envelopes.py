"""Inbox / Outbox / ToolCallResult — run-level material envelopes.

Inbox is what the user gives the harness; Outbox is what the harness returns
to the user. ToolCallResult is the per-tool envelope the worker receives back
after every tool invocation — bundling the result, the checkpoint verdict,
and any new alarms into one object the worker reads.

The tool-result envelope is the "errors-as-data" mechanism (deck pillar 2):
when a tool fails, the worker doesn't get an exception — it gets a
ToolCallResult with ok=False, error filled in, and the alarms the failure
fired.
"""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field, HttpUrl


# ---------- Inbox: what the harness consumes ----------

class Inbox(BaseModel):
    """One job's worth of input material. The harness's only entry point."""
    job_id: str
    url: HttpUrl
    target_clip_count: int = 1   # how many shorts to try to produce
    cost_ceiling_usd: float = 1.00
    cost_target_usd: float = 0.20
    requested_by: Optional[str] = None     # e.g. "demo", user email
    notes: Optional[str] = None            # free-form, ends up in trace


# ---------- Outbox: what the harness returns ----------

class Outbox(BaseModel):
    """Final material handed back to the user.

    Wraps PipelineResult with run-level metadata: which worker drove the
    loop, how many alarms fired, and where the trace can be found.
    """
    job_id: str
    success: bool
    worker_name: str
    worker_version: str
    result_clips: list[dict[str, Any]] = Field(default_factory=list)
    total_cost_usd: float = 0.0
    cost_breakdown: dict[str, float] = Field(default_factory=dict)
    duration_ms: int = 0
    alarms_fired: list[dict[str, Any]] = Field(default_factory=list)
    decisions: list[dict[str, Any]] = Field(default_factory=list)
    checkpoints_passed: int = 0
    checkpoints_failed: int = 0
    awaiting_human: bool = False
    error: Optional[str] = None
    trace_id: Optional[str] = None


# ---------- ToolCallResult: per-turn worker feedback ----------

class ToolCallResult(BaseModel):
    """What the worker reads after invoking a tool.

    ok=True       — tool returned cleanly; `output` holds the typed result
    ok=False      — tool failed; `error` holds a short string; output is None

    Either way, `checkpoint` is the post-tool verdict (if a checkpoint ran)
    and `new_alarms` is the list of alarms the bus saw fire during this
    tool call. The worker uses these to decide its next move.
    """
    tool_name: str
    ok: bool
    output: Optional[dict[str, Any]] = None
    error: Optional[str] = None
    checkpoint: Optional[dict[str, Any]] = None   # CheckpointVerdict.model_dump()
    new_alarms: list[dict[str, Any]] = Field(default_factory=list)
    duration_ms: int = 0
    cost_usd: float = 0.0
