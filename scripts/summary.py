"""Print a summary of the extraction run + validation results."""
import json, glob
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLIPS_DIR = ROOT / "source_data" / "clip_candidates"


def main():
    files = sorted(CLIPS_DIR.glob("*.clips.json"))
    n_videos = len(files)
    n_clips = 0
    n_implicit = 0
    n_in_intro = 0
    n_over = 0
    n_ctx_dep = 0
    n_with_trim = 0
    durations = []
    edit_durations = []
    clips_per_video = []
    for f in files:
        d = json.loads(f.read_text(encoding="utf-8"))
        durations.append(d["duration"])
        clips_per_video.append(len(d["clips"]))
        for c in d["clips"]:
            n_clips += 1
            edit_durations.append(c["edit_seconds"])
            if c["edit_seconds"] > 120:
                n_over += 1
            if c["context_dependent"]:
                n_ctx_dep += 1
            if c["trims_applied"]:
                n_with_trim += 1
            if "implicit_question" in c.get("notes", []):
                n_implicit += 1
            if "question_in_intro" in c.get("notes", []):
                n_in_intro += 1

    avg_edit = sum(edit_durations) / max(1, len(edit_durations))
    in_sweet = sum(1 for e in edit_durations if 45 <= e <= 90)
    in_hard = sum(1 for e in edit_durations if e <= 120)
    print(f"Videos processed:       {n_videos}")
    print(f"Total clips:            {n_clips}")
    print(f"  with trims applied:   {n_with_trim}")
    print(f"  implicit question:    {n_implicit}")
    print(f"  question in intro:    {n_in_intro}")
    print(f"  context-dependent:    {n_ctx_dep}")
    print(f"  over 120s budget:     {n_over}")
    print(f"  under 120s cap:       {in_hard} ({in_hard/n_clips:.0%})")
    print(f"  in 45-90s sweet spot: {in_sweet} ({in_sweet/n_clips:.0%})")
    print(f"Average clip length:    {avg_edit:.1f}s")
    print(f"Clips per video:        min={min(clips_per_video)} max={max(clips_per_video)} avg={sum(clips_per_video)/n_videos:.1f}")
    print()
    print("Distribution of clips per video:")
    from collections import Counter
    c = Counter(clips_per_video)
    for k in sorted(c):
        print(f"  {k} clips: {c[k]} videos")


if __name__ == "__main__":
    main()
