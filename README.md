# ACQ Clipper

**An autonomous AI agent that does the job of a full-time editor — scene selection, cropping, captioning, and quality control — for pennies per clip.**

> A team of editors costs tens of thousands of dollars a year. This pipeline does the same work for pennies per clip. If a pipeline like this doesn't save money, it isn't worth building. Saving money is the whole point.

[Live demo →](https://acq-clipper.vercel.app)

---

## What it does

Point it at a long-form YouTube video. Get back a finished, captioned, vertically-cropped short.

The agent runs the entire loop end-to-end without a human:

```
ingest → transcribe → pick_timestamps → crop → correct_captions → add_captions → finalize
```

Every stage is a checkpoint. When a stage fails or trips an alarm, the agent decides what to do next — retry with new params, drop the clip, or abort the whole run. The harness governs every decision so the loop can't go off the rails.

## The tools — built in-house

The hard parts aren't outsourced to a black-box SaaS. Each tool was built and tuned for this pipeline.

| Tool | What it does |
|---|---|
| **YOLO body cropper** | Detects the speaker's body frame-by-frame and centers a vertical 9:16 crop on it — no static crop, no jump cuts mid-sentence. |
| **Haiku scene picker** | Reads the transcript and picks the highest-impact 30-second moment. Claude Haiku 4.5 — cheap, fast, smart enough to pick well. |
| **Deepgram auto-captioner** | Word-level timestamps from Deepgram, then burnt into the video as yellow captions sized for mobile. |

Haiku is also the orchestrator. It makes every recovery decision on a failed checkpoint, at roughly a tenth of a cent per call. Cheap enough to use everywhere; smart enough that the loop actually adapts.

## Guardrails — the part that makes it worth running

Three layers, in priority order:

1. **Input sanitation.** URLs are validated against an allow-list before anything runs. Transcript text is stripped of suspicious patterns before it's fed into any LLM prompt — defends against prompt injection from hostile source content.
2. **Spending limits.** A hard ceiling per run (default $1.00) and a target ($0.20). The cost ledger projects every charge in real-time, and the harness aborts the run the instant the ceiling is in reach. Running this over a terabyte of footage at a company like Acquisition.com is the difference between "the pipeline ran" and "the pipeline ran us into the ground." This is the guardrail that makes that safe.
3. **Output quality checks.** Every finished clip is graded by an output guardrail. Anything below `acceptable` is rejected and the clip is dropped. No bad clips slip through to the published feed.

These exist because the moment a pipeline stops saving money, it isn't worth running. Guardrails are what keep the economics working at scale.

## Observability

Two operators are watching every run:

- **OpenTelemetry tracing.** Every stage emits a span with timing, cost, and outcome. Spans export over OTLP — pipe them to Honeycomb, Datadog, or anything else. You can see exactly which step ran, how long it took, and where the budget went.
- **Cost ledger.** Every LLM call, every Deepgram second, every YOLO inference is logged with stage, amount, and source. The harness reads the ledger before deciding to continue. When you're running this over a terabyte of media, this is the only thing standing between "we got 50,000 clips today" and "we got a $20,000 OpenAI bill today."

## Alarms surfaced to humans — not buried in logs

When something goes wrong, the agent's reasoning shouldn't disappear into a log file no PM will ever read.

Every alarm fires through a typed bus with a severity and a recommended action. The frontend reads these in real-time and renders them as **speech bubbles from the editor character on the homepage** — plain English explanations of what's happening, what failed, and what the agent is about to do about it.

A non-technical user can watch a run and understand it:

- "Got the transcript via Deepgram."
- "Cropping landed at quality=acceptable — continuing."
- "Source video too long — aborting run."

No log files. No JSON dumps. Just the agent telling you what it's doing.

## Try it

**Live:** https://acq-clipper.vercel.app — paste a YouTube URL, watch the editor work, download the clip.

**Local CLI:**

```bash
pip install -r requirements.txt
python -m scripts.run_harness "https://www.youtube.com/watch?v=..."
```

Requires `.env.local` with `ANTHROPIC_API_KEY` and `DEEPGRAM_API_KEY`. Optional: Upstash Redis + Vercel Blob keys for hosted runs with live progress streaming.

## Repository layout

```
api/             — Vercel Python functions (process, status)
app/             — Next.js frontend (editor character + speech bubbles)
public/          — Static HTML demo site
agents/          — ClaudeWorker (LLM-driven) and SwapWorker (deterministic)
harness/         — The loop governance: alarms, guardrails, limits, ledger, tracing
pipeline/        — Stage implementations: ingest, transcribe, crop, captions, finalize
scripts/         — CLI entrypoints (run_harness.py, resume_harness.py)
```

## License

MIT.
