"""CLI entrypoint for the harness — what the demo + smoke tests call.

    python -m scripts.run_harness <youtube_url> [job_id]

Or programmatically:

    from harness.material import Inbox
    from harness.runtime import setup
    from agents.claude_worker import ClaudeWorker
    outbox = ClaudeWorker().run(setup(inbox), inbox)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path


def _load_env_local() -> None:
    """Same env loader the old local_run.py used. Skips empty values."""
    env_path = Path(__file__).resolve().parents[1] / ".env.local"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        if val:
            os.environ.setdefault(key, val)


def main() -> int:
    _load_env_local()

    parser = argparse.ArgumentParser()
    parser.add_argument("url", help="YouTube URL to process")
    parser.add_argument(
        "--job-id", default=None,
        help="Stable job id (auto-generated if omitted)",
    )
    parser.add_argument(
        "--worker", default=os.environ.get("WORKER", "claude"),
        choices=["claude", "swap"],
        help="Which worker drives the loop. 'swap' is the bonus second worker.",
    )
    parser.add_argument("--target-clips", type=int, default=1)
    parser.add_argument("--cost-ceiling", type=float, default=1.00)
    parser.add_argument("--cost-target", type=float, default=0.20)
    parser.add_argument("--runs-root", default="pipeline_runs")
    args = parser.parse_args()

    # Local imports so --help works even when deps are missing.
    from harness.material import Inbox
    from harness.runtime import setup

    job_id = args.job_id or f"job-{uuid.uuid4().hex[:8]}"
    inbox = Inbox(
        job_id=job_id,
        url=args.url,
        target_clip_count=args.target_clips,
        cost_ceiling_usd=args.cost_ceiling,
        cost_target_usd=args.cost_target,
        requested_by="cli",
    )

    try:
        harness = setup(inbox, runs_root=args.runs_root)
    except Exception as e:
        print(f"setup failed: {e}", file=sys.stderr)
        return 2

    worker = _pick_worker(args.worker)
    print(f"Driving with worker: {worker.name} v{worker.version}")
    outbox = worker.run(harness, harness.inbox)

    print()
    print(f"Job:               {outbox.job_id}")
    print(f"Worker:            {outbox.worker_name} v{outbox.worker_version}")
    print(f"Success:           {outbox.success}")
    print(f"Clips produced:    {len(outbox.result_clips)}")
    print(f"Total cost:        ${outbox.total_cost_usd:.4f} / ${harness.ledger.ceiling_usd:.2f}")
    print(f"Duration:          {outbox.duration_ms/1000:.1f}s")
    print(f"Checkpoints:       {outbox.checkpoints_passed} pass / {outbox.checkpoints_failed} fail")
    print(f"Alarms fired:      {len(outbox.alarms_fired)}")
    if outbox.alarms_fired:
        sev_count = {}
        for a in outbox.alarms_fired:
            sev_count[a["severity"]] = sev_count.get(a["severity"], 0) + 1
        print(f"  by severity:     {sev_count}")
    print(f"Awaiting human:    {outbox.awaiting_human}")
    if outbox.error:
        print(f"Error:             {outbox.error}")

    print()
    print(f"Run dir:           {harness.run_dir}")
    print(f"  checkpoints/     {len(list((harness.run_dir / 'checkpoints').glob('*.json')))} verdicts")
    if (harness.run_dir / "decisions.jsonl").exists():
        n = sum(1 for _ in (harness.run_dir / "decisions.jsonl").open(encoding="utf-8"))
        print(f"  decisions.jsonl  {n} entries")

    return 0 if outbox.success else 1


def _pick_worker(name: str):
    if name == "swap":
        from agents.swap_worker import SwapWorker
        return SwapWorker()
    from agents.claude_worker import ClaudeWorker
    return ClaudeWorker()


if __name__ == "__main__":
    sys.exit(main())
