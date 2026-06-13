"""Stage 2 — Deepgram nova-3 transcription with word-level timings.

Cost model: Deepgram nova-3 lists at ~$0.0043 per minute of audio. For an
8-min video, ≈ $0.034. We round-up-charge the ledger BEFORE calling the API
so a budget-exceeded run does not still spend on transcription.

The transcript text is sanitized via guardrails.input.sanitize_transcript_text
before being returned, so anything downstream that drops it into a prompt is
already clean.
"""
from __future__ import annotations

import os
import json
from pathlib import Path
from typing import Optional

import httpx
from opentelemetry import trace

from ..guardrails.input import sanitize_transcript_text
from ..observability.ledger import get_ledger
from ..observability.tracer import traced
from ..types import IngestResult, TranscribeResult, Word


_REPO_ROOT = Path(__file__).resolve().parents[2]
_CACHED_TRANSCRIPTS_DIR = _REPO_ROOT / "source_data" / "transcripts"

DEEPGRAM_URL = "https://api.deepgram.com/v1/listen"
DEEPGRAM_PARAMS = {
    "model": "nova-3",
    "smart_format": "true",
    "punctuate": "true",
    "diarize": "true",
    "language": "en",
    "filler_words": "false",
}
PRICE_PER_MINUTE_USD = 0.0043   # Deepgram nova-3 list price
REQUEST_TIMEOUT_S = 600.0


class TranscribeFailed(Exception):
    pass


@traced("transcribe")
def transcribe(ingest_result: IngestResult, *, job_id: str) -> TranscribeResult:
    span = trace.get_current_span()
    span.set_attribute("job_id", job_id)
    span.set_attribute("stage", "transcribe")
    span.set_attribute("video_id", ingest_result.video_id)
    span.set_attribute("duration_s", ingest_result.duration_s)

    # ---- cache check ----
    cached = _CACHED_TRANSCRIPTS_DIR / f"{ingest_result.video_id}.deepgram.json"
    if cached.exists() and cached.stat().st_size > 0:
        try:
            data = json.loads(cached.read_text(encoding="utf-8"))
            span.set_attribute("cache.hit", True)
            get_ledger().charge("transcribe", 0.0, source="cache-hit")
            return _parse_deepgram(data, duration_s=ingest_result.duration_s)
        except Exception:
            # Fall through to a real API call if the cache file is bad.
            span.set_attribute("cache.parse_failed", True)

    span.set_attribute("cache.hit", False)

    # Pre-charge the ledger. If we don't have budget, fail before the API call.
    estimated_cost = (ingest_result.duration_s / 60.0) * PRICE_PER_MINUTE_USD
    get_ledger().charge(
        "transcribe", estimated_cost,
        source="deepgram-nova-3", minutes=ingest_result.duration_s / 60.0,
    )
    span.set_attribute("cost_usd", estimated_cost)

    api_key = os.environ.get("DEEPGRAM_API_KEY")
    if not api_key:
        raise TranscribeFailed("DEEPGRAM_API_KEY not set")

    video_path = Path(ingest_result.video_path)
    if not video_path.exists():
        raise TranscribeFailed(f"video missing: {video_path}")

    headers = {
        "Authorization": f"Token {api_key}",
        "Content-Type": _content_type(video_path),
    }
    with video_path.open("rb") as f:
        resp = httpx.post(
            DEEPGRAM_URL,
            params=DEEPGRAM_PARAMS,
            headers=headers,
            content=f.read(),
            timeout=REQUEST_TIMEOUT_S,
        )
    if resp.status_code != 200:
        raise TranscribeFailed(f"deepgram {resp.status_code}: {resp.text[:300]}")

    data = resp.json()
    return _parse_deepgram(data, duration_s=ingest_result.duration_s)


def _parse_deepgram(data: dict, *, duration_s: float) -> TranscribeResult:
    try:
        alt = data["results"]["channels"][0]["alternatives"][0]
    except (KeyError, IndexError):
        raise TranscribeFailed("deepgram response missing alternatives")

    full_text = sanitize_transcript_text(alt.get("transcript", ""))
    raw_words = alt.get("words", []) or []
    words: list[Word] = []
    for w in raw_words:
        try:
            plain = sanitize_transcript_text(w.get("word", "") or w.get("punctuated_word", ""))
            # punctuated_word carries capitalization + trailing punctuation.
            # We keep it separate so captions can render the human-readable
            # form while LLM/matching code uses the plain `word`.
            punct = sanitize_transcript_text(
                w.get("punctuated_word") or w.get("word", "")
            ) or None
            words.append(Word(
                text=plain,
                punctuated=punct,
                start_s=float(w.get("start", 0.0)),
                end_s=float(w.get("end", 0.0)),
                confidence=float(w.get("confidence", 1.0)),
                speaker=int(w["speaker"]) if "speaker" in w and w["speaker"] is not None else None,
            ))
        except (TypeError, ValueError):
            continue

    return TranscribeResult(
        words=words,
        full_text=full_text,
        language="en",
        duration_s=duration_s,
    )


def _content_type(path: Path) -> str:
    return {
        "mp3": "audio/mpeg", "m4a": "audio/mp4", "mp4": "audio/mp4",
        "wav": "audio/wav", "ogg": "audio/ogg", "opus": "audio/ogg",
        "webm": "audio/webm", "flac": "audio/flac",
    }.get(path.suffix.lower().lstrip("."), "application/octet-stream")
