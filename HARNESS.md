# The acq_clipper Harness

A harness for an AI agent that turns long-form Acquisition.com interviews into
9:16 captioned shorts. Built for the Fired Festival 24-hour AI Harness build
challenge.

The model is a commodity. The harness is the load-bearing engineering —
guardrails, checkpoints, material handling, alarms, observability, limits.
Drop in a different worker and the constraint-handling is invisible to it.

---

## Anatomy

The harness is organized around the four pillars from the spec deck **and**
the four components from the secondary requirements. Each lives in its own
package under `harness/` so judges can grep for them by name.

```
harness/                                 # The Harness itself
  runtime.py                             # Harness object + setup() factory
  guardrails/__init__.py                 # DECLARED list of 13 guardrails
  checkpoints/                           # 6 explicit pass/fail evaluators
    evaluators.py                        # one per stage
    persistence.py                       # JSON snapshots for replay
    types.py                             # CheckpointVerdict, Criterion
  material/                              # The in/out box
    envelopes.py                         # Inbox, Outbox, ToolCallResult
  alarms/                                # 19 named alarm types
    types.py                             # Alarm, Severity, RecommendedAction, AlarmBus
    taxonomy.py                          # registered catalog of every alarm
    translation.py                       # exception -> Alarm mapping
  observability/__init__.py              # OTel spans + cost ledger
  limits.py                              # turn cap, wall-clock, retries
  escalation.py                          # human-in-the-loop pause/resume

agents/                                  # Workers — the AI agents
  base.py                                # Worker Protocol (structural)
  tool_wrapper.py                        # the single dispatch choke point
  tools_registry.py                      # 6 typed tool specs
  claude_worker.py                       # primary worker (Claude-driven)
  swap_worker.py                         # second worker (deterministic, for bonus)

pipeline/                                # Toolbelt — the typed stages
  stages/{ingest,transcribe,correct_captions,finalize}.py
  tools/registry.py                      # allow-listed callable registry
  tools/real_adapters.py                 # bindings to third-party packages
  types.py                               # Pydantic stage I/O contracts

processing/                              # Third-party tools (Python packages)
  find_moments/                          # picks Q&A clips from transcripts
  acq_clipper/                           # 9:16 crop with YOLO face detection
  yellow_captions/                       # burns word-level captions

scripts/
  run_harness.py                         # CLI entry: one URL -> one Outbox
  resume_harness.py                      # operator-resolved escalation resume
```

---

## The four pillars (deck)

### 1. Loop

The worker drives a multi-turn loop. The default `ClaudeWorker` runs a
deterministic happy-path traversal of the six stages, but every checkpoint
failure or alarm routes through `_decide(...)` — a structured-output Claude
call that picks between `retry_stage / retry_with_args / drop_clip /
escalate_human / abort_run`. That is the agent-behavior-changes-on-feedback
mechanism.

The loop is bounded by `harness/limits.py`:

| Bound                  | Default | Where checked                       |
|------------------------|---------|-------------------------------------|
| `max_turns`            | 32      | `Harness.check_limits()` each turn  |
| `max_wall_seconds`     | 600     | same                                |
| `max_retries_per_stage`| 2       | `_do_stage(attempt=...)`            |
| `cost_ceiling_usd`     | per-run | `CostLedger.charge()` (synchronous) |

When any limit fires, the worker emits a critical alarm and the run halts.

### 2. Tools

Six tools, each typed and registered exactly once:

| Tool name        | Stage label       | Cost source                       | Checkpoint           |
|------------------|-------------------|-----------------------------------|----------------------|
| `ingest`         | `ingest`          | ~$0 (yt-dlp local)                | `checkpoint_ingest`  |
| `transcribe`     | `transcribe`      | Deepgram nova-3 (~$0.034/8min)    | `checkpoint_transcribe` |
| `pick_timestamps`| `pick_timestamps` | heuristic (~$0)                   | `checkpoint_pick`    |
| `crop_video`     | `crop_video`      | CPU (YOLO + ffmpeg)               | `checkpoint_crop`    |
| `add_captions`   | `add_captions`    | CPU (ffmpeg burn-in)              | `checkpoint_captions`|
| `finalize`       | `finalize`        | Vercel Blob egress                | `checkpoint_finalize`|

