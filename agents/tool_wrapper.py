"""The single tool-dispatch choke point used by every worker.

Per the deck (Pillar 2 - Tools):
    "Errors come back as data the model can react to, not crashes."

Per the hackathon spec:
    "The harness governs an AI agent and the agent's behavior changes
     meaningfully based on guardrail or checkpoint feedback."

This wrapper is how that happens. Every worker tool call goes through
`invoke_tool`. It:
    1. Drains any alarms already on the bus (so we only attribute new ones).
    2. Calls the tool.
    3. Translates any raised exception into an Alarm + ok=False result.
    4. Runs the corresponding checkpoint, persists its verdict, attaches it.
    5. Drains alarms again and bundles them into ToolCallResult.new_alarms.

The worker reads ToolCallResult and decides — that's the
agent-behavior-changes-on-feedback loop.
"""
from __future__ import annotations

import time
from typing import Any, Callable, Optional

from harness.alarms import (
    Alarm,
    Severity,
    get_bus,
    translate,
)
from harness.alarms.taxonomy import by_name
from harness.checkpoints import CheckpointVerdict, save_verdict
from harness.material.envelopes import ToolCallResult
from harness.observability import get_ledger


def invoke_tool(
    *,
    tool_name: str,
    fn: Callable[..., Any],
    args: dict[str, Any],
    stage: str,
    clip_id: Optional[str] = None,
    checkpoint: Optional[Callable[..., CheckpointVerdict]] = None,
    checkpoint_args: Optional[dict[str, Any]] = None,
    run_dir: Optional[str] = None,
    output_to_dict: Optional[Callable[[Any], dict[str, Any]]] = None,
) -> tuple[Optional[Any], ToolCallResult]:
    """Invoke a tool and return (raw_output, ToolCallResult).

    raw_output is None on failure; the worker uses it to chain into the next
    stage (passing the IngestResult to transcribe, etc.). ToolCallResult is
    the envelope the worker reads to decide next action.
    """
    bus = get_bus()
    bus.drain()  # discard any pre-existing alarms; we want only new ones

    cost_before = get_ledger().total_usd if _ledger_available() else 0.0
    t0 = time.monotonic()

    raw_output: Optional[Any] = None
    err: Optional[str] = None
    ok = False

    try:
        raw_output = fn(**args)
        ok = True
    except Exception as e:  # noqa: BLE001 — we translate everything
        alarm = translate(e, stage=stage, clip_id=clip_id)
        bus.fire(alarm)
        err = f"{type(e).__name__}: {str(e)[:300]}"

    duration_ms = int((time.monotonic() - t0) * 1000)
    cost_after = get_ledger().total_usd if _ledger_available() else 0.0
    cost_usd = max(0.0, cost_after - cost_before)

    # Run checkpoint if provided AND tool succeeded. (Failed tools have no
    # output to evaluate; the alarm fired during translate is the signal.)
    verdict: Optional[CheckpointVerdict] = None
    if ok and checkpoint is not None:
        try:
            cp_args = dict(checkpoint_args or {})
            verdict = checkpoint(raw_output, **cp_args)
            # Fire any alarms the checkpoint requested by name.
            for name in verdict.alarm_names:
                spec = by_name(name)
                if spec is None:
                    continue
                bus.fire(Alarm(
                    name=spec.name,
                    severity=spec.severity,
                    context={"stage": stage, "verdict_summary": verdict.summary},
                    recommended_action=spec.default_action,
                    stage=stage,
                    clip_id=clip_id,
                    message=verdict.summary,
                ))
            if run_dir is not None:
                save_verdict(run_dir, verdict, clip_id=clip_id)
        except Exception as e:  # noqa: BLE001
            # Checkpoint itself crashed — alarm, but keep the tool output.
            bus.fire(Alarm(
                name="UnexpectedFailure",
                severity=Severity.ERROR,
                context={"phase": "checkpoint", "stage": stage, "error": str(e)[:200]},
                recommended_action=__import__(
                    "harness.alarms", fromlist=["RecommendedAction"]
                ).RecommendedAction.CONTINUE,
                stage=stage,
                clip_id=clip_id,
            ))

    new_alarms = [a.to_dict() for a in bus.drain()]

    return raw_output, ToolCallResult(
        tool_name=tool_name,
        ok=ok and (verdict is None or verdict.passed),
        output=_safe_dict(raw_output, output_to_dict) if ok else None,
        error=err,
        checkpoint=verdict.model_dump() if verdict is not None else None,
        new_alarms=new_alarms,
        duration_ms=duration_ms,
        cost_usd=cost_usd,
    )


def _ledger_available() -> bool:
    try:
        get_ledger()
        return True
    except Exception:
        return False


def _safe_dict(obj: Any, custom: Optional[Callable[[Any], dict[str, Any]]]) -> dict[str, Any]:
    if custom is not None:
        try:
            return custom(obj)
        except Exception:
            pass
    # Pydantic v2
    if hasattr(obj, "model_dump"):
        try:
            return obj.model_dump(mode="json")
        except Exception:
            return obj.model_dump()
    if isinstance(obj, dict):
        return obj
    return {"value": repr(obj)[:500]}
