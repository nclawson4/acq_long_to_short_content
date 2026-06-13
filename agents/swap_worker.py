"""SwapWorker — a deterministic rule-based worker.

This is the second worker for the hackathon bonus ("a second worker is
swapped in during the demo to prove portability"). It conforms to the
agents.base.Worker Protocol exactly like ClaudeWorker, but it does NOT call
an LLM for decisions.

Why a deterministic swap, not just a different LLM:

    The deck's thesis: "The model is a commodity you call. Your harness is
    the durable engineering."

    A deterministic worker proves the thesis. Same harness, same alarms,
    same checkpoints, same guardrails — but the reasoning is a rule
    engine instead of Claude. If the harness is doing its job, the
    user-visible behavior on the failure paths should be similar.

When a checkpoint fails or an alarm fires, SwapWorker picks the
recommended_action of the highest-severity alarm. It also exposes a
`policy` hook so an OpenAI/Gemini variant can override decisions without
re-implementing the loop.

Demo script (Friday):
    1. Run ClaudeWorker on a video — show the decisions.jsonl with LLM
       reasoning strings.
    2. Re-run with `--worker swap` on the same video — show the same
       behavior (clips ship, alarms surface, checkpoints persist), but
       decisions.jsonl reasoning strings are "policy:<rule>" instead of
       LLM-generated.
    3. Point: the harness is the load-bearing layer. Swap the worker —
       constraints still hold.
"""
from __future__ import annotations

import tempfile
import time
from pathlib import Path
from typing import Any, Optional

from harness.alarms import Alarm, RecommendedAction, Severity, get_bus
from harness.material import (
    CaptionResult,
    ClipTimestamp,
    CropResult,
    FinalClip,
    Inbox,
    IngestResult,
    Outbox,
    PickTimestampsResult,
    TranscribeResult,
)
from harness.observability import BudgetExceeded
from harness.runtime import (
    Harness,
    TurnCapExceeded,
    WallClockExceeded,
    sanitize_transcript_text,
)

from pipeline.stages.correct_captions import correct_captions
from pipeline.tools.real_adapters import install_real_adapters


