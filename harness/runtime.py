"""The Harness runtime — the object every worker runs inside.

Workers do not import pipeline.* directly. They get a Harness, call
`harness.invoke(...)` for tools and `harness.consult_human(...)` for
escalation, and write nothing to disk themselves. Persistence, ledger
charging, alarm bus, and limits all live here.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from harness.alarms import AlarmBus, Severity, get_bus, set_bus
from harness.guardrails import (
    DECLARED,
    InputGuardrailFailed,
    sanitize_transcript_text,
    validate_url,
)
from harness.limits import HardLimits, TurnCapExceeded, WallClockExceeded
from harness.material.envelopes import Inbox, Outbox, ToolCallResult
from harness.observability import (
    CostLedger,
    get_ledger,
    init_tracing,
    set_ledger,
)


@dataclass
class Harness:
    """One per run. Owns ledger, alarm bus, limits, run directory."""
    inbox: Inbox
    ledger: CostLedger
    bus: AlarmBus
    limits: HardLimits
    run_dir: Path
    tools: dict[str, Any]                    # name -> ToolSpec
    worker_name: str = "unknown"             # set by the worker on entry
    started_at: float = field(default_factory=time.monotonic)
    decision_log: list[dict[str, Any]] = field(default_factory=list)
    awaiting_human: bool = False
    # ---- live progress mirror to Upstash Redis (so the FE polling works) ----
    # _stage_records is a dict[str, dict] keyed by stage label (incl. clip suffix).
    # _store is lazy-loaded; False means "tried and failed, don't retry".
    _stage_records: dict[str, dict[str, Any]] = field(default_factory=dict)
    _store: Any = None

    # ---------- tool dispatch ----------

    def invoke(
        self,
        tool_name: str,
        args: dict[str, Any],
        *,
        clip_id: Optional[str] = None,
        checkpoint_args: Optional[dict[str, Any]] = None,
    ) -> tuple[Optional[Any], ToolCallResult]:
        """Run a tool through the alarm-translating wrapper.

        Returns (raw_output, ToolCallResult). The worker chains raw_output
        into the next tool; reads ToolCallResult for the decision signal.
        """
        # Lazy import to avoid the agents <-> harness cycle.
        from agents.tool_wrapper import invoke_tool

        spec = self.tools.get(tool_name)
        if spec is None:
            from harness.alarms import Alarm, RecommendedAction
            self.bus.fire(Alarm(
                name="UnknownToolCalled",
                severity=Severity.ERROR,
                context={"tool_name": tool_name, "available": sorted(self.tools)},
                recommended_action=RecommendedAction.ABORT_RUN,
                stage="loop",
                clip_id=clip_id,
                message=f"unknown tool {tool_name!r}",
            ))
            return None, ToolCallResult(
                tool_name=tool_name,
                ok=False,
                error=f"unknown tool {tool_name!r}",
                new_alarms=[a.to_dict() for a in self.bus.drain()],
            )

        stage_key = spec.stage if clip_id is None else f"{spec.stage}[{clip_id}]"
        self._mark_stage_started(stage_key)
        self._publish_state(status="running")

        raw, result = invoke_tool(
            tool_name=tool_name,
            fn=spec.fn,
            args=args,
            stage=spec.stage,
            clip_id=clip_id,
            checkpoint=spec.checkpoint,
            checkpoint_args=checkpoint_args,
            run_dir=str(self.run_dir),
            output_to_dict=spec.output_to_dict,
        )

        self._mark_stage_finished(stage_key, result=result)
        self._publish_state(status="running")
        return raw, result

    # ---------- live progress mirror (Redis) ----------

    def _mark_stage_started(self, stage_key: str) -> None:
        prior = self._stage_records.get(stage_key) or {}
        self._stage_records[stage_key] = {
            "name": stage_key,
            "status": "in_progress",
            "started_at": (prior.get("started_at")
                           or datetime.now(timezone.utc).isoformat()),
            "completed_at": None,
            "attempts": int(prior.get("attempts") or 0) + 1,
            "cost_usd": float(prior.get("cost_usd") or 0.0),
            "duration_ms": int(prior.get("duration_ms") or 0),
            "error": None,
        }

    def _mark_stage_finished(self, stage_key: str, *, result) -> None:
        rec = self._stage_records.get(stage_key) or {"name": stage_key, "attempts": 1}
        rec.update({
            "status": "completed" if result.ok else "failed",
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "cost_usd": float(rec.get("cost_usd") or 0.0) + float(result.cost_usd or 0.0),
            "duration_ms": int(rec.get("duration_ms") or 0) + int(result.duration_ms or 0),
            "error": result.error,
        })
        self._stage_records[stage_key] = rec

    def _publish_state(
        self,
        *,
        status: str = "running",
        error: Optional[str] = None,
        result_clips: Optional[list[dict[str, Any]]] = None,
    ) -> None:
        """Mirror a JobState-shaped snapshot to Redis for the FE poll.

        Silently no-ops if the store is unavailable — the disk artifacts are
        the ground truth; Redis is a UI convenience.
        """
        store = self._get_store()
        if store is None:
            return
        try:
            from pipeline.types import (
                JobState, JobStatus, PipelineResult, StageRecord, StageStatus,
            )

            stages: dict[str, StageRecord] = {}
            for key, rec in self._stage_records.items():
                try:
                    stages[key] = StageRecord(
                        name=rec["name"],
                        status=StageStatus(rec["status"]),
                        started_at=_iso_to_dt(rec.get("started_at")),
                        completed_at=_iso_to_dt(rec.get("completed_at")),
                        attempts=int(rec.get("attempts") or 0),
                        cost_usd=float(rec.get("cost_usd") or 0.0),
                        duration_ms=int(rec.get("duration_ms") or 0),
                        error=rec.get("error"),
                    )
                except Exception:
                    continue

            result_obj = None
            if result_clips is not None:
                result_obj = PipelineResult(
                    job_id=self.inbox.job_id,
                    video_id=self._extract_video_id(),
                    clips=[],  # blob URLs already in Outbox; FE polling uses cost only
                    total_cost_usd=self.ledger.total_usd,
                    cost_breakdown=self.ledger.by_stage(),
                    duration_ms=int((time.monotonic() - self.started_at) * 1000),
                )

            state = JobState(
                job_id=self.inbox.job_id,
                url=str(self.inbox.url),
                status=JobStatus(status),
                stages=stages,
                total_cost_usd=self.ledger.total_usd,
                result=result_obj,
                error=error,
            )
            if status in {"done", "failed", "budget_exceeded", "timeout"}:
                state.completed_at = datetime.now(timezone.utc)
            store.save_job(state)
        except Exception:
            # Never let a publish failure break the run.
            pass

    def _get_store(self):
        if self._store is False:
            return None
        if self._store is None:
            try:
                from pipeline.state import get_store
                self._store = get_store()
            except Exception:
                self._store = False
                return None
        return self._store

    def _extract_video_id(self) -> str:
        import re
        from urllib.parse import parse_qs, urlparse
        parsed = urlparse(str(self.inbox.url))
        qs = parse_qs(parsed.query or "")
        if "v" in qs and re.fullmatch(r"[A-Za-z0-9_-]{11}", qs["v"][0]):
            return qs["v"][0]
        for part in (parsed.path or "").split("/"):
            if re.fullmatch(r"[A-Za-z0-9_-]{11}", part):
                return part
        return "unknown"

    # ---------- limits ----------

    def check_limits(self, turn: int) -> None:
        """Raise TurnCapExceeded / WallClockExceeded if breached."""
        self.limits.check(turn)

    # ---------- decision log (audit trail) ----------

    def log_decision(
        self,
        *,
        turn: int,
        situation: str,
        action: str,
        reasoning: str | None = None,
        context: Optional[dict[str, Any]] = None,
    ) -> None:
        entry = {
            "turn": turn,
            "ts": time.time(),
            "situation": situation,
            "action": action,
            "reasoning": reasoning,
            "context": context or {},
        }
        self.decision_log.append(entry)
        # Stream to disk so a crash leaves an audit trail.
        path = self.run_dir / "decisions.jsonl"
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
        # Stream to Redis so the FE speech bubbles can pick it up on the next
        # poll. Silent no-op if the store is unavailable.
        store = self._get_store()
        if store is None:
            return
        try:
            store._b.lpush(f"job:{self.inbox.job_id}:decisions", json.dumps(entry))
        except Exception:
            pass

    # ---------- human-in-the-loop escalation ----------

    def request_human(self, *, reason: str, context: dict[str, Any]) -> None:
        """Persist an awaiting_human.json marker. Worker should stop after.

        Detailed handling lives in harness/escalation.py (task #27).
        """
        from harness.escalation import write_awaiting_human

        self.awaiting_human = True
        write_awaiting_human(self.run_dir, reason=reason, context=context)
        self.log_decision(
            turn=-1,
            situation=reason,
            action="escalate_human",
            reasoning="harness wrote awaiting_human.json",
            context=context,
        )

    # ---------- outbox persistence ----------

    def publish_terminal(self, outbox: Outbox) -> None:
        """Push final status to Redis when the worker finishes."""
        if outbox.error and "budget" in outbox.error.lower():
            status = "budget_exceeded"
        elif outbox.awaiting_human:
            status = "failed"   # FE treats awaiting_human as a halt
        elif outbox.success:
            status = "done"
        else:
            status = "failed"
        self._publish_state(
            status=status,
            error=outbox.error,
            result_clips=outbox.result_clips,
        )

    def write_outbox(self, outbox: Outbox) -> Path:
        path = self.run_dir / "outbox.json"
        path.write_text(outbox.model_dump_json(indent=2), encoding="utf-8")
        # Also write a snapshot of the alarm bus + ledger at completion.
        (self.run_dir / "alarms.json").write_text(
            json.dumps(self.bus.to_list(), indent=2), encoding="utf-8"
        )
        (self.run_dir / "ledger.json").write_text(
            json.dumps({
                "total_usd": self.ledger.total_usd,
                "ceiling_usd": self.ledger.ceiling_usd,
                "by_stage": self.ledger.by_stage(),
                "entries": self.ledger.entries(),
            }, indent=2),
            encoding="utf-8",
        )
        (self.run_dir / "guardrails.json").write_text(
            json.dumps([
                {"name": g.name, "layer": g.layer, "description": g.description}
                for g in DECLARED
            ], indent=2),
            encoding="utf-8",
        )
        return path


# ---------------- factories ----------------

def setup(inbox: Inbox, *, runs_root: str | Path = "pipeline_runs") -> Harness:
    """Boot a harness for one run.

    Order matters: ledger and bus are installed as globals BEFORE any tool
    can fire alarms / charge costs. Tracing is also initialized here.
    """
    init_tracing("acq-clipper-harness")

    # Input guardrail — fail fast and loud before doing anything else.
    try:
        canonical_url = validate_url(str(inbox.url))
    except InputGuardrailFailed as e:
        from harness.alarms import Alarm, RecommendedAction
        bus = AlarmBus()
        set_bus(bus)
        bus.fire(Alarm(
            name="UrlRejected",
            severity=Severity.ERROR,
            context={"url": str(inbox.url), "reason": str(e)},
            recommended_action=RecommendedAction.ABORT_RUN,
            stage="guardrail:input",
            message=str(e),
        ))
        # Re-raise — the API entrypoint catches and returns a 400.
        raise

    inbox = Inbox.model_validate({**inbox.model_dump(mode="json"), "url": canonical_url})

    ledger = CostLedger(
        ceiling_usd=inbox.cost_ceiling_usd,
        target_usd=inbox.cost_target_usd,
    )
    set_ledger(ledger)

    bus = AlarmBus()
    set_bus(bus)

    limits = HardLimits.start(
        max_turns=32,
        max_wall_seconds=600,        # 10 minutes per run
        max_retries_per_stage=2,
    )

    run_dir = Path(runs_root) / inbox.job_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "checkpoints").mkdir(exist_ok=True)

    # Persist the inbox so replays know what we were asked to do.
    (run_dir / "inbox.json").write_text(
        inbox.model_dump_json(indent=2), encoding="utf-8"
    )

    # Build tool registry. Lazy to avoid circular import.
    from agents.tools_registry import build_default_specs
    tools = {spec.name: spec for spec in build_default_specs()}

    harness = Harness(
        inbox=inbox,
        ledger=ledger,
        bus=bus,
        limits=limits,
        run_dir=run_dir,
        tools=tools,
    )
    # Initial mirror to Redis so /api/status returns 200 immediately, not 404.
    harness._publish_state(status="queued")
    return harness


# ---------------- small helpers ----------------

def _iso_to_dt(s: Optional[str]):
    if not s:
        return None
    try:
        from datetime import datetime as _dt
        return _dt.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


__all__ = ["Harness", "setup", "TurnCapExceeded", "WallClockExceeded",
           "sanitize_transcript_text"]