All tool dispatch goes through `agents/tool_wrapper.invoke_tool(...)` — the
single choke point. There is no path to a tool that bypasses it. The wrapper:

1. Drains pre-existing alarms (so we attribute only new ones).
2. Calls the tool.
3. Translates any exception into a structured Alarm via the registry in
   `harness/alarms/translation.py` — errors come back as data, not crashes.
4. Runs the matching checkpoint and persists its verdict.
5. Bundles the result, verdict, and new alarms into a `ToolCallResult`.

### 3. Guardrails

Thirteen declared guardrails across four layers, listed in
`harness/guardrails/__init__.py::DECLARED`:

| Layer  | Count | Examples                                          |
|--------|-------|---------------------------------------------------|
| input  | 3     | `url_allow_list`, `transcript_injection_strip`, `transcript_size_cap` |
| action | 2     | `tool_allow_list`, `single_video_per_run`         |
| output | 4     | `clip_resolution_9_16`, `clip_duration_window`, `caption_drift_limit`, `loudness_in_spec` |
| limit  | 4     | `spend_ceiling`, `turn_cap`, `wall_clock_timeout`, `retries_per_stage` |

`DECLARED` is the single source of truth. Every run writes
`pipeline_runs/<job_id>/guardrails.json` so the operator can audit which
constraints were in force.

### 4. Observability

- OpenTelemetry spans per stage and per tool call (`harness.observability.span`).
- In-memory recorder for the local dashboard; OTLP HTTP exporter when
  `OTEL_EXPORTER_OTLP_ENDPOINT` is set — no lock-in.
- Cost ledger (`CostLedger`) charges every billable op and **synchronously
  raises `BudgetExceeded`** when a charge would breach the per-run ceiling.
  The exception is also translated to a critical alarm.
- Every run produces a structured artifact set:

```
pipeline_runs/<job_id>/
  inbox.json
  outbox.json
  alarms.json
  ledger.json
  guardrails.json
  decisions.jsonl
  checkpoints/
    01_ingest.json
    02_transcribe.json
    03_pick_timestamps.json
    04_crop_video_<clip_id>.json
    05_add_captions_<clip_id>.json
    06_finalize_<clip_id>.json
  awaiting_human.json        (only present when escalated)
  decision.json              (only present when operator has responded)
```

---

## The four secondary components

### Guardrails (declared, not implicit)

See `harness/guardrails/__init__.py::DECLARED`. Adding a guardrail is
appending one `Guardrail(name, layer, description)`. The harness writes the
declared list to `guardrails.json` on every run.

### Checkpoints (explicit pass/fail, persisted)

Each stage has exactly one checkpoint function in
`harness/checkpoints/evaluators.py`:

```python
checkpoint_ingest(IngestResult)          -> CheckpointVerdict
checkpoint_transcribe(TranscribeResult)  -> CheckpointVerdict
checkpoint_pick(PickTimestampsResult)    -> CheckpointVerdict
checkpoint_crop(CropResult)              -> CheckpointVerdict
checkpoint_captions(CaptionResult)       -> CheckpointVerdict
checkpoint_finalize(FinalClip, ...)      -> CheckpointVerdict
```

A `CheckpointVerdict` is a tuple of named criteria, each with `passed`,
`actual`, `expected`. Verdicts are written as JSON under
`checkpoints/<NN>_<stage>[_<clip_id>].json` so replay-from-checkpoint
becomes "read the verdict that failed and re-invoke from there." The
ingest and transcribe stages already cache on disk by `video_id`, so a
resumed run reuses their artifacts without re-downloading or re-billing.

### Material handling (typed in/out)

The harness's only entry point is an `Inbox`; its only output is an `Outbox`.
Both live in `harness/material/envelopes.py`. Between tools, every result
is wrapped in a `ToolCallResult` — that is how errors arrive as data the
worker can react to:

```python
class ToolCallResult(BaseModel):
    tool_name: str
    ok: bool
    output: Optional[dict]
    error: Optional[str]
    checkpoint: Optional[dict]
    new_alarms: list[dict]
    duration_ms: int
    cost_usd: float
```

The worker reads `new_alarms` and `checkpoint`, decides via `_decide(...)`,
and continues. There is no path where a worker sees a tool exception
directly.

### Alarms (structured, named, severity, action)

