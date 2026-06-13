"""Local CLI runner for the pipeline.

Walks a list of YouTube URLs, runs the orchestrator on each (cache-aware,
so we skip ingest + transcribe when source_data already has them), and
writes a per-job manifest under pipeline_runs/<job_id>/.

Usage:
    python scripts/local_run.py [--urls URLS_FILE] [--count 5] [--dry-run]

Defaults to the first 5 URLs from source_data/urls.txt.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _load_env_local() -> None:
    """Read .env.local into os.environ. Manual parser so we don't require
    python-dotenv as a hard dep. Existing env vars win — `export X=Y` in the
    shell still overrides the file.
    """
    env_path = _REPO_ROOT / ".env.local"
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        # Skip empty values so the setdefault fallbacks below still kick in.
        if k and v and k not in os.environ:
            os.environ[k] = v


_load_env_local()

# Fallbacks for anything still missing — cache hits skip Deepgram/Anthropic,
# but `correct_captions` will fall back gracefully if the key is invalid.
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-local-noop")
os.environ.setdefault("DEEPGRAM_API_KEY", "deepgram-local-noop")
os.environ.setdefault("PIPELINE_MAX_WALL_SECONDS", "1200")  # 20 min per video safety net

RUNS_DIR = _REPO_ROOT / "pipeline_runs"


def main() -> int:
    parser = argparse.ArgumentParser(description="run the clipper pipeline locally on N URLs")
    parser.add_argument("--urls", type=Path,
                        default=_REPO_ROOT / "source_data" / "urls.txt",
                        help="file with one URL per line")
    parser.add_argument("--count", type=int, default=5,
                        help="how many URLs from the file to process (default 5)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the plan and exit without running")
    args = parser.parse_args()

    urls = [u.strip() for u in args.urls.read_text(encoding="utf-8").splitlines()
            if u.strip() and not u.startswith("#")][:args.count]
    if not urls:
        print(f"no URLs to process in {args.urls}", file=sys.stderr)
        return 2

    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    os.environ["CLIPS_OUTPUT_DIR"] = str(RUNS_DIR)

    print(f"-- runs dir: {RUNS_DIR}")
    print(f"-- urls    : {len(urls)}")
    for u in urls:
        print(f"     {u}")
    if args.dry_run:
        return 0

    # Import after env is set so config picks up our values.
    from pipeline.orchestrator import Orchestrator
    from pipeline.tools.real_adapters import install_real_adapters
    from pipeline.tools.registry import install_default_stubs

    install_default_stubs()      # baseline
    install_real_adapters()      # swap in the real tools

    orch = Orchestrator()
    runs_meta: list[dict] = []

    for i, url in enumerate(urls):
        t0 = time.perf_counter()
        job_id = f"job_{i:02d}_{int(time.time())}"
        print(f"\n[{i+1}/{len(urls)}] {url}  ->{job_id}")
        try:
            result = orch.run(url=url, job_id=job_id)
            elapsed = time.perf_counter() - t0
            print(
                f"   done  cost=${result.total_cost_usd:.4f}  "
                f"clips={len(result.clips)}  "
                f"elapsed={elapsed:.1f}s"
            )
            runs_meta.append(_persist_run(job_id, url, result, elapsed=elapsed, error=None))
        except Exception as e:
            elapsed = time.perf_counter() - t0
            print(f"   FAIL  {type(e).__name__}: {e}  elapsed={elapsed:.1f}s")
            runs_meta.append(_persist_run(job_id, url, result=None, elapsed=elapsed, error=str(e)))

    manifest = {
        "generated_at": int(time.time()),
        "runs": runs_meta,
    }
    (RUNS_DIR / "manifest.json").write_text(
        json.dumps(manifest, indent=2, default=str), encoding="utf-8"
    )
    print(f"\nManifest: {RUNS_DIR / 'manifest.json'}")
    print(f"Dashboard: python scripts/dashboard_serve.py  ->http://localhost:3002/eval")
    return 0


def _persist_run(job_id: str, url: str, result, *, elapsed: float, error: str | None) -> dict:
    """Dump per-run JSON files and return a manifest row."""
    from pipeline.state import get_store

    run_dir = RUNS_DIR / job_id
    run_dir.mkdir(parents=True, exist_ok=True)

    store = get_store()
    state = store.load_job(job_id)
    trace_spans = store.read_trace(job_id)
    ledger = store.read_ledger(job_id)

    if state is not None:
        (run_dir / "state.json").write_text(
            state.model_dump_json(indent=2), encoding="utf-8"
        )
    (run_dir / "trace.json").write_text(
        json.dumps(trace_spans, indent=2, default=str), encoding="utf-8"
    )
    (run_dir / "ledger.json").write_text(
        json.dumps(ledger, indent=2, default=str), encoding="utf-8"
    )

    if result is not None:
        (run_dir / "result.json").write_text(
            result.model_dump_json(indent=2), encoding="utf-8"
        )

    return {
        "job_id": job_id,
        "url": url,
        "status": (state.status.value if state else "unknown") if state else "unknown",
        "video_id": result.video_id if result else None,
        "total_cost_usd": result.total_cost_usd if result else 0.0,
        "n_clips": len(result.clips) if result else 0,
        "n_qc_passed": sum(1 for c in (result.clips if result else []) if c.qc_passed),
        "elapsed_s": elapsed,
        "error": error,
        "clips": [
            {
                "clip_id": c.clip_id,
                "blob_url": c.blob_url,
                "duration_s": c.duration_s,
                "size_bytes": c.size_bytes,
                "qc_passed": c.qc_passed,
                "qc_failures": c.qc_failures,
            }
            for c in (result.clips if result else [])
        ],
    }


if __name__ == "__main__":
    raise SystemExit(main())
