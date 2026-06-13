"""Runtime config — env-backed, validated once at boot."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Optional


def _get(name: str, default: Optional[str] = None, *, required: bool = False) -> Optional[str]:
    v = os.environ.get(name)
    if v is None or v == "":
        if required:
            raise RuntimeError(f"missing required env var: {name}")
        return default
    return v


def _get_float(name: str, default: float) -> float:
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    return float(v)


def _get_int(name: str, default: int) -> int:
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    return int(v)


@dataclass(frozen=True)
class Config:
    # --- secrets ---
    anthropic_api_key: str
    deepgram_api_key: str
    blob_token: str
    redis_url: str
    redis_token: str

    # --- budgets (USD per video) ---
    cost_target_usd: float
    cost_ceiling_usd: float

    # --- hard limits ---
    max_turns: int
    max_wall_seconds: int
    max_retries_per_stage: int

    # --- model selection ---
    claude_model: str

    # --- observability ---
    otel_endpoint: Optional[str]
    otel_headers: Optional[dict]


def load_config() -> Config:
    """Read env once, fail fast if anything required is missing."""
    otel_headers_raw = _get("OTEL_EXPORTER_OTLP_HEADERS")
    otel_headers = None
    if otel_headers_raw:
        try:
            otel_headers = dict(
                kv.split("=", 1) for kv in otel_headers_raw.split(",") if "=" in kv
            )
        except Exception:
            otel_headers = None

    return Config(
        anthropic_api_key=_get("ANTHROPIC_API_KEY", required=True) or "",
        deepgram_api_key=_get("DEEPGRAM_API_KEY", required=True) or "",
        blob_token=_get("BLOB_READ_WRITE_TOKEN", default="") or "",
        redis_url=_get("UPSTASH_REDIS_REST_URL", default="") or "",
        redis_token=_get("UPSTASH_REDIS_REST_TOKEN", default="") or "",
        cost_target_usd=_get_float("PIPELINE_VIDEO_COST_TARGET", 0.20),
        cost_ceiling_usd=_get_float("PIPELINE_VIDEO_COST_CEILING", 1.00),
        max_turns=_get_int("PIPELINE_MAX_TURNS", 8),
        max_wall_seconds=_get_int("PIPELINE_MAX_WALL_SECONDS", 720),
        max_retries_per_stage=_get_int("PIPELINE_MAX_RETRIES_PER_STAGE", 2),
        claude_model=_get("CLAUDE_MODEL", default="claude-haiku-4-5-20251001") or "claude-haiku-4-5-20251001",
        otel_endpoint=_get("OTEL_EXPORTER_OTLP_ENDPOINT"),
        otel_headers=otel_headers,
    )