Nineteen named alarm types in `harness/alarms/taxonomy.py`. Every alarm
fires onto the per-run `AlarmBus` with:

```python
class Alarm:
    name: str                           # e.g. "NoFacesDetected"
    severity: Severity                  # info / warning / error / critical
    context: dict                       # arbitrary structured detail
    recommended_action: RecommendedAction   # closed enum: continue / retry_stage /
                                            # retry_with_args / drop_clip /
                                            # escalate_human / abort_run
    stage: Optional[str]
    clip_id: Optional[str]
    fired_at: float
    message: Optional[str]
```

`recommended_action` is a closed enum on purpose — leaving it open lets a
broken worker invent unsafe actions. The taxonomy at
`harness/alarms/taxonomy.py::TAXONOMY` documents every alarm the harness
can emit.

---

## Worker Protocol (swappable)

```python
@runtime_checkable
class Worker(Protocol):
    name: str
    version: str
    def run(self, harness: Harness, inbox: Inbox) -> Outbox: ...
```

Two implementations ship today:

- **`agents.claude_worker.ClaudeWorker`** — primary. Drives a deterministic
  happy-path traversal but consults Claude Haiku 4.5 for decisions whenever
  a checkpoint fails or an alarm fires. Decisions are persisted to
  `decisions.jsonl` with reasoning strings.

- **`agents.swap_worker.SwapWorker`** — second worker for the demo bonus.
  Same Protocol, no LLM. Picks the highest-severity alarm's
  `recommended_action`. Demonstrates the deck's claim that the harness is
  the durable engineering: swap the brain, same constraints, same artifacts.

Select via `--worker claude|swap` on `run_harness.py`. Adding a third
worker (e.g. an OpenAI variant) is a single new file conforming to the
Protocol — no harness changes required.

---

## Human-in-the-loop escalation

A worker calls `harness.request_human(reason, context)`, which writes
`pipeline_runs/<job_id>/awaiting_human.json` and stops the run. The
operator inspects the run, writes a `decision.json` with shape
`{"action": "continue|retry|drop_clip|abort", "notes": "..."}`, then runs:

```bash
python -m scripts.resume_harness <job_id>
```

The resume CLI reads the decision and re-invokes the worker. Cached
artifacts (`source_data/videos/<vid>.mp4`,
`source_data/transcripts/<vid>.deepgram.json`) make the resume cheap —
ingest and transcribe hit the cache and the run continues from the failure.

---

## Running it

### Local CLI

```bash
# primary worker
python -m scripts.run_harness "https://www.youtube.com/watch?v=<id>"

# swap in the deterministic worker
python -m scripts.run_harness "https://www.youtube.com/watch?v=<id>" --worker swap

# resume an escalated run
python -m scripts.resume_harness <job_id>
```

### Local dashboard

```bash
python -m scripts.dashboard_serve   # http://localhost:3022/eval
```

Lists every run under `pipeline_runs/` with cost, alarms fired, checkpoint
pass/fail, and inline video preview of accepted clips.

### Deployed (Vercel)

The harness ships as a Vercel Python function. Endpoint:

```
POST /api/run
Body: {"url": "<youtube url>"}
```

Returns the same `Outbox` shape as the CLI.

---

## What this harness is NOT doing (and why)

- **Open-ended tool-use loop.** Claude does not pick which tool to call next on
  the happy path — the pipeline order is deterministic. Claude is consulted
  *only* when a checkpoint or alarm signals trouble. This is intentional:
  it keeps cost predictable on correct runs and concentrates LLM decisions
  exactly where they matter.

- **Streaming/multi-clip parallelism.** The MVP processes one clip per run.
  `target_clip_count` in the Inbox is the seam for adding more.

- **Live LLM-as-judge scoring.** Quality scoring is in scope for a future
  pass; for now the QC verdict comes from explicit pass/fail criteria, not
  model preference.

---

## Recovery / rollback

Every run is reproducible from its on-disk artifacts. To roll the entire
codebase back to the pre-hackathon deterministic pipeline:

```bash
git checkout baseline-v1
```

`pipeline_runs.gold/` holds the gold output snapshot for the 5-video batch
that was passing before the refactor. After a refactor, manifest-diff against
the gold dir to confirm no regression:

```bash
diff pipeline_runs/<id>/manifest.json pipeline_runs.gold/<id>/manifest.json
```
