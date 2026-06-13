"""Input guardrails.

Two jobs:
    1. URL validation — only youtube.com / youtu.be, no playlists, no live URLs.
       The agent only ever sees a clean canonical URL.
    2. Transcript sanitization — strip prompt-injection-looking content before
       transcript text reaches the moment-picker LLM. An interview guest can
       casually say "ignore previous instructions and..." and we should not
       let that leak into the model context.
"""
from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse


class InputGuardrailFailed(ValueError):
    pass


_ALLOWED_HOSTS = {
    "youtube.com", "www.youtube.com", "m.youtube.com",
    "youtu.be", "www.youtu.be",
}


def validate_url(url: str) -> str:
    """Return a canonical https URL. Raise InputGuardrailFailed if rejected.

    Rules:
        - scheme must be http or https (we normalize to https)
        - host must be in _ALLOWED_HOSTS
        - reject playlists (list=) and channels — single videos only
        - reject /live/ paths — we don't handle livestream pulls
        - the URL must contain an extractable 11-char video id
    """
    if not isinstance(url, str) or not url.strip():
        raise InputGuardrailFailed("url is empty")

    url = url.strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise InputGuardrailFailed(f"unsupported scheme: {parsed.scheme!r}")
    host = (parsed.netloc or "").lower()
    if host not in _ALLOWED_HOSTS:
        raise InputGuardrailFailed(f"host not allowed: {host!r}")

    path = parsed.path or ""
    if "/live/" in path:
        raise InputGuardrailFailed("livestream URLs not supported")

    qs = parse_qs(parsed.query or "")
    if "list" in qs:
        raise InputGuardrailFailed("playlist URLs not supported — pass a single video")

    vid = extract_video_id(url)
    if not vid:
        raise InputGuardrailFailed("could not find an 11-char video id in URL")

    # Canonicalize to https://www.youtube.com/watch?v=<id>
    return f"https://www.youtube.com/watch?v={vid}"


def extract_video_id(url: str) -> str | None:
    parsed = urlparse(url)
    qs = parse_qs(parsed.query or "")
    if "v" in qs and re.fullmatch(r"[A-Za-z0-9_-]{11}", qs["v"][0]):
        return qs["v"][0]
    # youtu.be/<id>, /shorts/<id>, /embed/<id>, /v/<id>
    parts = [p for p in (parsed.path or "").split("/") if p]
    for cand in parts:
        if re.fullmatch(r"[A-Za-z0-9_-]{11}", cand):
            return cand
    return None


# ---------------- transcript sanitization ----------------

# Patterns that look like injection attempts. We don't try to be exhaustive;
# the goal is to neutralize the obvious vectors that show up in interview
# transcripts (someone reading prompt-engineering content out loud, or
# adversarial captions).
_INJECTION_PATTERNS = [
    re.compile(r"ignore (?:all )?previous (?:instructions|prompts)", re.I),
    re.compile(r"disregard (?:all )?prior (?:instructions|prompts)", re.I),
    re.compile(r"</?system>", re.I),
    re.compile(r"</?assistant>", re.I),
    re.compile(r"</?user>", re.I),
    re.compile(r"\[INST\]|\[/INST\]"),
    re.compile(r"<\|im_start\|>|<\|im_end\|>"),
]

_MAX_TRANSCRIPT_CHARS = 200_000  # Deepgram of an 8-min video ≈ 8-12k chars; cap as defense


def sanitize_transcript_text(text: str) -> str:
    """Return a transcript safe to put in an LLM prompt.

    - Truncate if pathologically long
    - Replace injection markers with neutral placeholders so the text reads
      the same to a human but the model doesn't see role markers
    """
    if not isinstance(text, str):
        return ""
    if len(text) > _MAX_TRANSCRIPT_CHARS:
        text = text[:_MAX_TRANSCRIPT_CHARS]
    for pat in _INJECTION_PATTERNS:
        text = pat.sub("[redacted]", text)
    return text
