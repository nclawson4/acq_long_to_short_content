"""Per-clip caption-text correction with Claude Haiku 4.5.

The orchestrator runs this between subsetting transcript words to the clip
window and dispatching the captioner. It returns a new list of `Word`s with
the same length and timings as the input — only `text` and `punctuated` may
have been edited. Timing is preserved by index so downstream caption splitting
+ burn-in stays aligned with the audio.

Failure semantics (per the harness deck):
    LLM unavailable / response malformed / parsing fails / budget breach
    → return the input words unchanged. The span tags `correct_captions.
    fallback=true` so it's visible. We do NOT raise on these because caption
    correction is a quality improvement, not a hard requirement; the run
    should still ship the clip.

Cost: ~$0.001-0.005 per 30s clip on Haiku 4.5. Prompt cached on the system
block so per-call cost drops to nearly free after the first invocation.

Disable via env var: PIPELINE_CORRECT_CAPTIONS=0.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Optional

from opentelemetry import trace

from ..llm import call_claude
from ..observability.ledger import BudgetExceeded, get_ledger
from ..types import Word


_SYSTEM_PROMPT = """You are a transcript-quality reviewer for short-form video captions. Your job is to identify and fix obvious transcription errors from a Deepgram speech-to-text model.

You will receive a JSON array of word tokens for one ~30-second clip. Each token has:
  i:    word index (use this in your output)
  t:    punctuated word as Deepgram heard it
  conf: confidence score 0.0 to 1.0 (lower = more likely wrong)

Return STRICT JSON only — no commentary, no markdown:

{
  "corrections": [
    {"i": <index>, "new": "<corrected punctuated word>", "reason": "<short>"}
  ]
}

WHEN TO CORRECT
- Confidence < 0.85 AND the word doesn't fit the surrounding sentence
- Common substitutions where context makes the right word obvious:
    pest ↔ best     of ↔ have     to ↔ two/too     their ↔ there/they're
    accept ↔ except     affect ↔ effect     then ↔ than
- Proper nouns mistranscribed when context names the correct one
- Numbers that don't match the spoken context (e.g. "200 ish" → "200" if
  the next word is "employees" and "ish" reads as a stutter)
- A capitalized brand or place name that Deepgram lowercased

WHEN NOT TO CORRECT
- High-confidence words (>0.95)
- Filler words: um, uh, like, you know — leave as transcribed
- Stylistic choices the speaker actually made (contractions, slang)
- Anything you'd guess at without strong evidence — if you're less than
  80% sure the new word is correct, SKIP it

RULES
- Output exactly one correction per index. No splitting or merging — timing
  must stay aligned. If a word should be removed, leave it; the captioner
  will not crash on extra words.
- Preserve capitalization and trailing punctuation appropriate to the new word
- Conservative > aggressive. A correct transcript with a few low-confidence
  errors is better than a confidently rewritten one.
- Sort `corrections` by ascending `i`. No duplicates.

