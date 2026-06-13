"""ClaudeWorker — the primary AI agent that orchestrates the pipeline.

This is the "Loop" pillar (deck pillar 1). The harness governs the loop —
limits, alarms, checkpoints, persistence — and this worker is the reasoning
that drives it.

Loop shape (hybrid: deterministic happy path + LLM decision on failure):

    setup -> ingest -> [checkpoint] ->
             transcribe -> [checkpoint] ->
             pick_timestamps -> [checkpoint] ->
                 per-clip:
                    crop -> [checkpoint] ->
                    correct_captions (LLM) ->
                    add_captions -> [checkpoint] ->
                    finalize -> [checkpoint]
                 (after each: if !ok, consult Claude for action)

Why hybrid: the happy path doesn't need an LLM decision — the next stage is
obvious. But the deck-required "agent behavior changes meaningfully based on
checkpoint feedback" is satisfied because every checkpoint failure / alarm
routes through `_consult_claude_for_action(situation, alarms)`, which picks
between retry / drop_clip / escalate_human / abort.

A pure tool-use agent loop is more dazzling but burns tokens on every step
without changing the behavior of correct runs. We optimize for visible
LLM-driven decisions exactly where they matter: when something failed.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

from harness.alarms import (
    Alarm,
    RecommendedAction,
    Severity,
    get_bus,
)
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
from harness.observability import BudgetExceeded, get_ledger
from harness.runtime import (
    Harness,
    TurnCapExceeded,
    WallClockExceeded,
    sanitize_transcript_text,
)

from pipeline.llm import call_claude
from pipeline.stages.correct_captions import correct_captions
from pipeline.tools.real_adapters import install_real_adapters


# Decision verbs the worker can return. Mirrors RecommendedAction but the
# worker is allowed to be more specific (e.g. nudge a retry with a smaller
# window) by returning new tool args.
ACTIONS = ("continue", "retry_stage", "retry_with_args", "drop_clip",
           "escalate_human", "abort_run")


_DECIDER_SYSTEM = """You are the recovery brain of an automated YouTube-to-shorts pipeline.

You are NOT writing code. You are NOT calling tools. You are giving a structured decision when a checkpoint has failed or an alarm has fired.

INPUT (in the user message):
    situation         — short string describing what just happened
    failed_checkpoint — pass/fail criteria from the checkpoint
    alarms            — list of {name, severity, recommended_action, context}
    available_actions — which decisions are valid right now
    state             — concise summary of the run so far (turn, clip, retries)

OUTPUT — STRICT JSON only, no commentary, no markdown:

