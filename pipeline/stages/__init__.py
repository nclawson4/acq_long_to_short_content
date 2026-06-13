"""Concrete stage implementations.

Stages owned by the harness (this package):
    ingest      — yt-dlp pull
    transcribe  — Deepgram nova-3
    finalize    — Vercel Blob upload + final QC

Stages provided by other agents and wrapped here as adapters under tools/:
    pick_timestamps — tool 2
    crop_video      — tool 1
    add_captions    — tool 3
"""
from .ingest import ingest  # noqa: F401
from .transcribe import transcribe  # noqa: F401
from .finalize import finalize_clip  # noqa: F401
from .correct_captions import correct_captions  # noqa: F401
