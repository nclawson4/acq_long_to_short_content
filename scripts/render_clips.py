"""Render audio for each clip candidate so it can be reviewed in the dashboard.

For each clip in source_data/clip_candidates/<vid>.clips.json:
  - Read kept_segments (list of [start, end] time ranges in the source video)
  - Use ffmpeg to extract each segment from source_data/videos/<vid>.mp4
  - Concatenate the segments into a single .mp3 in
    processing/clip_dashboard/clips/<vid>__<idx>.mp3

Also emits processing/clip_dashboard/manifest.json — the data the UI binds to.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLIPS_JSON_DIR = ROOT / "source_data" / "clip_candidates"
VIDEOS_DIR = ROOT / "source_data" / "videos"
VIDEO_INFO_DIR = ROOT / "source_data" / "video_info"
OUT_DIR = ROOT / "processing" / "clip_dashboard"
AUDIO_DIR = OUT_DIR / "clips"


def run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        sys.stderr.write(f"FFmpeg failed: {' '.join(cmd)}\n{proc.stderr}\n")
        raise RuntimeError(proc.stderr)


def render_one(video_path: Path, segments: list[tuple[float, float]],
               out_path: Path) -> None:
    """Cut each segment to a temp .mp3 then concat with ffmpeg's concat demuxer.
    A concat demuxer is the most reliable way to splice multiple cuts without
    re-encoding artifacts between joins.
    """
    if not segments:
        return
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        seg_files: list[Path] = []
        for i, (start, end) in enumerate(segments):
            seg_file = tmp_dir / f"seg_{i:03d}.mp3"
            # Cut audio only, force consistent bitrate so concat works cleanly
            run([
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-ss", f"{start:.3f}",
                "-to", f"{end:.3f}",
                "-i", str(video_path),
                "-vn",
                "-acodec", "libmp3lame",
                "-ar", "44100",
                "-ac", "1",
                "-b:a", "96k",
                str(seg_file),
            ])
            seg_files.append(seg_file)

        # Build concat list file
        list_file = tmp_dir / "concat.txt"
        list_file.write_text(
            "\n".join(f"file '{p.as_posix()}'" for p in seg_files),
            encoding="utf-8",
        )
        # Concatenate and re-encode so the final MP3 carries a proper Xing
        # VBR header — browsers need it to enable scrubbing/seeking. Using
        # -c copy here produces a "headerless" MP3 that plays but isn't
        # seekable in HTML <audio>.
        out_path.parent.mkdir(parents=True, exist_ok=True)
        run([
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "concat", "-safe", "0",
            "-i", str(list_file),
            "-c:a", "libmp3lame",
            "-ar", "44100", "-ac", "1", "-b:a", "96k",
            "-write_xing", "1",
            str(out_path),
        ])


def load_title(vid: str) -> str:
    p = VIDEO_INFO_DIR / f"{vid}.info.json"
    if not p.exists():
        return vid
    try:
        return json.loads(p.read_text(encoding="utf-8")).get("title", vid)
    except Exception:
        return vid


def main():
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    manifest = {"videos": []}
    total_clips = 0
    rendered = 0
    skipped = 0
    for clips_path in sorted(CLIPS_JSON_DIR.glob("*.clips.json")):
        data = json.loads(clips_path.read_text(encoding="utf-8"))
        vid = data["video_id"]
        video_file = VIDEOS_DIR / f"{vid}.mp4"
        if not video_file.exists():
            print(f"[skip] {vid}: source video missing")
            continue
        title = load_title(vid)
        clips_meta = []
        for idx, clip in enumerate(data.get("clips", [])):
            total_clips += 1
            segments = [tuple(s) for s in clip["kept_segments"]]
            out_audio = AUDIO_DIR / f"{vid}__{idx:02d}.mp3"
            if out_audio.exists() and out_audio.stat().st_size > 0:
                skipped += 1
            else:
                try:
                    render_one(video_file, segments, out_audio)
                    rendered += 1
                    print(f"  rendered {out_audio.name}  "
                          f"({clip['edit_seconds']:.1f}s, "
                          f"{len(segments)} segs)")
                except Exception as e:
                    print(f"  FAIL {out_audio.name}: {e}")
                    continue
            clips_meta.append({
                "idx": idx,
                "audio": f"clips/{out_audio.name}",
                "edit_seconds": clip["edit_seconds"],
                "raw_seconds": clip["raw_seconds"],
                "score": clip["score"],
                "context_dependent": clip["context_dependent"],
                "notes": clip.get("notes", []),
                "trims_applied": clip.get("trims_applied", []),
                "intro_text": clip["intro_text"],
                "question_text": clip["question_text"],
                "answer_text": clip["answer_text"],
                "kept_segments": clip["kept_segments"],
            })
        manifest["videos"].append({
            "video_id": vid,
            "title": title,
            "duration": data["duration"],
            "guest_speaker": data.get("guest_speaker"),
            "host_speaker": data.get("host_speaker"),
            "clips": clips_meta,
        })
    manifest["total_clips"] = total_clips
    manifest["total_videos"] = len(manifest["videos"])
    (OUT_DIR / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    print(f"\nRendered {rendered} clips, skipped {skipped} already-on-disk, "
          f"total {total_clips} across {len(manifest['videos'])} videos.")
    print(f"Manifest -> {OUT_DIR / 'manifest.json'}")


if __name__ == "__main__":
    main()