{
  "action": "<one of available_actions>",
  "reasoning": "<one short sentence why>",
  "args_override": {<optional adjustments to the failing tool's args>}
}

RULES
- Prefer the recommended_action of the highest-severity alarm UNLESS context
  suggests it would just fail again. e.g. retrying a "no faces detected"
  crop with the same window will fail again — drop_clip is better.
- If multiple clips are queued and one is failing, drop_clip > abort_run.
- If we are at the spend ceiling, abort_run is correct.
- If a transcript checkpoint failed AND we have not yet retried, retry_stage
  may help (sometimes Deepgram returns garbage on flaky network).
- escalate_human only when the failure is ambiguous (e.g. moderate alarm,
  no clear automated path). The human gets a copy of `state`.
- Never invent fields not on `args_override`. If you have no override to
  propose, return {} for it.

Return JSON only."""


class ClaudeWorker:
    """Primary worker. Conforms to agents.base.Worker Protocol."""

    name = "claude-haiku-orchestrator"
    version = "1.0"

    def __init__(self, *, model: Optional[str] = None) -> None:
        # None -> defer to CLAUDE_MODEL env var or pipeline.llm default.
        self._model = model

    # ------------------------------------------------------------------
    # Top-level run
    # ------------------------------------------------------------------

    def run(self, harness: Harness, inbox: Inbox) -> Outbox:
        # Make sure real adapters are in the tool registry (this is the
        # boot step the orchestrator used to do — moved into the worker so
        # workers stay self-contained when swapped).
        try:
            install_real_adapters()
        except Exception:
            pass  # placeholders fall back

        t_start = time.monotonic()
        turn = 0
        ingest_res: Optional[IngestResult] = None
        transcript: Optional[TranscribeResult] = None
        picks: Optional[PickTimestampsResult] = None
        final_clips: list[FinalClip] = []
        error: Optional[str] = None
        workdir = Path(tempfile.mkdtemp(prefix=f"{inbox.job_id}_"))

        try:
            # -------- stage 1: ingest --------
            turn += 1
            harness.check_limits(turn)
            ingest_res = self._do_stage(
                harness, turn, "ingest",
                args={
                    "url": str(inbox.url),
                    "workdir": str(workdir),
                    "job_id": inbox.job_id,
                },
                stage_label="ingest",
            )
            if ingest_res is None:
                return self._abort(harness, "ingest_failed", t_start, final_clips)

            # -------- stage 2: transcribe --------
            turn += 1
            harness.check_limits(turn)
            transcript = self._do_stage(
                harness, turn, "transcribe",
                args={"ingest": ingest_res, "job_id": inbox.job_id},
                stage_label="transcribe",
            )
            if transcript is None:
                return self._abort(harness, "transcribe_failed", t_start, final_clips)

            # Input guardrail: sanitize transcript text BEFORE any LLM stage
            # touches it (correct_captions, future scoring agent).
            transcript = self._sanitize_transcript(transcript)

            # -------- stage 3: pick --------
            turn += 1
            harness.check_limits(turn)
            picks = self._do_stage(
                harness, turn, "pick_timestamps",
                args={"transcript": transcript, "ingest": ingest_res},
                stage_label="pick_timestamps",
                checkpoint_args={"source_duration_s": ingest_res.duration_s},
            )
            if picks is None or not picks.clips:
                return self._abort(harness, "no_clips_picked", t_start, final_clips)

            # -------- per-clip pipeline --------
            for clip in picks.clips[: inbox.target_clip_count]:
                if harness.awaiting_human:
                    break
                fc = self._process_clip(
                    harness, turn, clip, ingest=ingest_res, transcript=transcript,
                    workdir=workdir,
                )
                turn = harness.limits.max_turns  # turn-accounting happens inside _process_clip
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
                context={
                    "stage": e.stage, "attempted_usd": e.attempted_usd,
                    "total_usd": e.total_usd, "ceiling": e.ceiling,
                },
                recommended_action=RecommendedAction.ABORT_RUN,
                stage="loop",
                message=str(e),
            ))
            error = str(e)
        except Exception as e:  # noqa: BLE001
            get_bus().fire(Alarm(
                name="UnexpectedFailure",
                severity=Severity.ERROR,
                context={"exception_class": type(e).__name__},
                recommended_action=RecommendedAction.ABORT_RUN,
                stage="loop",
                message=str(e)[:300],
            ))
            error = f"{type(e).__name__}: {e}"

        return self._build_outbox(harness, t_start, final_clips, error=error)

    # ------------------------------------------------------------------
    # Per-clip pipeline
    # ------------------------------------------------------------------

    def _process_clip(
        self,
        harness: Harness,
        turn_base: int,
        clip: ClipTimestamp,
        *,
        ingest: IngestResult,
        transcript: TranscribeResult,
        workdir: Path,
    ) -> Optional[FinalClip]:
        clip_id = clip.clip_id
        turn = turn_base

        # ----- crop -----
        turn += 1
        harness.check_limits(turn)
        crop_res = self._do_stage(
            harness, turn, "crop_video",
            args={"ingest": ingest, "clip": clip, "workdir": workdir},
            clip_id=clip_id,
            stage_label="crop_video",
        )
        if crop_res is None:
            return None  # drop this clip; alarm already fired

        # ----- correct_captions (per-clip transcript subset) -----
        clip_words = _subset_words_to_clip(transcript, clip)
        try:
            corrected = correct_captions(
                clip_words, clip_id=clip_id, video_title=ingest.title or ""
            )
        except Exception:  # noqa: BLE001
            corrected = clip_words

        # ----- add_captions -----
        turn += 1
        harness.check_limits(turn)
        cap_res = self._do_stage(
            harness, turn, "add_captions",
            args={
                "crop": crop_res, "words": corrected,
                "clip_start_s": float(clip.start_s), "workdir": workdir,
            },
            clip_id=clip_id,
            stage_label="add_captions",
        )
        if cap_res is None:
            return None

        # ----- finalize -----
        turn += 1
        harness.check_limits(turn)
        fc = self._do_stage(
            harness, turn, "finalize",
            args={
                "caption": cap_res, "crop": crop_res, "clip": clip,
                "job_id": harness.inbox.job_id, "video_id": ingest.video_id,
            },
            clip_id=clip_id,
            stage_label="finalize",
            checkpoint_args={
                "expected_duration_s": _expected_duration_s(clip),
                "local_path": cap_res.captioned_video_path,
            },
        )
        return fc

    # ------------------------------------------------------------------
    # Stage runner with retry/consult logic
    # ------------------------------------------------------------------

    def _do_stage(
        self,
        harness: Harness,
        turn: int,
        tool_name: str,
        *,
        args: dict[str, Any],
        stage_label: str,
        clip_id: Optional[str] = None,
        checkpoint_args: Optional[dict[str, Any]] = None,
        attempt: int = 0,
    ) -> Optional[Any]:
        """Invoke a tool, run its checkpoint, and on failure consult Claude."""
        raw, result = harness.invoke(
            tool_name, args,
            clip_id=clip_id,
            checkpoint_args=checkpoint_args,
        )

        if result.ok:
            harness.log_decision(
                turn=turn,
                situation=f"{tool_name}:ok",
                action="continue",
                reasoning=f"checkpoint passed; cost +${result.cost_usd:.4f}",
                context={"clip_id": clip_id, "duration_ms": result.duration_ms},
            )
            return raw

        # Failure path: consult the model for a decision.
        decision = self._decide(
            harness=harness,
            turn=turn,
            tool_name=tool_name,
            tool_result=result,
            attempt=attempt,
            clip_id=clip_id,
        )
        action = decision.get("action", "abort_run")
        reasoning = decision.get("reasoning", "")
        args_override = decision.get("args_override") or {}

        harness.log_decision(
            turn=turn,
            situation=f"{tool_name}:fail",
            action=action,
            reasoning=reasoning,
            context={
                "clip_id": clip_id,
                "alarms": [a["name"] for a in result.new_alarms],
                "checkpoint_failed": (
                    [c["name"] for c in (result.checkpoint or {}).get("criteria", []) if not c.get("passed")]
                    if result.checkpoint else []
                ),
                "attempt": attempt,
                "args_override": args_override,
            },
        )

        if action == "retry_stage" and attempt < harness.limits.max_retries_per_stage:
            return self._do_stage(
                harness, turn, tool_name,
                args=args, stage_label=stage_label, clip_id=clip_id,
                checkpoint_args=checkpoint_args, attempt=attempt + 1,
            )
        if action == "retry_with_args" and attempt < harness.limits.max_retries_per_stage:
            merged = {**args, **args_override}
            return self._do_stage(
                harness, turn, tool_name,
                args=merged, stage_label=stage_label, clip_id=clip_id,
                checkpoint_args=checkpoint_args, attempt=attempt + 1,
            )
        if action == "drop_clip":
            return None
        if action == "escalate_human":
            harness.request_human(
                reason=f"{tool_name} failed and the worker requested human input",
                context={
                    "tool": tool_name,
                    "clip_id": clip_id,
                    "alarms": result.new_alarms,
                    "checkpoint": result.checkpoint,
                    "attempt": attempt,
                },
            )
            return None
        # abort_run, continue (unsafe to continue past a failed checkpoint),
        # or unknown -> treat as drop_clip if there's a clip, else return None.
        return None

    # ------------------------------------------------------------------
    # Decision call (the LLM judgment seam)
    # ------------------------------------------------------------------

    def _decide(
        self,
        *,
        harness: Harness,
        turn: int,
        tool_name: str,
        tool_result,
        attempt: int,
        clip_id: Optional[str],
    ) -> dict[str, Any]:
        """Ask Claude what to do given the failure. Falls back to the
        alarm's recommended_action if the LLM can't be reached or budget
        is exhausted."""
        alarms = tool_result.new_alarms or []
        checkpoint = tool_result.checkpoint or {}
        available = self._available_actions(attempt, harness)

        # Default action if Claude isn't callable: follow the highest-severity
        # alarm's recommended_action.
        fallback = self._fallback_action(alarms, available)

        # If we're already near the budget ceiling, don't spend more on the
        # decision call — just use the fallback.
        if harness.ledger.remaining_usd() < 0.01:
            return {
                "action": fallback,
                "reasoning": "budget exhausted; using alarm fallback",
                "args_override": {},
            }

        state = {
            "turn": turn,
            "tool": tool_name,
            "clip_id": clip_id,
            "attempt": attempt,
            "spend_usd": round(harness.ledger.total_usd, 4),
            "ceiling_usd": harness.ledger.ceiling_usd,
            "remaining_clips_target": max(0, harness.inbox.target_clip_count - 1),
        }

        user_msg = json.dumps({
            "situation": f"{tool_name} produced ok=False at turn {turn}",
            "failed_checkpoint": checkpoint,
            "alarms": alarms,
            "available_actions": available,
            "state": state,
            "fallback": fallback,
        }, indent=2)[:8000]

        try:
            res = call_claude(
                stage="decision",
                system=_DECIDER_SYSTEM,
                messages=[{"role": "user", "content": user_msg}],
                model=self._model,
                max_tokens=512,
                temperature=0.0,
                cache_system=True,
            )
        except BudgetExceeded:
            return {"action": "abort_run", "reasoning": "BudgetExceeded during decision", "args_override": {}}
        except Exception as e:  # noqa: BLE001
            return {"action": fallback, "reasoning": f"decision_llm_error:{type(e).__name__}", "args_override": {}}

        parsed = _parse_decision(res.text, available, fallback)
        return parsed

    def _available_actions(self, attempt: int, harness: Harness) -> list[str]:
        actions = ["drop_clip", "escalate_human", "abort_run"]
        if attempt < harness.limits.max_retries_per_stage:
            actions = ["retry_stage", "retry_with_args"] + actions
        return actions

    def _fallback_action(self, alarms: list[dict[str, Any]], available: list[str]) -> str:
        """Pick the highest-severity alarm's recommended_action that is in
        `available`, else 'abort_run'."""
        order = {"critical": 0, "error": 1, "warning": 2, "info": 3}
        for a in sorted(alarms, key=lambda x: order.get(x.get("severity", "info"), 4)):
            ra = a.get("recommended_action")
            if ra in available:
                return ra
        return "abort_run"

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _sanitize_transcript(self, transcript: TranscribeResult) -> TranscribeResult:
        before = transcript.full_text
        after = sanitize_transcript_text(before)
        if after != before:
            get_bus().fire(Alarm(
                name="TranscriptInjectionStripped",
                severity=Severity.WARNING,
                context={"chars_changed": len(before) - len(after)},
                recommended_action=RecommendedAction.CONTINUE,
                stage="guardrail:input",
                message="injection markers replaced with [redacted]",
            ))
        return transcript.model_copy(update={"full_text": after})

    def _abort(self, harness: Harness, reason: str, t_start: float,
               clips: list[FinalClip]) -> Outbox:
        return self._build_outbox(harness, t_start, clips, error=reason)

    def _build_outbox(
        self,
        harness: Harness,
        t_start: float,
        final_clips: list[FinalClip],
        *,
        error: Optional[str] = None,
    ) -> Outbox:
        verdicts_dir = harness.run_dir / "checkpoints"
        passed = failed = 0
        if verdicts_dir.exists():
            for p in verdicts_dir.glob("*.json"):
                try:
                    obj = json.loads(p.read_text(encoding="utf-8"))
                    if obj.get("passed"):
                        passed += 1
                    else:
                        failed += 1
                except json.JSONDecodeError:
                    continue

        success = bool(final_clips) and not error and not harness.awaiting_human

        outbox = Outbox(
            job_id=harness.inbox.job_id,
            success=success,
            worker_name=self.name,
            worker_version=self.version,
            result_clips=[fc.model_dump() for fc in final_clips],
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


# ---------------------------------------------------------------------------
# small free helpers
# ---------------------------------------------------------------------------

def _extract_video_id(url: str) -> str:
    import re
    from urllib.parse import parse_qs, urlparse
    parsed = urlparse(url)
    qs = parse_qs(parsed.query or "")
    if "v" in qs:
        m = re.fullmatch(r"[A-Za-z0-9_-]{11}", qs["v"][0])
        if m:
            return qs["v"][0]
    for part in (parsed.path or "").split("/"):
        if re.fullmatch(r"[A-Za-z0-9_-]{11}", part):
            return part
    return "unknown"


def _subset_words_to_clip(transcript: TranscribeResult, clip: ClipTimestamp):
    """Slice transcript words to the clip envelope (or kept_segments)."""
    if clip.kept_segments:
        out = []
        for a, b in clip.kept_segments:
            out.extend(w for w in transcript.words if a <= w.start_s < b)
        return out
    return [w for w in transcript.words if clip.start_s <= w.start_s < clip.end_s]


def _expected_duration_s(clip: ClipTimestamp) -> float:
    if clip.kept_segments:
        return sum(max(0.0, b - a) for a, b in clip.kept_segments)
    return clip.end_s - clip.start_s


def _parse_decision(text: str, available: list[str], fallback: str) -> dict[str, Any]:
    """Tolerant JSON extractor — same approach as correct_captions."""
    if not text:
        return {"action": fallback, "reasoning": "empty_response", "args_override": {}}
    raw = text.strip()
    if raw.startswith("```"):
        # strip fence
        raw = raw.strip("`")
        if raw.startswith("json"):
            raw = raw[4:]
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end < 0:
        return {"action": fallback, "reasoning": "no_json", "args_override": {}}
    try:
        obj = json.loads(raw[start: end + 1])
    except json.JSONDecodeError:
        return {"action": fallback, "reasoning": "json_decode", "args_override": {}}
    action = obj.get("action") or fallback
    if action not in available:
        action = fallback
    return {
        "action": action,
        "reasoning": (obj.get("reasoning") or "")[:300],
        "args_override": obj.get("args_override") or {},
    }
