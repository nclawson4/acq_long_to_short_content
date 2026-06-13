"""Match short-form transcripts against long-form transcripts.

For each short, pull a handful of distinctive multi-word phrases from its
transcript and search every long-form transcript for verbatim hits. Report the
long-form video with the most matches and an approximate time window.
"""
import json
import re
from pathlib import Path
from collections import Counter

ROOT = Path(__file__).resolve().parent.parent
SHORTS_DIR = ROOT / "shorts_ingest" / "transcripts"
LONG_DIR = ROOT / "source_data" / "transcripts"


def normalize(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[^\w\s']", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def ngrams(words: list[str], n: int) -> list[str]:
    return [" ".join(words[i:i + n]) for i in range(len(words) - n + 1)]


def load_long_index(long_path: Path) -> dict:
    """Load a long-form transcript and return its full normalized text plus a
    word-time index for locating matches."""
    d = json.loads(long_path.read_text(encoding="utf-8"))
    alt = d["results"]["channels"][0]["alternatives"][0]
    words = alt["words"]
    norm_tokens: list[tuple[str, float, float]] = []
    for w in words:
        bare = normalize(w["punctuated_word"]).split()
        if bare:
            norm_tokens.append((bare[0], w["start"], w["end"]))
    return {
        "id": long_path.stem.replace(".deepgram", ""),
        "duration": d["metadata"]["duration"],
        "tokens": norm_tokens,
        "joined": " ".join(t[0] for t in norm_tokens),
    }


def find_ngram_hits(long_doc: dict, ngram: str) -> list[tuple[float, float]]:
    """Find all occurrences of `ngram` (lowercased, space-joined) in the long
    transcript. Return list of (start_time, end_time) for each hit."""
    tokens = long_doc["tokens"]
    target = ngram.split()
    L = len(target)
    hits: list[tuple[float, float]] = []
    for i in range(len(tokens) - L + 1):
        if all(tokens[i + k][0] == target[k] for k in range(L)):
            hits.append((tokens[i][1], tokens[i + L - 1][2]))
    return hits


def match_short(short_path: Path, longs: list[dict]) -> dict:
    d = json.loads(short_path.read_text(encoding="utf-8"))
    alt = d["results"]["channels"][0]["alternatives"][0]
    short_text = alt["transcript"]
    short_dur = d["metadata"]["duration"]

    norm = normalize(short_text)
    tokens = norm.split()
    # Use 6-grams — long enough to be distinctive, short enough to survive
    # minor diarization or punctuation differences.
    candidates = ngrams(tokens, 6)
    # Sample at most ~25 evenly-spaced 6-grams to keep search cheap
    if len(candidates) > 25:
        step = max(1, len(candidates) // 25)
        candidates = candidates[::step][:25]

    per_long: dict[str, list[tuple[float, float, str]]] = {}
    for long_doc in longs:
        for ng in candidates:
            for (s, e) in find_ngram_hits(long_doc, ng):
                per_long.setdefault(long_doc["id"], []).append((s, e, ng))

    if not per_long:
        return {
            "short_id": short_path.stem.replace(".deepgram", ""),
            "short_duration": short_dur,
            "match": None,
        }

    # Rank long videos by number of distinct ngrams matched
    ranked = sorted(
        per_long.items(),
        key=lambda kv: (len({ng for _, _, ng in kv[1]}), len(kv[1])),
        reverse=True,
    )
    best_id, best_hits = ranked[0]
    starts = [h[0] for h in best_hits]
    ends = [h[1] for h in best_hits]
    distinct_ngrams = len({ng for _, _, ng in best_hits})
    return {
        "short_id": short_path.stem.replace(".deepgram", ""),
        "short_duration": short_dur,
        "match": {
            "long_video_id": best_id,
            "distinct_ngrams_hit": distinct_ngrams,
            "total_hits": len(best_hits),
            "time_window_in_long": [min(starts), max(ends)],
            "approx_span_seconds": max(ends) - min(starts),
            "n_candidate_ngrams": len(candidates),
            "match_rate": round(distinct_ngrams / len(candidates), 2),
            "runner_up": ranked[1][0] if len(ranked) > 1 else None,
            "runner_up_distinct": (
                len({ng for _, _, ng in ranked[1][1]}) if len(ranked) > 1 else 0
            ),
        },
    }


def main():
    longs = [load_long_index(p) for p in sorted(LONG_DIR.glob("*.deepgram.json"))]
    print(f"Indexed {len(longs)} long-form transcripts.\n")
    results = []
    for sp in sorted(SHORTS_DIR.glob("*.deepgram.json")):
        r = match_short(sp, longs)
        results.append(r)
        if r["match"]:
            m = r["match"]
            print(
                f"{r['short_id']:14s} ({r['short_duration']:5.1f}s)  -->  "
                f"{m['long_video_id']:14s}  "
                f"hits={m['distinct_ngrams_hit']}/{m['n_candidate_ngrams']} "
                f"({m['match_rate']:.0%})  "
                f"win={m['time_window_in_long'][0]:.0f}-{m['time_window_in_long'][1]:.0f}s "
                f"(span {m['approx_span_seconds']:.0f}s)  "
                f"runner_up={m['runner_up']}({m['runner_up_distinct']})"
            )
        else:
            print(f"{r['short_id']:14s} ({r['short_duration']:5.1f}s)  -->  NO MATCH")
    out_path = ROOT / "source_data" / "validation" / "shorts_matches.json"
    out_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