class SwapWorker:
    """Deterministic worker conforming to agents.base.Worker Protocol.

    The implementation deliberately mirrors ClaudeWorker's structure so a
    side-by-side reading of the two files makes the swap obvious: same
    pipeline, different brain.
    """

    name = "deterministic-rule-worker"
    version = "1.0"

    def run(self, harness: Harness, inbox: Inbox) -> Outbox:
        try:
            install_real_adapters()
        except Exception:
            pass

        t_start = time.monotonic()
        turn = 0
        final_clips: list[FinalClip] = []
        error: Optional[str] = None
        workdir = Path(tempfile.mkdtemp(prefix=f"{inbox.job_id}_swap_"))

        try:
            # stage 1: ingest
            turn += 1
            harness.check_limits(turn)
            ingest_res = self._do_stage(
                harness, turn, "ingest",
                {"url": str(inbox.url), "workdir": str(workdir), "job_id": inbox.job_id},
            )
            if ingest_res is None:
                return self._finish(harness, t_start, final_clips, "ingest_failed")

            # stage 2: transcribe
            turn += 1
            harness.check_limits(turn)
            transcript = self._do_stage(
                harness, turn, "transcribe",
                {"ingest": ingest_res, "job_id": inbox.job_id},
            )
            if transcript is None:
                return self._finish(harness, t_start, final_clips, "transcribe_failed")

            transcript = self._sanitize(transcript)

            # stage 3: pick
            turn += 1
            harness.check_limits(turn)
            picks: Optional[PickTimestampsResult] = self._do_stage(
                harness, turn, "pick_timestamps",
                {"transcript": transcript, "ingest": ingest_res},
                checkpoint_args={"source_duration_s": ingest_res.duration_s},
            )
            if picks is None or not picks.clips:
                return self._finish(harness, t_start, final_clips, "no_clips_picked")

            for clip in picks.clips[: inbox.target_clip_count]:
                if harness.awaiting_human:
                    break
                fc = self._process_clip(harness, turn, clip, ingest_res, transcript, workdir)
                if fc is not None:
                    final_clips.append(fc)

        except (TurnCapExceeded, WallClockExceeded) as e:
            get_bus().fire(Alarm(
                name="TurnCapExceeded" if isinstance(e, TurnCapExceeded) else "WallClockExceeded",
                severity=Severity.CRITICAL,
                context={"message": str(e)},
                recommended_action=RecommendedAction.ABORT_RUN,
                stage="loop",
                message=str(e),
            ))
            error = str(e)
        except BudgetExceeded as e:
            get_bus().fire(Alarm(
                name="BudgetExceeded",
                severity=Severity.CRITICAL,
                context={"total_usd": e.total_usd, "ceiling": e.ceiling},
                recommended_action=RecommendedAction.ABORT_RUN,
                stage="loop",
                message=str(e),
            ))
            error = str(e)

        return self._finish(harness, t_start, final_clips, error)

    # ------------------------------------------------------------------
    # per-clip
    # ------------------------------------------------------------------

    def _process_clip(self, harness, turn_base, clip, ingest, transcript, workdir) -> Optional[FinalClip]:
        cid = clip.clip_id
        turn = turn_base

        turn += 1
        harness.check_limits(turn)
        crop = self._do_stage(
            harness, turn, "crop_video",
            {"ingest": ingest, "clip": clip, "workdir": workdir},
            clip_id=cid,
        )
        if crop is None:
            return None

        clip_words = self._subset_words(transcript, clip)
        try:
            corrected = correct_captions(clip_words, clip_id=cid, video_title=ingest.title or "")
        except Exception:
            corrected = clip_words

        turn += 1
        harness.check_limits(turn)
        cap = self._do_stage(
            harness, turn, "add_captions",
            {"crop": crop, "words": corrected, "clip_start_s": float(clip.start_s), "workdir": workdir},
            clip_id=cid,
        )
        if cap is None:
            return None

        turn += 1
        harness.check_limits(turn)
        fc = self._do_stage(
            harness, turn, "finalize",
            {
                "caption": cap, "crop": crop, "clip": clip,
                "job_id": harness.inbox.job_id, "video_id": ingest.video_id,
            },
            clip_id=cid,
            checkpoint_args={
                "expected_duration_s": self._expected_dur(clip),
                "local_path": cap.captioned_video_path,
            },
        )
        return fc

    # ------------------------------------------------------------------
    # decision policy (deterministic)
    # ------------------------------------------------------------------

    def _do_stage(
        self,
        harness: Harness,
        turn: int,
        tool_name: str,
        args: dict[str, Any],
        *,
        clip_id: Optional[str] = None,
        checkpoint_args: Optional[dict[str, Any]] = None,
        attempt: int = 0,
    ):
        raw, result = harness.invoke(
            tool_name, args, clip_id=clip_id, checkpoint_args=checkpoint_args
        )

        if result.ok:
            harness.log_decision(
                turn=turn, situation=f"{tool_name}:ok", action="continue",
                reasoning="policy: checkpoint passed",
                context={"clip_id": clip_id, "duration_ms": result.duration_ms},
            )
            return raw

        # Deterministic decision: highest-severity alarm's recommended_action,
        # clamped to what's allowed at this attempt count.
        allowed = self._allowed_actions(attempt, harness)
        action = self._pick(result.new_alarms, allowed)
        harness.log_decision(
            turn=turn, situation=f"{tool_name}:fail",
            action=action,
            reasoning=f"policy:swap_worker:attempt={attempt}",
            context={
                "clip_id": clip_id,
                "alarms": [a["name"] for a in result.new_alarms],
            },
        )

        if action == "retry_stage" and attempt < harness.limits.max_retries_per_stage:
            return self._do_stage(harness, turn, tool_name, args,
                                  clip_id=clip_id, checkpoint_args=checkpoint_args,
                                  attempt=attempt + 1)
        if action == "drop_clip":
            return None
        if action == "escalate_human":
            harness.request_human(
                reason=f"swap-worker policy: {tool_name} failed and is unrecoverable",
                context={
                    "tool": tool_name, "clip_id": clip_id,
                    "alarms": result.new_alarms,
                    "checkpoint": result.checkpoint,
                },
            )
            return None
        return None

    def _allowed_actions(self, attempt: int, harness: Harness) -> list[str]:
        ok = ["drop_clip", "escalate_human", "abort_run"]
        if attempt < harness.limits.max_retries_per_stage:
            ok = ["retry_stage"] + ok
        return ok

    def _pick(self, alarms: list[dict[str, Any]], allowed: list[str]) -> str:
        # Map LLM-only actions to nearest deterministic equivalent.
        downgrade = {"retry_with_adjusted_args": "retry_stage"}
        order = {"critical": 0, "error": 1, "warning": 2, "info": 3}
        for a in sorted(alarms, key=lambda x: order.get(x.get("severity"), 4)):
            ra = a.get("recommended_action")
            ra = downgrade.get(ra, ra)
            if ra in allowed:
                return ra
            # If the LLM-recommended action isn't available, fall back to
            # drop_clip when severity is error or worse — better than aborting
            # the whole run.
            if a.get("severity") in {"error", "critical"} and "drop_clip" in allowed:
                return "drop_clip"
        return "abort_run"

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _sanitize(self, t: TranscribeResult) -> TranscribeResult:
        before = t.full_text
        after = sanitize_transcript_text(before)
        if after != before:
            get_bus().fire(Alarm(
                name="TranscriptInjectionStripped",
                severity=Severity.WARNING,
                context={"chars_changed": len(before) - len(after)},
                recommended_action=RecommendedAction.CONTINUE,
                stage="guardrail:input",
            ))
        return t.model_copy(update={"full_text": after})

    def _subset_words(self, t: TranscribeResult, clip: ClipTimestamp):
        if clip.kept_segments:
            out = []
            for a, b in clip.kept_segments:
                out.extend(w for w in t.words if a <= w.start_s < b)
            return out
        return [w for w in t.words if clip.start_s <= w.start_s < clip.end_s]

    def _expected_dur(self, clip: ClipTimestamp) -> float:
        if clip.kept_segments:
            return sum(max(0.0, b - a) for a, b in clip.kept_segments)
        return clip.end_s - clip.start_s

    def _finish(
        self,
        harness: Harness,
        t_start: float,
        clips: list[FinalClip],
        error: Optional[str],
    ) -> Outbox:
        import json as _json
        verdicts_dir = harness.run_dir / "checkpoints"
        passed = failed = 0
        if verdicts_dir.exists():
            for p in verdicts_dir.glob("*.json"):
                try:
                    obj = _json.loads(p.read_text(encoding="utf-8"))
                    if obj.get("passed"):
                        passed += 1
                    else:
                        failed += 1
                except _json.JSONDecodeError:
                    continue
        success = bool(clips) and not error and not harness.awaiting_human
        outbox = Outbox(
            job_id=harness.inbox.job_id,
            success=success,
            worker_name=self.name,
            worker_version=self.version,
            result_clips=[c.model_dump() for c in clips],
            total_cost_usd=harness.ledger.total_usd,
            cost_breakdown=harness.ledger.by_stage(),
            duration_ms=int((time.monotonic() - t_start) * 1000),
            alarms_fired=harness.bus.to_list(),
            decisions=list(harness.decision_log),
            checkpoints_passed=passed,
            checkpoints_failed=failed,
            awaiting_human=harness.awaiting_human,
            error=error,
        )
        harness.write_outbox(outbox)
        harness.publish_terminal(outbox)
        return outbox
