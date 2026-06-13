"""Dump full transcripts to a readable .txt with paragraph indices and times,
so I can review and write ground-truth clip boundaries by hand."""
import json, os
from glob import glob
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TRANS_DIR = ROOT / "source_data" / "transcripts"
OUT_DIR = ROOT / "source_data" / "validation" / "transcripts_readable"
OUT_DIR.mkdir(parents=True, exist_ok=True)

# Videos chosen for validation — challenging cases.
VALIDATION_VIDEOS = [
    "HlK_MeYWKEs",  # longest, 898s
    "rd_urnkST6g",  # 844s, complex multi-stream business
    "Ht9u-qEXTQY",  # 3 speakers
    "LGbS0GOZBNE",  # host recap intro
    "4J_Bo4Dbxjk",  # problem statement without ?
    "TaeBazpcRk8",  # rapid diagnostic Q&A
    "3Lvhd3LIwwY",  # question buried in intro
    "2PfbKVGNgPM",  # short, sustained dialogue
]


def dump(vid: str):
    src = TRANS_DIR / f"{vid}.deepgram.json"
    if not src.exists():
        return
    d = json.loads(src.read_text(encoding="utf-8"))
    paras = d["results"]["channels"][0]["alternatives"][0]["paragraphs"]["paragraphs"]
    duration = d["metadata"]["duration"]
    lines = [f"# {vid}  (duration={duration:.0f}s, paragraphs={len(paras)})", ""]
    for i, p in enumerate(paras):
        text = " ".join(s["text"] for s in p["sentences"])
        wc = sum(len(s["text"].split()) for s in p["sentences"])
        lines.append(f"[{i:3d}] spk={p['speaker']}  {p['start']:6.1f}-{p['end']:6.1f}  ({wc}w)")
        lines.append(f"      {text}")
    (OUT_DIR / f"{vid}.txt").write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {OUT_DIR / f'{vid}.txt'}")


if __name__ == "__main__":
    for v in VALIDATION_VIDEOS:
        dump(v)
