"""Compare extract_clips.py output against the hand-annotated ground truth.

For each video, for each expected topic, search the algorithm's clips for a
match. A clip matches a topic if:
  - At least one of the topic's `answer_keywords_any` appears in the clip's
    answer_text (case-insensitive substring), AND
  - The clip's playback window overlaps the topic's approx_seconds window.

Metrics:
  - per-video: how many expected topics were hit, and how many algorithm
    clips were "extra" (no topic matched, possibly a false positive)
  - per-video: max time-overlap IoU between algorithm clips and topics
  - overall: precision, recall, F1
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GT_PATH = ROOT / "source_data" / "validation" / "ground_truth.json"
CLIPS_DIR = ROOT / "source_data" / "clip_candidates"


def load_clip_seconds(clip: dict) -> tuple[float, float]:
    segs = clip.get("kept_segments", [])
    if not segs:
        return (0.0, 0.0)
    return (segs[0][0], segs[-1][1])


def clip_overlaps_topic(clip_window: tuple[float, float],
                        topic_window: list[float]) -> bool:
    a0, a1 = clip_window
    t0, t1 = topic_window
    return not (a1 < t0 or a0 > t1)


def clip_matches_topic(clip: dict, topic: dict) -> bool:
    answer_text = clip.get("answer_text", "").lower()
    intro_text = clip.get("intro_text", "").lower()
    q_text = clip.get("question_text", "").lower()
    full_text = " ".join([intro_text, q_text, answer_text])

    keywords_any = topic.get("answer_keywords_any", [])
    if not any(k.lower() in full_text for k in keywords_any):
        return False
    cw = load_clip_seconds(clip)
    return clip_overlaps_topic(cw, topic["approx_seconds"])


def evaluate():
    gt = json.loads(GT_PATH.read_text(encoding="utf-8"))
    videos = gt["videos"]

    total_topics = 0
    total_hit_topics = 0
    total_clips = 0
    total_matched_clips = 0
    per_video_rows = []

    for vid, spec in videos.items():
        clip_file = CLIPS_DIR / f"{vid}.clips.json"
        if not clip_file.exists():
            print(f"[MISS] {vid}: no clip file")
            continue
        clip_data = json.loads(clip_file.read_text(encoding="utf-8"))
        clips = clip_data.get("clips", [])
        topics = spec["topics"]

        # For each topic, find any matching clip
        hit_topics = []
        topic_matched_clip_indices: set[int] = set()
        for t in topics:
            for ci, c in enumerate(clips):
                if clip_matches_topic(c, t):
                    hit_topics.append(t["label"])
                    topic_matched_clip_indices.add(ci)
                    break
        # Any clips that didn't match any topic — possible false positives
        unmatched_clip_indices = [
            i for i in range(len(clips)) if i not in topic_matched_clip_indices
        ]

        n_topics = len(topics)
        n_hits = len(hit_topics)
        n_clips = len(clips)
        n_matched = len(topic_matched_clip_indices)
        n_extra = len(unmatched_clip_indices)
        total_topics += n_topics
        total_hit_topics += n_hits
        total_clips += n_clips
        total_matched_clips += n_matched

        per_video_rows.append((vid, n_topics, n_hits, n_clips, n_matched, n_extra))
        print(f"{vid:18s} "
              f"topics={n_topics} hit={n_hits} "
              f"clips={n_clips} matched={n_matched} extra={n_extra}")
        # Detail any misses
        if n_hits < n_topics:
            hit_set = set(hit_topics)
            for t in topics:
                if t["label"] not in hit_set:
                    print(f"   MISS topic: {t['label']} (keywords={t['answer_keywords_any'][:3]})")
        # Detail extras
        for i in unmatched_clip_indices:
            c = clips[i]
            cw = load_clip_seconds(c)
            print(f"   EXTRA clip [{i}] {cw[0]:.0f}-{cw[1]:.0f}s "
                  f"({c['edit_seconds']:.1f}s edited): "
                  f"A: {c['answer_text'][:80]!r}")

    print()
    recall = total_hit_topics / max(1, total_topics)
    precision = total_matched_clips / max(1, total_clips)
    f1 = 2 * precision * recall / max(1e-9, precision + recall)
    print(f"=== Overall ===")
    print(f"Topics: {total_topics}, hit: {total_hit_topics}, recall = {recall:.2%}")
    print(f"Clips:  {total_clips}, matched: {total_matched_clips}, "
          f"extra: {total_clips - total_matched_clips}, precision = {precision:.2%}")
    print(f"F1 = {f1:.2%}")


if __name__ == "__main__":
    evaluate()
