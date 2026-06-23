# ACQ Clipper

**Make long-to-short form content 100x cheaper.**

An autonomous pipeline that does the job of a full-time editor — scene selection, cropping, captioning, and quality control — for pennies per clip. Measured average: **$0.054 per finished short**, **~131 seconds wall time**, fully unattended.

> A team of editors costs tens of thousands of dollars a year. This pipeline does the same work for pennies per clip. If a pipeline like this doesn't save money, it isn't worth building. Saving money is the whole point.

[Live demo →](https://acq-clipper.vercel.app)

---

## What it does

Point it at a long-form YouTube video. Get back a finished, captioned, vertically-cropped short.

The pipeline runs the entire loop end-to-end without a human:

```
ingest → transcribe → pick_timestamps → crop → correct_captions → add_captions → finalize
```

Every stage is a checkpoint. When a stage fails or trips a typed alarm, the pipeline picks the recommended action from a closed set: `retry_stage`, `retry_with_adjusted_args`, `drop_clip`, `escalate_human`, or `abort_run`. The decision logic is deterministic — there is no LLM in the loop driver. This was a deliberate choice: a deterministic control path performed better in practice and avoided the unnecessary complexity and cost of putting a model in the loop just to make routing decisions a few rules can make reliably. The harness governs every move so nothing goes off the rails silently.

## The tools — built in-house

The hard parts aren't outsourced to a black-box SaaS. Each tool was built and tuned for this pipeline.

| Tool | What it does |
|---|---|
| **YOLOv8 face cropper** | Detects the speaker's face frame-by-frame and centers a vertical 1080×1920 crop on it — no static crop, no jump cuts mid-sentence. |
| **Heuristic scene picker** | Reads the transcript and picks the highest-impact moment, 15 to 150 seconds long. A deterministic heuristic — no LLM call, no per-clip token cost. |
| **Deepgram Nova-3 transcription** | Word-level timestamps from Deepgram Nova-3. Feeds scene picking and the caption burn-in step. |
| **libass + ffmpeg caption burn-in** | Renders the corrected transcript as burnt-in yellow captions sized for mobile. Done in ffmpeg via the libass subtitles filter; not by Deepgram. |

Claude Haiku 4.5 is also called once per clip for caption-text correction (fixing transcription mistakes word-by-word before burn-in). That's the only LLM in the per-clip path; the rest of the pipeline — including every retry/drop/abort decision — runs fully deterministically.

## Guardrails — the part that makes it worth running

Validation isn't graded after the fact. It's enforced in code, in advance. Before any clip ships, it goes through a **QC gate of 8 checks** against the finished mp4 — anchored to direct ffprobe and ffmpeg ebur128 reads, not to the tools that produced the clip. A single failure drops it. After the run, **3 more run-level checks** grade the whole job before it's marked done. Across the pipeline there are **19 named alarms** the harness can raise, each tied to a recommended action.

Three layers, in priority order:

1. **Input sanitation.** URLs are validated against an allow-list before anything runs. Transcript text is stripped of suspicious patterns before it's fed into any LLM prompt — defends against prompt injection from hostile source content.
2. **Spending limits.** A hard ceiling per run (default $1.00) and a target ($0.20). The cost ledger projects every charge in real-time, and the harness aborts the run the instant the ceiling is in reach. Running this over a terabyte of footage at a company like Acquisition.com is the difference between "the pipeline ran" and "the pipeline ran us into the ground." This is the guardrail that makes that safe.
3. **Output quality checks.** Every finished clip is graded by an output guardrail with explicit criteria: exactly **1080×1920**, duration in **[15, 150] seconds**, face centered, source crop quality not `poor` (`good` / `acceptable` / `soft` all pass), caption sync drift under **250 ms**. Anything that misses any check is rejected. No bad clips slip through to the published feed.

These exist because the moment a pipeline stops saving money, it isn't worth running. Guardrails are what keep the economics working at scale.

## Observability

Two operators are watching every run:

- **OpenTelemetry tracing.** Every stage emits a span with timing, cost, and outcome. Spans export over OTLP — pipe them to Honeycomb, Datadog, or anything else. You can see exactly which step ran, how long it took, and where the budget went.
- **Cost ledger.** Every LLM call, every Deepgram second, every YOLO inference is logged with stage, amount, and source. The harness reads the ledger before deciding to continue. When you're running this over a terabyte of media, this is the only thing standing between "we got 50,000 clips today" and "we got a $20,000 OpenAI bill today."

## Failure recovery

A pipeline that nobody can see is a pipeline that doesn't work. Three layers make sure that every kind of failure leaves a paged human, not a stuck spinner.

**1. Typed alarms inside the run.** Every guardrail, checkpoint, and tool fires named alarms with a severity (info / warning / error / critical) and a recommended action (`retry_stage`, `drop_clip`, `escalate_human`, `abort_run`). 19 alarm names are pre-registered in `harness/alarms/taxonomy.py` so no stage can invent its own.

**2. Webhook on error + critical.** Every `error` or `critical` alarm posts to `ACQ_ALARM_WEBHOOK_URL` in the background. **Slack**, **Discord**, or **PagerDuty Events v2** are auto-detected by URL. Payload includes alarm name, stage, recommended action, and context. No webhook configured = silent no-op; the pipeline never blocks on the call.

**3. Heartbeat from Vercel cron.** `/api/heartbeat` runs every 5 minutes from the edge, probes the tunnel + runner, and fires the same webhook if anything is wrong: tunnel unreachable, secret unset, or blob token missing. Catches outages the per-run alarms can't see — because no one is running anything.

### Surfacing to humans during a run

When a run is in progress, the pipeline's stage progress is rendered as speech bubbles from the editor character on the homepage. A non-technical user can watch a run and understand it:

- "Pulling the video from YouTube…"
- "Transcribing with Deepgram…"
- "Picking the strongest moment with Haiku…"
- "Tracking faces with YOLO…"
- "Cropping to 9:16 vertical…"
- "Burning in yellow captions…"
- "Finalizing and uploading…"

No log files. No JSON dumps. Just the editor telling you what it's doing.

## Cost

Real measurements from 28 actual runs: the pipeline averages **$0.054 per finished short**, end-to-end, with **~131 seconds wall time** per video. A freelance editor producing the equivalent short from a long-form interview charges between **$40 and $150** depending on turnaround. At volume, the gap is roughly **1,000× to 3,000×** on direct unit cost.

Where each $0.054 goes:

| Stage | Cost |
|---|---:|
| YOLOv8 face crop (compute) | $0.0148 |
| Caption correction (Claude Haiku 4.5) | $0.0113 |
| Caption burn-in (ffmpeg + libass) | $0.0100 |
| Transcribe (Deepgram Nova-3) | $0.0085 |
| Finalize + upload (Vercel Blob) | $0.0030 |
| Pick best moment (heuristic, no LLM) | $0.0001 |
| Download (yt-dlp) + ingest | $0.0000 |
| **Total per clip (averaged)** | **$0.054** |

Scaling is sub-linear — same code path, more compute when needed.

## Try it

**Live:** https://acq-clipper.vercel.app — paste a YouTube URL, watch the editor work, download the clip.

**Local CLI:**

```bash
pip install -r requirements.txt
python -m scripts.run_harness "https://www.youtube.com/watch?v=..."
```

Requires `.env.local` with `ANTHROPIC_API_KEY` and `DEEPGRAM_API_KEY`. Optional: Upstash Redis + Vercel Blob keys for hosted runs with live progress streaming, and `ACQ_ALARM_WEBHOOK_URL` to wire up the failure-recovery webhook.

## Repository layout

```
api/             — Vercel Python functions (process, status, heartbeat)
app/             — Next.js shell + API route handlers; redirects / to /index.html
public/          — The actual frontend: hero, validation, failure-recovery, cost
                   sections, animated editor character, live speech bubbles
agents/          — ClaudeWorker (LLM-driven) and SwapWorker (deterministic)
harness/         — Loop governance: alarms (with webhook notifier), guardrails,
                   and limits; re-exports the cost ledger + tracer from
                   pipeline/observability as the canonical harness surface
pipeline/        — Deterministic pipeline driver + stage implementations.
                   pipeline/stages/ holds ingest, transcribe, correct_captions,
                   and finalize; pick_timestamps, crop, and add_captions are
                   dispatched as tools (pipeline/tools/ → external tool packages)
(external tools) — Not vendored in this repo: acq_clipper (YOLO face cropper,
                   its own ML deps — opencv, numpy, onnxruntime), yellow_captions
                   (burn-in), and find_moments (scene picker) are in-house tool
                   packages the pipeline imports lazily, not a local directory
homebox/         — Local pipeline runner + ngrok tunnel scripts + batch tooling
                   for the box that backs the live site
scripts/         — CLI entrypoints (run_harness.py, resume_harness.py)
```

## Next steps: hardening observability

The failure-recovery story is solid, but the cron heartbeat is not yet at its
full design. Planned hardening:

- Today the Vercel-cron heartbeat probes the tunnel/runner, the shared secret,
  and the blob token, and pages on any of those.
- Enforce the "busy too long" check (`ACQ_HEARTBEAT_MAX_BUSY_MIN`) — the
  heartbeat reads this threshold but does not yet act on it.
- Add a proposed "no successful run in N hours" staleness check — a new
  threshold that is not yet implemented (no env var or logic exists for it).
- Route both conditions to the same `ACQ_ALARM_WEBHOOK_URL` alarm webhook the
  other checks already use, so a stalled box pages on-call.

## License

MIT.
