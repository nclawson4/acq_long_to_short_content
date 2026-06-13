"""Resume a run that was paused for human review.

Workflow:

    1. A worker calls harness.request_human(reason, context). The harness
       writes `pipeline_runs/<job_id>/awaiting_human.json` and the run
       exits gracefully with Outbox.awaiting_human=True.

    2. The operator inspects the run dir, then writes a
       `decision.json` next to awaiting_human.json with one of:

         {"action": "continue",  "notes": "..."}
         {"action": "drop_clip", "notes": "skip this one"}
         {"action": "retry",     "notes": "go again"}
         {"action": "abort",     "notes": "..."}

    3. The operator runs:

         python -m scripts.resume_harness <job_id>

       This script reads decision.json, looks at the last successful
       checkpoint, and replays the run from there.

For the MVP we surface the decision in the resumed Outbox and write a
"resumed" marker file; restart-from-checkpoint logic is wired through
existing stage caching (ingest+transcribe cache on disk) so a resumed run
re-uses cached artifacts rather than re-downloading and re-transcribing.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("job_id")
    parser.add_argument("--runs-root", default="pipeline_runs")
    parser.add_argument("--worker", default="claude", choices=["claude", "swap"])
    args = parser.parse_args()

    run_dir = Path(args.runs_root) / args.job_id
    if not run_dir.exists():
        print(f"no such run dir: {run_dir}", file=sys.stderr)
        return 2

    from harness.escalation import (
        awaiting_path, clear_awaiting, decision_path, read_decision,
    )

    if not awaiting_path(run_dir).exists():
        print(f"run {args.job_id} is not awaiting human review", file=sys.stderr)
        return 2

    decision = read_decision(run_dir)
    if decision is None:
        print(f"please write {decision_path(run_dir)} with shape "
              f"{{\"action\": ...}} before resuming", file=sys.stderr)
        return 2

    print(f"Resuming {args.job_id} with operator decision: {decision}")

    inbox_path = run_dir / "inbox.json"
    if not inbox_path.exists():
        print("inbox.json missing — cannot resume", file=sys.stderr)
        return 2

    inbox_data = json.loads(inbox_path.read_text(encoding="utf-8"))
    action = decision.get("action", "abort")

    if action == "abort":
        clear_awaiting(run_dir)
        (run_dir / "resumed.json").write_text(
            json.dumps({"action": "abort", "decision": decision}, indent=2),
            encoding="utf-8",
        )
        print("Run aborted per operator decision.")
        return 0

    # For continue/retry/drop_clip we re-invoke the worker. The deterministic
    # stages (ingest, transcribe) hit on-disk caches keyed by video_id, so the
    # run resumes from where it left off without re-downloading.
    from harness.material import Inbox
    from harness.runtime import setup
    if args.worker == "swap":
        from agents.swap_worker import SwapWorker as W
    else:
        from agents.claude_worker import ClaudeWorker as W

    inbox = Inbox(**inbox_data)
    clear_awaiting(run_dir)
    (run_dir / "resumed.json").write_text(
        json.dumps({"action": action, "decision": decision}, indent=2),
        encoding="utf-8",
    )

    harness = setup(inbox, runs_root=args.runs_root)
    outbox = W().run(harness, harness.inbox)
    print(f"Resumed run finished: success={outbox.success}, "
          f"clips={len(outbox.result_clips)}, cost=${outbox.total_cost_usd:.4f}")
    return 0 if outbox.success else 1


if __name__ == "__main__":
    sys.exit(main())
