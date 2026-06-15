"""Single-URL runner for the deterministic baseline-v1 pipeline.

Usage:
    python run_one.py <youtube_url> [job_id]

Prints a JSON line at the end with clip blob URLs.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
PROC = REPO / "processing"
if PROC.exists() and str(PROC) not in sys.path:
    sys.path.insert(0, str(PROC))


def _load_env(p: Path) -> None:
    if not p.exists():
        return
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and v and k not in os.environ:
            os.environ[k] = v


_load_env(REPO / ".env.local")
os.environ.setdefault("PIPELINE_MAX_WALL_SECONDS", "1500")
# Clips land under pipeline_runs/<job_id>/ unless overridden.
os.environ.setdefault("CLIPS_OUTPUT_DIR", str(REPO / "pipeline_runs"))


from pipeline.tools.real_adapters import install_real_adapters  # noqa: E402
install_real_adapters()

from pipeline.orchestrator import Orchestrator  # noqa: E402


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: run_one.py <url> [job_id]", file=sys.stderr)
        return 2
    url = sys.argv[1]
    job_id = sys.argv[2] if len(sys.argv) > 2 else None
    orch = Orchestrator()
    result = orch.run(url=url, job_id=job_id)

    clips = []
    for c in result.clips:
        clips.append({
            "clip_id": getattr(c, "clip_id", None),
            "local_path": getattr(c, "local_path", None) or getattr(c, "output_path", None),
            "blob_url": getattr(c, "blob_url", None) or getattr(c, "url", None),
            "duration_s": getattr(c, "duration_s", None),
            "width": getattr(c, "width", None),
            "height": getattr(c, "height", None),
        })
    out = {
        "ok": True,
        "job_id": result.job_id,
        "video_id": getattr(result, "video_id", None),
        "clips": clips,
        "total_cost_usd": getattr(result, "total_cost_usd", None),
        "duration_ms": getattr(result, "duration_ms", None),
    }
    print("---RESULT---")
    print(json.dumps(out, indent=2))
    return 0 if clips else 1


if __name__ == "__main__":
    sys.exit(main())
