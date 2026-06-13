"""Guardrails — declared, not implicit.

This module is the single source of truth for what guardrails this harness
enforces. Judges (and the worker) consult `DECLARED` to know what's in
force.

Three layers (per the deck):
    input   — what the worker is allowed to receive
    action  — which tools the worker is allowed to call
    output  — what's allowed to ship to the user

Plus hard limits (see harness.limits).

The implementations live in `pipeline.guardrails` and `pipeline.tools.registry`
because they predate this refactor; this module re-exports them so the harness
namespace is the canonical surface area.
"""
from __future__ import annotations

from dataclasses import dataclass

from pipeline.guardrails.input import (
    InputGuardrailFailed,
    sanitize_transcript_text,
    validate_url,
)
from pipeline.guardrails.output import (
    MAX_CAPTION_DRIFT_MS,
    MAX_DURATION_S,
    MIN_DURATION_S,
    QCFailed,
    TARGET_HEIGHT,
    TARGET_LUFS,
    TARGET_WIDTH,
    qc_clip,
    qc_run,
)


@dataclass(frozen=True)
class Guardrail:
    """One declared guardrail. Listed in DECLARED below."""
    name: str
    layer: str  # "input" | "action" | "output" | "limit"
    description: str


DECLARED: tuple[Guardrail, ...] = (
    # ---------- input ----------
    Guardrail(
        name="url_allow_list",
        layer="input",
        description="Reject non-YouTube hosts, playlists, livestream URLs. "
                    "Canonicalize to https://www.youtube.com/watch?v=<id>.",
    ),
    Guardrail(
        name="transcript_injection_strip",
        layer="input",
        description="Replace prompt-injection markers (ignore previous "
                    "instructions, fake system/assistant tags, [INST], "
                    "<|im_start|>) with [redacted] before transcript reaches "
                    "any LLM-driven stage.",
    ),
    Guardrail(
        name="transcript_size_cap",
        layer="input",
        description="Truncate transcript text to 200,000 chars to bound "
                    "LLM prompt size and cost.",
    ),
    # ---------- action ----------
    Guardrail(
        name="tool_allow_list",
        layer="action",
        description="Worker may only call tools registered in "
                    "pipeline.tools.registry. Unknown tool names are rejected "
                    "as 'unknown tool' errors-as-data — they do not crash.",
    ),
    Guardrail(
        name="single_video_per_run",
        layer="action",
        description="Each job processes exactly one source URL. Playlist "
                    "expansion is explicitly disallowed at input guardrail.",
    ),
    # ---------- output ----------
    Guardrail(
        name="clip_resolution_9_16",
        layer="output",
        description="Final clip must be 1080x1920 (true 9:16). QC fails if "
                    "ffprobe reports otherwise.",
    ),
    Guardrail(
        name="clip_duration_window",
        layer="output",
        description=f"Final clip duration in [{MIN_DURATION_S:.0f}, "
                    f"{MAX_DURATION_S:.0f}]s. Outside window fails QC.",
    ),
    Guardrail(
        name="caption_drift_limit",
        layer="output",
        description=f"Caption-to-audio drift must stay under "
                    f"{MAX_CAPTION_DRIFT_MS:.0f}ms (half a typical Deepgram word).",
    ),
    Guardrail(
        name="loudness_in_spec",
        layer="output",
        description=f"Integrated loudness within ±3 LU of {TARGET_LUFS:.0f} LUFS "
                    f"(YouTube Shorts / TikTok target).",
    ),
    # ---------- limit ----------
    Guardrail(
        name="spend_ceiling",
        layer="limit",
        description="Hard $/run ceiling enforced synchronously by the cost "
                    "ledger. Charge attempts above ceiling raise "
                    "BudgetExceeded — the run halts.",
    ),
    Guardrail(
        name="turn_cap",
        layer="limit",
        description="Worker loop is capped at MAX_TURNS iterations. "
                    "A confused agent cannot spin forever.",
    ),
    Guardrail(
        name="wall_clock_timeout",
        layer="limit",
        description="Total run wall-clock bounded. Exceeding raises "
                    "WallClockExceeded.",
    ),
    Guardrail(
        name="retries_per_stage",
        layer="limit",
        description="Each stage retries at most N times before the harness "
                    "drops the clip or escalates to human.",
    ),
)


def by_layer(layer: str) -> tuple[Guardrail, ...]:
    return tuple(g for g in DECLARED if g.layer == layer)


__all__ = [
    "DECLARED",
    "Guardrail",
    "InputGuardrailFailed",
    "QCFailed",
    "by_layer",
    "qc_clip",
    "qc_run",
    "sanitize_transcript_text",
    "validate_url",
]
