"""Human-in-the-loop escalation.

Spec ("Should"): the harness knows when to stop and ask rather than guess.

Mechanism:
    - Worker emits HumanReviewRequested alarm (recommended_action=escalate_human)
      OR worker calls harness.request_human(reason, context).
    - Harness writes pipeline_runs/<job_id>/awaiting_human.json with the
      escalation reason and current state.
    - Run halts gracefully (Outbox.awaiting_human = True).
    - Operator resolves by adding a `decision.json` to the run dir, then
      re-invoking with the same job_id; runtime.resume() picks up from the
      last successful checkpoint.

Resume semantics live here too because they're tied to escalation.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Optional


def awaiting_path(run_dir: str | Path) -> Path:
    return Path(run_dir) / "awaiting_human.json"


def decision_path(run_dir: str | Path) -> Path:
    return Path(run_dir) / "decision.json"


def write_awaiting_human(
    run_dir: str | Path,
    *,
    reason: str,
    context: dict[str, Any],
) -> Path:
    payload = {
        "reason": reason,
        "context": context,
        "fired_at": time.time(),
        "instructions": (
            "To resume this run, write a JSON file `decision.json` in this "
            "directory with shape {action: 'drop_clip'|'retry'|'abort'|'continue', "
            "notes?: '...'}. Then re-invoke the harness with the same job_id."
        ),
    }
    path = awaiting_path(run_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def read_decision(run_dir: str | Path) -> Optional[dict[str, Any]]:
    """Return the operator's decision JSON, or None if not yet written."""
    path = decision_path(run_dir)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def is_awaiting_human(run_dir: str | Path) -> bool:
    return awaiting_path(run_dir).exists() and not decision_path(run_dir).exists()


def clear_awaiting(run_dir: str | Path) -> None:
    """Clean up the awaiting marker once a decision lands."""
    p = awaiting_path(run_dir)
    if p.exists():
        p.unlink()
