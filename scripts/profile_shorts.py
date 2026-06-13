"""Profile the structure of the 11 reference shorts so we can compare against
our extractor's output. Each short is hand-segmented into intro / question /
answer by heuristic, then we measure how much time each segment occupies."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from statistics import mean, median

sys.path.insert(0, str(Path(__file__).resolve().parent))
from extract_clips import (
    GUEST_STRONG_MARKERS, GUEST_WEAK_MARKERS,
    QUESTION_LIKE_PHRASES, is_backchannel_sentence,
)

ROOT = Path(__file__).resolve().parent.parent
SHORTS_DIR = ROOT / "shorts_ingest" / "transcripts"


def segment_short(short_path: Path) -> dict:
    d = json.loads(short_path.read_text(encoding="utf-8"))
    alt = d["results"]["channels"][0]["alternatives"][0]
    paras = alt["paragraphs"]["paragraphs"]
    duration = d["metadata"]["duration"]

    # Heuristic segmentation:
    #   intro    = first contiguous guest-speaker paragraph(s) that contain a
    #              guest-strong/weak marker
    #   question = guest content from end-of-intro up to the first substantive
    #              host paragraph (>=10 host words)
    #   answer   = everything after intro+Q
    # Guest speaker = the speaker of the FIRST paragraph (per the convention).
    if not paras:
        return None
    guest_spk = paras[0]["speaker"]
    host_spks = {p["speaker"] for p in paras} - {guest_spk}

    # Find intro end: walk until first host paragraph >=10 substantive words
    # OR up to 20s.
    intro_end_idx = 0
    for i, p in enumerate(paras):
        if p["start"] > 20.0:
            break
        if p["speaker"] in host_spks:
            words_in = sum(
                len(s["text"].split()) for s in p["sentences"]
                if not is_backchannel_sentence(s["text"])
            )
            if words_in >= 10:
                break
        intro_end_idx = i

    # If a guest paragraph after intro contains a '?' or question-like phrase
    # before the first substantive host paragraph, that's the explicit Q.
    q_start_para = None
    q_end_para = None
    first_answer_idx = None
    for i, p in enumerate(paras[intro_end_idx + 1:], start=intro_end_idx + 1):
        if p["speaker"] in host_spks:
            words_in = sum(
                len(s["text"].split()) for s in p["sentences"]
                if not is_backchannel_sentence(s["text"])
            )
            if words_in >= 10:
                first_answer_idx = i
                break
        elif q_start_para is None:
            q_start_para = i
            q_end_para = i
        else:
            q_end_para = i
    if first_answer_idx is None:
        first_answer_idx = len(paras)

    intro_start = paras[0]["start"]
    intro_end = paras[intro_end_idx]["end"]
    q_seconds = 0.0
    if q_start_para is not None and q_end_para is not None:
        q_seconds = paras[q_end_para]["end"] - paras[q_start_para]["start"]
    a_seconds = 0.0
    if first_answer_idx < len(paras):
        a_seconds = paras[-1]["end"] - paras[first_answer_idx]["start"]

    intro_seconds = intro_end - intro_start
    return {
        "id": short_path.stem.replace(".deepgram", ""),
        "duration": duration,
        "intro_s": intro_seconds,
        "question_s": q_seconds,
        "answer_s": a_seconds,
        "intro_pct": intro_seconds / duration,
        "question_pct": q_seconds / duration,
        "answer_pct": a_seconds / duration,
    }


def main():
    rows = []
    for sp in sorted(SHORTS_DIR.glob("*.deepgram.json")):
        r = segment_short(sp)
        if r is None:
            continue
        rows.append(r)
    print(f"{'short_id':14s} {'dur':>6s} {'intro':>7s} {'Q':>7s} {'A':>7s}   "
          f"{'intro%':>6s} {'Q%':>6s} {'A%':>6s}")
    for r in rows:
        print(f"{r['id']:14s} "
              f"{r['duration']:6.1f}s "
              f"{r['intro_s']:6.1f}s "
              f"{r['question_s']:6.1f}s "
              f"{r['answer_s']:6.1f}s   "
              f"{r['intro_pct']:6.0%} {r['question_pct']:6.0%} {r['answer_pct']:6.0%}")

    print()
    print("== Aggregate ==")
    durs = [r["duration"] for r in rows]
    intros = [r["intro_s"] for r in rows]
    qs = [r["question_s"] for r in rows]
    answs = [r["answer_s"] for r in rows]
    print(f"  duration:  mean={mean(durs):.1f}s  median={median(durs):.1f}s  "
          f"range=[{min(durs):.0f}, {max(durs):.0f}]s")
    print(f"  intro:     mean={mean(intros):.1f}s  median={median(intros):.1f}s")
    print(f"  question:  mean={mean(qs):.1f}s   median={median(qs):.1f}s")
    print(f"  answer:    mean={mean(answs):.1f}s  median={median(answs):.1f}s")
    print(f"  intro %:   mean={mean(r['intro_pct'] for r in rows):.0%}")
    print(f"  question%: mean={mean(r['question_pct'] for r in rows):.0%}")
    print(f"  answer %:  mean={mean(r['answer_pct'] for r in rows):.0%}")


if __name__ == "__main__":
    main()