Return JSON only."""


_DISABLE_KEY = "PIPELINE_CORRECT_CAPTIONS"


def correct_captions(
    words: list[Word],
    *,
    clip_id: str,
    video_title: str = "",
) -> list[Word]:
    """Return a new word list with likely transcription errors fixed.

    Always succeeds: if the LLM call fails or the response is malformed, the
    original `words` are returned and the span is tagged with the failure
    reason for trace visibility.
    """
    span = trace.get_current_span()
    span.set_attribute("correct_captions.clip_id", clip_id)
    span.set_attribute("correct_captions.word_count", len(words))

    # Feature kill-switch — useful for cost comparison runs and emergencies.
    if os.environ.get(_DISABLE_KEY, "1") == "0":
        span.set_attribute("correct_captions.disabled", True)
        get_ledger().charge("correct_captions", 0.0, source="disabled")
        return list(words)

    if not words:
        get_ledger().charge("correct_captions", 0.0, source="empty")
        return list(words)

    # Build a compact LLM payload — drop fields the model doesn't need so we
    # don't pay for tokens that don't earn their keep.
    payload = _compact_payload(words)
    user_msg = (
        f'Clip from "{video_title or "untitled"}". {len(words)} word tokens:\n'
        f"{json.dumps(payload, separators=(',', ':'))}"
    )

    try:
        result = call_claude(
            stage="correct_captions",
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
            max_tokens=1024,
            temperature=0.0,
            cache_system=True,
        )
    except BudgetExceeded:
        # Budget mattered more than caption polish — fall through and ship
        # the originals. Don't propagate; that would fail the run.
        span.set_attribute("correct_captions.fallback", "budget_exceeded")
        get_ledger().charge("correct_captions", 0.0, source="fallback:budget")
        return list(words)
    except Exception as e:
        span.set_attribute("correct_captions.fallback", f"llm_error:{type(e).__name__}")
        get_ledger().charge("correct_captions", 0.0, source=f"fallback:{type(e).__name__}")
        return list(words)

    span.set_attribute("correct_captions.tokens_in", result.input_tokens)
    span.set_attribute("correct_captions.tokens_out", result.output_tokens)

    corrections = _parse_corrections(result.text)
    if corrections is None:
        span.set_attribute("correct_captions.fallback", "parse_error")
        # call_claude already charged the actual model cost; no extra charge.
        return list(words)

    new_words = _apply_corrections(words, corrections)
    edits_applied = sum(1 for a, b in zip(words, new_words) if a.text != b.text)
    span.set_attribute("correct_captions.edits_applied", edits_applied)
    return new_words


# ---------------- helpers ----------------

def _compact_payload(words: list[Word]) -> list[dict[str, Any]]:
    """One small dict per word so the LLM input fits inside the prompt cache
    and the per-call cost stays predictable."""
    out: list[dict[str, Any]] = []
    for i, w in enumerate(words):
        token = w.punctuated or w.text or ""
        out.append({
            "i": i,
            "t": token,
            "conf": round(float(w.confidence or 1.0), 3),
        })
    return out


_JSON_FENCED_RE = re.compile(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```")


def _parse_corrections(text: str) -> Optional[list[dict[str, Any]]]:
    """Tolerant JSON extractor. Returns the corrections list or None on failure."""
    if not text:
        return None
    raw = text.strip()
    fenced = _JSON_FENCED_RE.search(raw)
    if fenced:
        raw = fenced.group(1)
    # Find the first balanced { ... } object.
    start = raw.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    end = -1
    for i in range(start, len(raw)):
        c = raw[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    if end < 0:
        return None
    try:
        obj = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return None
    corrections = obj.get("corrections")
    if not isinstance(corrections, list):
        return None
    cleaned: list[dict[str, Any]] = []
    for entry in corrections:
        if not isinstance(entry, dict):
            continue
        idx = entry.get("i")
        new = entry.get("new")
        if isinstance(idx, int) and isinstance(new, str) and new.strip():
            cleaned.append({"i": idx, "new": new.strip()})
    return cleaned


def _apply_corrections(words: list[Word], corrections: list[dict[str, Any]]) -> list[Word]:
    """Apply index-keyed corrections, preserving timing for every word."""
    by_idx = {c["i"]: c["new"] for c in corrections if 0 <= c["i"] < len(words)}
    if not by_idx:
        return list(words)
    out: list[Word] = []
    for i, w in enumerate(words):
        if i in by_idx:
            new_text = by_idx[i]
            # Update both text and punctuated. Drop trailing punctuation off
            # the plain form so downstream code (LLM matching, currency
            # rewrites) keeps working.
            plain = re.sub(r"[.,!?;:]+$", "", new_text).lower() or new_text.lower()
            out.append(w.model_copy(update={"text": plain, "punctuated": new_text}))
        else:
            out.append(w)
    return out
