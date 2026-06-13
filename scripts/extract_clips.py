"""
Find clippable Q&A moments in Acquisition.com long-form transcripts.

Per video, emit zero-or-more clip candidates. Each candidate is:
  [intro from start of video] + [a specific question] + [its answer]
packed into <=60s by trimming filler/cross-talk/low-value sentences,
always snapping cuts to word boundaries from the deepgram timing.

Reads:  source_data/transcripts/<id>.deepgram.json
Writes: source_data/clip_candidates/<id>.clips.json
"""
from __future__ import annotations

import json
import math
import os
import re
import sys
from dataclasses import dataclass, field, asdict
from glob import glob
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TRANS_DIR = ROOT / "source_data" / "transcripts"
OUT_DIR = ROOT / "source_data" / "clip_candidates"

MAX_SECONDS = 150.0  # hard upper bound (user OK with going over 120 for quality)
TARGET_SECONDS = 90.0  # preferred sweet-spot ceiling
MIN_SECONDS = 15.0  # don't ship a too-short clip
CONCLUSION_KEEP = 10  # always keep this many trailing answer sentences (Alex's punchline)

# ---- linguistic markers --------------------------------------------------

# Strong self-introducer phrases — almost always the guest describing themselves.
GUEST_STRONG_MARKERS = [
    "i sell", "we sell", "i run", "we run",
    "we're a", "we are a", "i'm a ", "i am a",
    "we've got a ", "we have a ", "i have a ",
    "our company", "our business", "my company", "my business",
    "we own", "i own", "we operate",
    "our revenue", "our team", "we make",
    "wanna get to", "want to get to", "wanna scale", "want to scale",
    "looking to scale", "looking to grow", "looking to hit",
    "do about", "we do about",
]
# Weak markers — could be either speaker depending on context
GUEST_WEAK_MARKERS = [
    "per month", "a month", "a year", "per year",
    "in revenue", "in sales", "million", "thousand a month",
]
HOST_MARKERS = [
    "let me ask you", "what you wanna",
    "here's what i'd", "the way i'd do",
    "if i were you", "you should", "you need to",
    "you gotta", "you've got to",
    "the answer is", "the way you", "what i would",
]
# Things that look like a recap of the guest by the host
HOST_RECAP_MARKERS = [
    "so you have", "so you've", "so you do", "so you run",
    "so you sell", "so you are", "so you're",
    "you said you", "you mentioned",
]

QUESTION_STARTERS = (
    "how", "what", "why", "when", "where", "should", "do ", "does ",
    "did ", "is ", "are ", "can ", "could ", "would ", "will ", "won't ",
    "am i", "have you", "have we",
)
# Filler tokens that can be cut individually (word-level)
FILLER_WORDS = {
    "um", "umm", "uh", "uhh", "er", "eh", "mhm", "mhmm", "hmm",
    "like", "kinda", "kind", "sorta", "literally", "basically",
    "actually", "honestly", "right",
}
# Multi-token fillers (cut as a contiguous run)
FILLER_PHRASES = [
    ["you", "know"],
    ["i", "mean"],
    ["sort", "of"],
    ["kind", "of"],
    ["you", "know", "what", "i", "mean"],
]
# Backchannels — tiny acknowledgments the OTHER speaker makes during a turn
BACKCHANNEL_SENTENCES = {
    "yeah", "yeah.", "yes", "yes.", "yep", "yep.", "right", "right.",
    "okay", "okay.", "ok", "ok.", "got it", "got it.", "mhm", "mhm.",
    "sure", "sure.", "mm hmm", "mm-hmm", "uh huh", "uh-huh", "cool",
    "cool.", "true", "true.", "exactly", "exactly.", "of course",
    "of course.", "totally", "totally.",
}
# Anaphora markers — question starts that *clearly* depend on prior context.
# A bare "So " or "So," is NOT included: many self-contained questions open
# with "So how do I..." once the intro has set up the topic.
ANAPHORIC_QUESTION_STARTS = (
    "yeah but", "yeah, but",
    "what about", "and what", "and how", "and why", "and should",
    "okay but", "ok but",
    "but how do", "but what about", "but why",
)

WORD_RE = re.compile(r"[a-z']+")


# ---- data types ----------------------------------------------------------

@dataclass
class Word:
    idx: int
    text: str           # punctuated
    bare: str           # lower, no punctuation
    start: float
    end: float
    speaker: int
    para_idx: int       # which paragraph it belongs to
    sent_idx: int       # which sentence within the transcript
    sent_text: str

    @property
    def dur(self) -> float:
        return max(0.0, self.end - self.start)


@dataclass
class Sentence:
    idx: int
    text: str
    start: float
    end: float
    speaker: int
    para_idx: int
    word_indices: list[int] = field(default_factory=list)


@dataclass
class Paragraph:
    idx: int
    speaker: int
    start: float
    end: float
    sentences: list[Sentence]
    text: str


@dataclass
class Span:
    """A kept window of word indices [w_start, w_end] inclusive."""
    w_start: int
    w_end: int

    def duration(self, words: list[Word]) -> float:
        return words[self.w_end].end - words[self.w_start].start


# ---- loading -------------------------------------------------------------

def load_transcript(path: Path) -> tuple[list[Word], list[Sentence], list[Paragraph], float]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    alt = raw["results"]["channels"][0]["alternatives"][0]
    duration = raw["metadata"]["duration"]

    word_records = alt["words"]
    para_records = alt["paragraphs"]["paragraphs"]

    # Flatten paragraphs/sentences and assign a global sentence index.
    sentences: list[Sentence] = []
    paragraphs: list[Paragraph] = []
    sent_idx = 0
    for p_idx, p in enumerate(para_records):
        para_sents: list[Sentence] = []
        for s in p["sentences"]:
            sent = Sentence(
                idx=sent_idx,
                text=s["text"],
                start=s["start"],
                end=s["end"],
                speaker=p["speaker"],
                para_idx=p_idx,
            )
            sentences.append(sent)
            para_sents.append(sent)
            sent_idx += 1
        para_text = " ".join(s.text for s in para_sents)
        paragraphs.append(Paragraph(
            idx=p_idx, speaker=p["speaker"],
            start=p["start"], end=p["end"],
            sentences=para_sents, text=para_text,
        ))

    # Attach words to sentences by timestamp (deepgram words have start/end + speaker;
    # paragraphs/sentences also carry timing, so we map word to the sentence whose
    # window contains its midpoint).
    words: list[Word] = []
    s_cursor = 0
    for w_idx, w in enumerate(word_records):
        start = w["start"]
        end = w["end"]
        mid = (start + end) / 2.0
        while s_cursor + 1 < len(sentences) and sentences[s_cursor].end < mid - 0.01:
            s_cursor += 1
        sent = sentences[s_cursor]
        bare = WORD_RE.findall(w["punctuated_word"].lower())
        bare_s = bare[0] if bare else w["word"].lower()
        word = Word(
            idx=w_idx,
            text=w["punctuated_word"],
            bare=bare_s,
            start=start,
            end=end,
            speaker=w.get("speaker", sent.speaker),
            para_idx=sent.para_idx,
            sent_idx=sent.idx,
            sent_text=sent.text,
        )
        words.append(word)
        sent.word_indices.append(w_idx)

    return words, sentences, paragraphs, duration


# ---- speaker detection ---------------------------------------------------

def detect_guest_speaker(paragraphs: list[Paragraph]) -> tuple[int, dict[int, dict]]:
    """Score each speaker; the guest talks about their own business; the host
    addresses the guest and gives advice. Returns guest_speaker id and per-spk scores.

    Strategy: the GUEST nearly always opens the conversation with a strong-guest
    marker (\"I sell / we sell / we run...\"). Use that as a near-deterministic
    signal when available, fall back to scoring otherwise.
    """
    # ---- shortcut: who opens the conversation with a strong guest marker? ----
    early_window_end = 30.0
    opener_hits: dict[int, int] = {}
    for p in paragraphs:
        if p.start >= early_window_end:
            continue
        if sum(len(s.text.split()) for s in p.sentences) < 5:
            continue
        lower = p.text.lower()
        if any(m in lower for m in GUEST_STRONG_MARKERS):
            # Don't count host-recap intros like "So you sell pest control..."
            if any(m in lower for m in HOST_RECAP_MARKERS):
                continue
            opener_hits[p.speaker] = opener_hits.get(p.speaker, 0) + 1

    scores: dict[int, dict] = {}
    for p in paragraphs:
        spk = p.speaker
        rec = scores.setdefault(spk, {
            "strong_guest": 0.0, "weak_guest": 0.0, "host_score": 0.0,
            "i_we": 0, "you_your": 0,
            "words": 0, "first_para_start": p.start,
        })
        words_in = sum(len(s.text.split()) for s in p.sentences)
        rec["words"] += words_in
        rec["first_para_start"] = min(rec["first_para_start"], p.start)
        lower = p.text.lower()
        for m in GUEST_STRONG_MARKERS:
            if m in lower:
                rec["strong_guest"] += 1.0
        for m in GUEST_WEAK_MARKERS:
            if m in lower:
                rec["weak_guest"] += 1.0
        for m in HOST_MARKERS:
            if m in lower:
                rec["host_score"] += 1.0
        for m in HOST_RECAP_MARKERS:
            if m in lower:
                rec["host_score"] += 1.5  # strong host signal
        # Pronoun ratios
        for tok in re.findall(r"\b(i|we|my|our)\b", lower):
            rec["i_we"] += 1
        for tok in re.findall(r"\b(you|your)\b", lower):
            rec["you_your"] += 1

    # If a speaker is barely there (<5% of total words), drop them.
    total_words = sum(r["words"] for r in scores.values()) or 1
    candidates = {spk: r for spk, r in scores.items()
                  if r["words"] / total_words >= 0.05}
    if not candidates:
        candidates = scores

    # Deterministic path: if exactly one speaker opens the conversation with
    # a strong guest marker, they're the guest.
    if len(opener_hits) == 1:
        winner = next(iter(opener_hits))
        if winner in candidates:
            return winner, scores

    # If multiple speakers hit opener markers (rare — host paraphrasing), pick
    # the one with the lower host_score among them.
    if len(opener_hits) > 1:
        contenders = [s for s in opener_hits if s in candidates]
        if contenders:
            best = min(contenders, key=lambda s: candidates[s]["host_score"])
            return best, scores

    # The host typically talks MORE than the guest (delivers long answers).
    # The guest tends to:
    #   - open the conversation (lowest first_para_start)
    #   - use more "I/we" relative to "you/your"
    #   - hit strong guest markers (own-business descriptions)
    def score(spk: int) -> float:
        r = candidates[spk]
        # Pronoun ratio (avoid div-by-zero)
        i_we = r["i_we"]
        you_your = r["you_your"]
        denom = i_we + you_your + 1
        pronoun_signal = (i_we - you_your) / denom  # in [-1, 1]
        # Earlier first-para -> guest
        earliest = min(c["first_para_start"] for c in candidates.values())
        opener_bonus = 1.5 if abs(r["first_para_start"] - earliest) < 0.5 else 0.0
        # Word count: less talk usually means guest, but only weakly
        share = r["words"] / total_words
        talk_penalty = 1.0 * (share - 0.5)  # positive share>0.5 => more talk => more host
        return (
            2.0 * r["strong_guest"]
            + 0.4 * r["weak_guest"]
            - 1.2 * r["host_score"]
            + 2.5 * pronoun_signal
            + opener_bonus
            - talk_penalty
        )

    best = max(candidates.keys(), key=score)
    return best, scores


# ---- question detection --------------------------------------------------

def is_question_text(text: str) -> bool:
    t = text.strip()
    if not t:
        return False
    if t.endswith("?"):
        return True
    low = t.lower()
    for q in QUESTION_STARTERS:
        if low.startswith(q):
            # avoid false positives like "Should you do X." (rare without ?)
            return t.endswith("?")
    return False


def is_anaphoric_question(text: str) -> bool:
    low = text.strip().lower()
    return any(low.startswith(p) for p in ANAPHORIC_QUESTION_STARTS)


def is_backchannel_sentence(text: str) -> bool:
    low = text.strip().lower().rstrip(".,!?")
    return low in {b.rstrip(".,!?") for b in BACKCHANNEL_SENTENCES} or len(low) <= 2


def is_noise_sentence(text: str) -> bool:
    """Mic adjustments, repair requests, or diarization-split fragments —
    drop these same as backchannels."""
    stripped = text.strip()
    if not stripped:
        return True
    low = stripped.lower()
    if any(p in low for p in NOISE_SENTENCE_PATTERNS):
        return True
    # Diarization-split fragments: deepgram normally capitalizes the first
    # letter of every sentence. A sentence that starts with a lowercase
    # alphabetic character is almost always a mid-stream continuation that
    # got mis-attributed to the wrong speaker (e.g. the host says "...can
    # they hear" and the next speaker's paragraph starts with "hear Yeah").
    # Drop only if it's also short — long lowercase-led sentences are
    # usually transcription oddities we'd rather keep.
    first = stripped[0]
    if first.isalpha() and first.islower() and len(stripped.split()) <= 6:
        return True
    return False


def is_outro_promo(text: str) -> bool:
    low = text.strip().lower()
    return any(m in low for m in OUTRO_MARKERS)


def is_filler_sentence(text: str) -> bool:
    """Combined check: backchannel OR noise OR outro promo."""
    return (is_backchannel_sentence(text)
            or is_noise_sentence(text)
            or is_outro_promo(text))


# ---- intro extraction ----------------------------------------------------

def extract_intro_span(words: list[Word], paragraphs: list[Paragraph],
                       guest_spk: int) -> tuple[int, int] | None:
    """Find the opening intro: the guest's setup at the top of the video,
    everything from the first guest sentence up to either:
      - the FIRST substantive host response (>=12 host words), OR
      - 30 seconds of guest content (hard cap to keep clips tight)
    Returns (w_start, w_end) inclusive."""
    intro_cap = 60.0
    intro_max_seconds = 30.0
    host_spks = {p.speaker for p in paragraphs} - {guest_spk}

    # Find the first paragraph that contains a guest marker (or is from the
    # guest speaker and substantive).
    first_intro_para_idx: int | None = None
    for i, p in enumerate(paragraphs):
        if p.start >= intro_cap:
            break
        if p.speaker != guest_spk:
            continue
        if sum(len(s.text.split()) for s in p.sentences) < 3:
            continue
        lower = p.text.lower()
        if any(m in lower for m in GUEST_STRONG_MARKERS) or any(m in lower for m in GUEST_WEAK_MARKERS):
            first_intro_para_idx = i
            break
    # Fallback: just take the first guest paragraph
    if first_intro_para_idx is None:
        for i, p in enumerate(paragraphs):
            if p.speaker == guest_spk and p.start < intro_cap:
                first_intro_para_idx = i
                break
    if first_intro_para_idx is None:
        return None

    # Find the END of the intro: walk forward; stop just before the first host
    # paragraph that has >=12 substantive words (real response, not a probe)
    # OR when we exceed intro_max_seconds of guest content.
    last_intro_para_idx = first_intro_para_idx
    intro_start_time = paragraphs[first_intro_para_idx].start
    for j in range(first_intro_para_idx, len(paragraphs)):
        p = paragraphs[j]
        if p.start > intro_cap:
            break
        if p.start - intro_start_time > intro_max_seconds:
            break
        if p.speaker in host_spks:
            words_in = sum(
                len(s.text.split()) for s in p.sentences
                if not is_backchannel_sentence(s.text)
            )
            if words_in >= 12:
                break
            # Otherwise it's a probe/clarifying question — keep it inside intro
            last_intro_para_idx = j
        else:
            last_intro_para_idx = j

    intro_paras = paragraphs[first_intro_para_idx:last_intro_para_idx + 1]
    # Word range = first word of first intro para, last word of last intro para.
    w_start = min(min(s.word_indices) for p in intro_paras for s in p.sentences if s.word_indices)
    w_end = max(max(s.word_indices) for p in intro_paras for s in p.sentences if s.word_indices)
    return (w_start, w_end)


# ---- Q&A discovery -------------------------------------------------------

@dataclass
class QAPair:
    question_sents: list[Sentence]
    answer_sents: list[Sentence]
    q_word_range: tuple[int, int]
    a_word_range: tuple[int, int]
    context_dependent: bool
    notes: list[str] = field(default_factory=list)


def find_implicit_qa(paragraphs: list[Paragraph], guest_spk: int,
                     intro_end_word: int, words: list[Word]) -> QAPair | None:
    """Fallback: when the guest never asks a literal '?' question, treat their
    opening problem-statement as the implicit question and Alex's first
    substantive response as the answer. Used only if find_qa_pairs returns []."""
    host_spks = {p.speaker for p in paragraphs} - {guest_spk}
    intro_end_time = words[intro_end_word].end if intro_end_word < len(words) else 0.0
    # Find the first host paragraph with >=20 words after the intro window.
    n = len(paragraphs)
    answer_sents: list[Sentence] = []
    a_start_idx = None
    for i, p in enumerate(paragraphs):
        if p.start < intro_end_time - 1.0:
            continue
        if p.speaker in host_spks:
            words_in = sum(
                len(s.text.split()) for s in p.sentences
                if not is_backchannel_sentence(s.text)
            )
            if words_in >= 20:
                a_start_idx = i
                break
    if a_start_idx is None:
        return None
    # The implicit "question" = the guest's most recent substantive setup
    # before this answer. Walk back through preceding paragraphs (both speakers)
    # collecting guest sentences; stop once we have ~20+ guest words OR we hit
    # the end of the intro window.
    q_sents: list[Sentence] = []
    total_words = 0
    j = a_start_idx - 1
    while j >= 0 and total_words < 20:
        pa = paragraphs[j]
        if pa.speaker == guest_spk:
            for s in reversed(pa.sentences):
                if is_backchannel_sentence(s.text):
                    continue
                q_sents.insert(0, s)
                total_words += len(s.text.split())
                if total_words >= 20:
                    break
        # Don't cross too far back — bail if we've reached the intro window.
        if pa.start < intro_end_time - 1.0:
            break
        j -= 1
    if not q_sents:
        return None
    q_w_start = min(min(s.word_indices) for s in q_sents if s.word_indices)
    q_w_end = max(max(s.word_indices) for s in q_sents if s.word_indices)

    # Collect answer: contiguous host paragraphs until guest speaks substantively.
    k = a_start_idx
    substantive_word_threshold = 8
    while k < n:
        pk = paragraphs[k]
        if pk.speaker in host_spks:
            for s in pk.sentences:
                if not is_backchannel_sentence(s.text):
                    answer_sents.append(s)
            k += 1
        else:
            guest_words = sum(
                len(s.text.split()) for s in pk.sentences
                if not is_backchannel_sentence(s.text)
            )
            if guest_words >= substantive_word_threshold:
                break
            k += 1
    if not answer_sents:
        return None
    a_w_start = min(min(s.word_indices) for s in answer_sents if s.word_indices)
    a_w_end = max(max(s.word_indices) for s in answer_sents if s.word_indices)
    return QAPair(
        question_sents=q_sents,
        answer_sents=answer_sents,
        q_word_range=(q_w_start, q_w_end),
        a_word_range=(a_w_start, a_w_end),
        context_dependent=False,
        notes=["implicit_question"],
    )


MIN_ANSWER_WORDS = 30         # an "answer" worth clipping has at least this many host words
MIN_GUEST_INTERRUPT_WORDS = 10  # legacy threshold (no longer used as a hard break)

# Sentence patterns that look like noise — mic adjustments, repair requests,
# operator chatter. These get dropped sentence-level the same way backchannels
# are.
NOISE_SENTENCE_PATTERNS = [
    "go to the mic", "into the mic", "can you go to",
    "can you say that again", "say that again",
    "what was that", "what was the question",
    "from the top", "start over", "starting over",
    "can you repeat", "say it again",
    "hold on a second", "hold on,", "wait wait",
    "i'm sorry, what", "sorry, what?", "what did you say",
    "what did you ask",
    "can you hear", "can everybody hear", "is this on",
]

# Outro promo: Alex's standard "If you're a business owner" closer. Always cut.
OUTRO_MARKERS = [
    "acquisition.com/roadmap", "acquisition.com /roadmap",
    "free gift", "100000000 scaling road map",
    "scaling road map",
]

# Phrases that mark a guest sentence as a real "question / problem statement"
# (as opposed to a response to an Alex diagnostic probe). Kept tight to avoid
# matching things the guest commonly says while answering Alex.
QUESTION_LIKE_PHRASES = [
    "the problem is", "the issue is", "the challenge is",
    "i want to", "i wanna", "we want to", "we wanna",
    "i need to", "we need to",
    "i'm trying to", "i'm looking to", "we're trying to", "we're looking to",
    "i'm stuck", "stuck on", "stuck at",
    "should i", "should we",
    "how do i", "how do we", "how should",
    "what should", "what would you",
    "i don't know how", "i don't know what",
    "looking to scale", "looking to grow",
    "wanna get to", "want to get to",
    "wanna scale", "want to scale",
    "wanna figure", "want to figure", "trying to figure",
    "what's stopping", "what is stopping", "stopping me", "stopping us",
    "i'd like to", "we'd like to", "i'd love to", "we'd love to",
    "i'd want to", "we'd want to",
    "would like to", "would love to",
    "ideally", "long term", "long-term",
    "how do you", "what do you",
]

# Interjection / clarification patterns that may end with '?' but aren't real
# questions (the guest is asking Alex to repeat or apologizing).
INTERJECTION_Q_PATTERNS = [
    "what did you ask", "what did you say", "what was that", "what's that",
    "say that again", "huh?", "sorry?", "sorry,", "what?",
    "what was the question", "what was the",
]


def is_question_or_setup(text: str) -> bool:
    low = text.lower()
    # Explicit-? sentences only count if they're substantive — checked by
    # caller via the explicit_q flag, not here.
    return any(p in low for p in QUESTION_LIKE_PHRASES)


def is_interjection_question(text: str) -> bool:
    low = text.lower().strip()
    return any(p in low for p in INTERJECTION_Q_PATTERNS)


def find_qa_pairs(paragraphs: list[Paragraph], guest_spk: int,
                  intro_end_word: int, words: list[Word],
                  intro_text: str = "") -> list[QAPair]:
    """Find Q&A pairs by:
      1. Locating each substantive host answer block (>= MIN_ANSWER_WORDS host words,
         allowing short guest interjections inside the block).
      2. For each block, walking back to find the preceding guest setup.
      3. Classifying: if the guest setup contains a '?', explicit question;
         otherwise, implicit question.
    """
    host_spks = {p.speaker for p in paragraphs} - {guest_spk}
    n = len(paragraphs)
    intro_end_time = words[intro_end_word].end if intro_end_word < len(words) else 0.0

    # --- Step 1: build answer blocks ---
    # An answer block = host run that contains real advice. Guest paragraphs
    # interleaved inside don't end the block UNLESS they're a new substantive
    # question (has '?' and >=5 words). This way, "Yeah, 40%" or "We sell to
    # consumers" from the guest answering Alex's probe doesn't truncate
    # Alex's eventual punchline.
    #
    # The block must end on a non-'?' host sentence — if its tail is all
    # diagnostic questions, we extend forward; if extension hits a real new
    # question or the outro, we trim the trailing '?' sentences from the
    # block so its final sentence is a real statement.
    def is_substantive_new_question(p: Paragraph) -> bool:
        # Only counts as a real new question if a '?' sentence has >=5 words.
        # Short clarifications like "Upfront?" or "Sorry?" do NOT break the
        # answer block — they're the guest responding to Alex's probe.
        for s in p.sentences:
            if is_filler_sentence(s.text):
                continue
            if s.text.strip().endswith("?") and len(s.text.split()) >= 5:
                return True
        return False

    def last_host_sentence_ends_with_q(end_idx: int, start_idx: int) -> bool:
        for k in range(end_idx, start_idx - 1, -1):
            p = paragraphs[k]
            if p.speaker not in host_spks:
                continue
            for s in reversed(p.sentences):
                if is_filler_sentence(s.text):
                    continue
                return s.text.strip().endswith("?")
        return False

    answer_blocks: list[tuple[int, int, int]] = []  # (start_p, end_p, host_words)
    i = 0
    while i < n:
        p = paragraphs[i]
        if p.speaker not in host_spks or p.end <= intro_end_time:
            i += 1
            continue
        block_start = i
        host_words = 0
        j = i
        last_host_j = i - 1
        outro_hit = False
        while j < n:
            pj = paragraphs[j]
            if pj.speaker in host_spks:
                # Check for outro promo — stop the block before it.
                pj_text = " ".join(s.text for s in pj.sentences)
                if is_outro_promo(pj_text):
                    outro_hit = True
                    break
                w = sum(len(s.text.split()) for s in pj.sentences
                        if not is_filler_sentence(s.text))
                host_words += w
                last_host_j = j
                j += 1
            else:
                # Guest paragraph — break only on a new substantive question.
                if is_substantive_new_question(pj):
                    break
                # Otherwise the guest is responding to Alex's probe; keep the
                # block alive so we can capture Alex's eventual punchline.
                j += 1

        # Trim trailing host paragraphs whose only content is a '?' sentence
        # — those are Alex's probes, not advice. We walk back through the
        # block while the last host sentence ends with '?', dropping that
        # host paragraph and re-pointing last_host_j to the prior host run.
        while last_host_j > block_start and last_host_sentence_ends_with_q(
            last_host_j, block_start
        ):
            # Try shrinking last_host_j back to the previous host paragraph
            new_end = last_host_j - 1
            while new_end > block_start and paragraphs[new_end].speaker not in host_spks:
                new_end -= 1
            if new_end <= block_start:
                break
            last_host_j = new_end

        if host_words >= MIN_ANSWER_WORDS and not last_host_sentence_ends_with_q(
            last_host_j, block_start
        ):
            answer_blocks.append((block_start, last_host_j, host_words))
        # Always advance i. If we broke immediately on the outro or a new
        # question, j may still equal i — bump past block_start to avoid an
        # infinite loop.
        i = max(j, block_start + 1)

    # --- Step 2: for each answer block, find the preceding question/setup ---
    pairs: list[QAPair] = []
    used_qa_word_starts: set[int] = set()
    intro_has_setup = is_question_or_setup(intro_text)
    for block_pos, (a_start_idx, a_end_idx, host_words) in enumerate(answer_blocks):
        # Collect answer sentences in the block (host turns only, drop backchannels)
        answer_sents: list[Sentence] = []
        for k in range(a_start_idx, a_end_idx + 1):
            pk = paragraphs[k]
            if pk.speaker not in host_spks:
                continue
            for s in pk.sentences:
                if is_backchannel_sentence(s.text):
                    continue
                answer_sents.append(s)
        if not answer_sents:
            continue
        a_w_start = min(min(s.word_indices) for s in answer_sents if s.word_indices)
        a_w_end = max(max(s.word_indices) for s in answer_sents if s.word_indices)

        # Walk backwards from a_start_idx to find guest setup. We accumulate
        # guest sentences (skipping backchannels and host interjections) until
        # one of:
        #   - we cross before intro_end_time (-1s slack)
        #   - we've collected an explicit '?' sentence and have enough words
        #   - we hit the previous answer_block
        prev_answer_end = -1
        for (prev_start, prev_end, _) in answer_blocks:
            if prev_end < a_start_idx and prev_end > prev_answer_end:
                prev_answer_end = prev_end

        q_sents_reversed: list[Sentence] = []
        explicit_q = False
        total_q_words = 0
        j = a_start_idx - 1
        while j > prev_answer_end and j >= 0:
            pa = paragraphs[j]
            # Stop if we cross back to before the intro window
            if pa.end < intro_end_time - 1.0:
                break
            if pa.speaker == guest_spk:
                for s in reversed(pa.sentences):
                    if is_backchannel_sentence(s.text):
                        continue
                    q_sents_reversed.append(s)
                    total_q_words += len(s.text.split())
                    if s.text.strip().endswith("?"):
                        explicit_q = True
                # Stop early once we have a '?' and enough words
                if explicit_q and total_q_words >= 6:
                    break
            j -= 1

        if not q_sents_reversed:
            # The whole setup is inside the intro. If this is the first answer
            # block AND the intro carries a real question/setup, emit a clip
            # whose question slot is empty (intro carries everything).
            if block_pos == 0 and intro_has_setup:
                # Use a sentinel q-range that lies fully inside the intro so
                # downstream code folds it.
                q_w_start = intro_end_word
                q_w_end = intro_end_word
                if q_w_start in used_qa_word_starts:
                    continue
                used_qa_word_starts.add(q_w_start)
                pairs.append(QAPair(
                    question_sents=[],
                    answer_sents=answer_sents,
                    q_word_range=(q_w_start, q_w_end),
                    a_word_range=(a_w_start, a_w_end),
                    context_dependent=False,
                    notes=["question_in_intro"],
                ))
            continue
        question_sents = list(reversed(q_sents_reversed))

        # If the question is just a few words and lacks a '?', it's probably
        # not a real question setup — try widening backward a bit more (up to
        # 30 guest words) before giving up.
        if not explicit_q and total_q_words < 8:
            j2 = j  # continue from where we stopped
            while j2 >= 0 and total_q_words < 30:
                pa = paragraphs[j2]
                if pa.end < intro_end_time - 1.0:
                    break
                if pa.speaker == guest_spk:
                    for s in reversed(pa.sentences):
                        if is_backchannel_sentence(s.text):
                            continue
                        q_sents_reversed.append(s)
                        total_q_words += len(s.text.split())
                j2 -= 1
            question_sents = list(reversed(q_sents_reversed))

        if total_q_words < 4:
            continue

        q_w_start = min(min(s.word_indices) for s in question_sents if s.word_indices)
        q_w_end = max(max(s.word_indices) for s in question_sents if s.word_indices)

        # Filter: question block must contain a real question or problem statement.
        # If it's just the guest answering Alex's probe ("Yeah, performance based"
        # / "Three months" / "I do"), skip — that's not a clippable Q.
        combined_q_text = " ".join(s.text for s in question_sents)
        # If the question is folded into the intro, the intro itself carries the
        # setup — allow if intro_text contains question/setup content.
        intro_carries_setup = (
            q_w_start <= intro_end_word and is_question_or_setup(intro_text)
        )
        # Reject explicit-`?` matches whose `?` sentence is short (usually
        # diarization mis-attributing host probes like "What else?" to the
        # guest) OR is an interjection like "What did you ask me again?".
        if explicit_q:
            q_sentences_with_qmark = [
                s for s in question_sents if s.text.strip().endswith("?")
            ]
            substantive_q_sentences = [
                s for s in q_sentences_with_qmark
                if len(s.text.split()) >= 5 and not is_interjection_question(s.text)
            ]
            if not substantive_q_sentences:
                explicit_q = False
        # For the FIRST answer block in the video, be lenient: even without an
        # explicit `?` or question phrase, the guest's setup IS the implicit
        # question. Require some substance (>=15 guest words) so we don't pick
        # up trivial responses.
        first_block_lenient = (
            block_pos == 0 and total_q_words >= 15
        )
        if not (explicit_q or is_question_or_setup(combined_q_text)
                or intro_carries_setup or first_block_lenient):
            continue

        # Dedupe: don't emit two pairs whose questions start at the same word.
        if q_w_start in used_qa_word_starts:
            continue
        used_qa_word_starts.add(q_w_start)

        # Context-dependence: based on the FIRST kept question sentence.
        head_text = question_sents[0].text
        context_dep = is_anaphoric_question(head_text)

        notes: list[str] = []
        if not explicit_q:
            notes.append("implicit_question")

        pairs.append(QAPair(
            question_sents=question_sents,
            answer_sents=answer_sents,
            q_word_range=(q_w_start, q_w_end),
            a_word_range=(a_w_start, a_w_end),
            context_dependent=context_dep,
            notes=notes,
        ))
    return pairs


# ---- trimming ------------------------------------------------------------

def compute_kept_words(words: list[Word], ranges: list[tuple[int, int]],
                      target_spk: int | None = None,
                      strip_other_spk: bool = False,
                      strip_fillers: bool = False,
                      strip_backchannels: bool = True) -> list[Word]:
    """Walk through the union of given (w_start, w_end) ranges in order and
    apply a configurable set of cuts at the word level. Returns the kept list."""
    kept: list[Word] = []
    for (a, b) in ranges:
        i = a
        n = len(words)
        while i <= b and i < n:
            w = words[i]
            sent_text = w.sent_text.lower().strip()

            # Backchannel sentence removal (skip the whole sentence)
            if strip_backchannels and is_backchannel_sentence(w.sent_text):
                # advance to end of this sentence
                while i + 1 <= b and i + 1 < n and words[i + 1].sent_idx == w.sent_idx:
                    i += 1
                i += 1
                continue

            # Strip the OTHER speaker's words inside the segment
            if strip_other_spk and target_spk is not None and w.speaker != target_spk:
                i += 1
                continue

            # Phrase fillers
            if strip_fillers:
                matched_phrase = False
                for phr in sorted(FILLER_PHRASES, key=len, reverse=True):
                    if i + len(phr) - 1 <= b and i + len(phr) - 1 < n:
                        bs = [words[i + k].bare for k in range(len(phr))]
                        if bs == phr:
                            i += len(phr)
                            matched_phrase = True
                            break
                if matched_phrase:
                    continue

            # Single-token fillers
            if strip_fillers and w.bare in FILLER_WORDS:
                # only strip if it's not the only content word in its sentence
                i += 1
                continue

            kept.append(w)
            i += 1
    return kept


def kept_duration(kept: list[Word]) -> float:
    """Sum of contiguous spans in kept words (drop the gaps that come from
    trimmed material)."""
    if not kept:
        return 0.0
    total = 0.0
    seg_start = kept[0].start
    last_end = kept[0].end
    for w in kept[1:]:
        # Treat consecutive original-indices as one playback segment, since the
        # edit will splice cuts together.
        if w.idx == kept[kept.index(w) - 1].idx + 1:
            last_end = w.end
        else:
            total += last_end - seg_start
            seg_start = w.start
            last_end = w.end
    total += last_end - seg_start
    return total


def kept_segments(kept: list[Word]) -> list[tuple[float, float]]:
    """Group kept words into contiguous (by original index) playback segments."""
    segs: list[tuple[float, float]] = []
    if not kept:
        return segs
    seg_start = kept[0].start
    last_end = kept[0].end
    last_idx = kept[0].idx
    for w in kept[1:]:
        if w.idx == last_idx + 1:
            last_end = w.end
        else:
            segs.append((seg_start, last_end))
            seg_start = w.start
            last_end = w.end
        last_idx = w.idx
    segs.append((seg_start, last_end))
    return segs


def edit_duration(segs: list[tuple[float, float]]) -> float:
    return sum(e - s for s, e in segs)


def words_text(words: list[Word]) -> str:
    return " ".join(w.text for w in words)


# ---- clip assembly -------------------------------------------------------

@dataclass
class ClipCandidate:
    intro_range: tuple[int, int]
    q_range: tuple[int, int]
    a_range: tuple[int, int]
    kept_segments: list[tuple[float, float]]
    intro_text: str
    question_text: str
    answer_text: str
    raw_seconds: float
    edit_seconds: float
    trims_applied: list[str]
    score: float
    notes: list[str]
    context_dependent: bool


def _sentences_in_range(words: list[Word], r: tuple[int, int]) -> list[int]:
    """All sentence IDs that have at least one word in [r[0], r[1]]."""
    if r is None:
        return []
    out: list[int] = []
    seen: set[int] = set()
    for i in range(r[0], r[1] + 1):
        sid = words[i].sent_idx
        if sid not in seen:
            seen.add(sid)
            out.append(sid)
    return out


def _build_sentence_segments(
    words: list[Word], sentences: dict[int, Sentence],
    kept_sent_ids: set[int], merge_gap_s: float = 0.4,
    tail_pad_s: float = 0.35,
) -> list[tuple[float, float]]:
    """Render kept sentences into playback segments. Adjacent kept sentences in
    the source video get merged into one continuous segment, and small gaps
    (<merge_gap_s) get bridged. Each segment's end gets padded by tail_pad_s
    seconds so the final word's audio isn't cut off mid-phoneme, clamped so it
    doesn't run into the *next* non-kept sentence."""
    if not kept_sent_ids:
        return []
    kept = sorted(
        (sentences[sid] for sid in kept_sent_ids if sid in sentences),
        key=lambda s: s.start,
    )
    if not kept:
        return []
    # Build a sorted list of ALL sentence starts (for the tail-pad clamp).
    all_sent_starts = sorted(s.start for s in sentences.values())
    import bisect

    def pad_end(end: float) -> float:
        # Extend end by tail_pad_s, but stop well before the next sentence
        # would otherwise begin. We use a 0.15s safety buffer so we don't
        # bleed into a dropped sentence's opening syllable (the previous
        # ~0.02s clamp was too tight — audio of the next word could still
        # leak through).
        target = end + tail_pad_s
        idx = bisect.bisect_right(all_sent_starts, end)
        if idx < len(all_sent_starts):
            next_start = all_sent_starts[idx]
            safe_end = max(end + 0.05, next_start - 0.15)
            return min(target, safe_end)
        return target

    segs: list[list[float]] = [[kept[0].start, pad_end(kept[0].end)]]
    for s in kept[1:]:
        s_end_padded = pad_end(s.end)
        if s.start - segs[-1][1] <= merge_gap_s:
            segs[-1][1] = max(segs[-1][1], s_end_padded)
        else:
            segs.append([s.start, s_end_padded])
    return [(a, b) for a, b in segs]


def build_clip(words: list[Word], intro_range: tuple[int, int], qa: QAPair,
               guest_spk: int, host_spk: int,
               sentences: dict[int, Sentence] | None = None,
               allow_diagnostic: bool = False) -> ClipCandidate | None:
    intro_w = (intro_range[0], intro_range[1])
    q_w = qa.q_word_range
    a_w = qa.a_word_range

    # If the question is entirely inside the intro, the intro already contains
    # the question — collapse the Q range to a no-op (empty interval).
    q_in_intro = q_w[0] >= intro_w[0] and q_w[1] <= intro_w[1]
    if not q_in_intro and q_w[0] <= intro_w[1] < q_w[1]:
        q_w = (intro_w[1] + 1, q_w[1])
    if q_in_intro:
        q_w = None

    # --- Sentence-level trim pipeline -------------------------------------
    # Determine the SET of sentences to keep (intro + question + answer,
    # minus pure backchannels). Then build playback segments by sorting kept
    # sentences in time and merging any two that sit within MERGE_GAP_S of
    # each other (so natural pauses between Alex's sentences stitch into one
    # smooth segment, but a 30-second host-monologue-then-guest-Q gap stays
    # as a real cut). This is the right balance between "no micro-cuts" and
    # "don't drag in 200s of irrelevant cross-talk".
    MERGE_GAP_S = 2.0
    assert sentences is not None, "build_clip requires sentence map"

    intro_sent_ids = _sentences_in_range(words, intro_w)
    q_sent_ids = _sentences_in_range(words, q_w) if q_w is not None else []
    a_sent_ids = _sentences_in_range(words, a_w)

    def drop_filler(ids: list[int]) -> list[int]:
        # Drop backchannels, mic-noise repair lines, and outro promo lines.
        return [sid for sid in ids
                if not is_filler_sentence(sentences[sid].text)]

    intro_keep = drop_filler(intro_sent_ids)
    q_keep = drop_filler(q_sent_ids)
    a_keep = drop_filler(a_sent_ids)

    if not intro_keep or not a_keep:
        return None

    def render(intro_ids, q_ids, a_ids):
        kept_ids = set(intro_ids) | set(q_ids) | set(a_ids)
        segs = _build_sentence_segments(
            words, sentences, kept_ids, merge_gap_s=MERGE_GAP_S,
        )
        return segs, sum(e - s for s, e in segs)

    segs, edit_s = render(intro_keep, q_keep, a_keep)
    trims: list[str] = []

    # ---- Tier 1: drop low-value MIDDLE answer sentences first --------------
    # The user explicitly wants Alex's conclusion preserved over middle
    # diagnostic content. We always keep the last CONCLUSION_KEEP substantive
    # answer sentences (the punchline zone). Above that floor, we rank
    # middle sentences by drop priority:
    #   priority 1: host questions (Alex's diagnostic probes) — drop first
    #   priority 2: very short host sentences (≤4 words) — likely transitions
    #   priority 3: non-question host sentences (real content) — last resort
    a_sorted = sorted(a_keep, key=lambda sid: sentences[sid].start)

    def drop_priority(sid: int) -> int:
        s = sentences[sid]
        text = s.text.strip()
        wc = len(text.split())
        if text.endswith("?"):
            return 1  # diagnostic probe — drop first
        if wc <= 4:
            return 2  # short transition — drop next
        return 3  # real content — last resort

    # Indices in a_sorted that we're allowed to drop (everything except the
    # last CONCLUSION_KEEP and the first 2, which are the entry into Alex's
    # answer — preserve the lead-in).
    def droppable_indices(current: list[int]) -> list[int]:
        if len(current) <= CONCLUSION_KEEP + 2:
            return []
        return list(range(2, len(current) - CONCLUSION_KEEP))

    dropped_middle: list[int] = []
    if edit_s > MAX_SECONDS:
        current = list(a_sorted)
        while edit_s > MAX_SECONDS:
            idxs = droppable_indices(current)
            if not idxs:
                break
            # Pick the next drop: lowest priority first, then earliest in time.
            idxs.sort(key=lambda i: (drop_priority(current[i]), i))
            drop_idx = idxs[0]
            dropped_middle.append(current[drop_idx])
            current = current[:drop_idx] + current[drop_idx + 1:]
            segs, edit_s = render(intro_keep, q_keep, current)
        if dropped_middle:
            a_keep = current
            trims.append(f"drop_middle_answer:{len(dropped_middle)}")

    # ---- Tier 1b: if still over, drop from the END (last resort) -----------
    a_keep_sorted = sorted(a_keep, key=lambda sid: sentences[sid].start)
    n_drop_tail = 0
    while edit_s > MAX_SECONDS and len(a_keep_sorted) - n_drop_tail > 3:
        n_drop_tail += 1
        trimmed_a = a_keep_sorted[:-n_drop_tail]
        segs, edit_s = render(intro_keep, q_keep, trimmed_a)
    # Ensure the last kept answer sentence isn't a question (or any of the
    # last 3, which would feel like an unresolved probing exchange).
    def last_three_have_q(seq: list[int]) -> bool:
        tail = seq[-3:] if len(seq) >= 3 else seq
        return any(sentences[sid].text.strip().endswith("?") for sid in tail)
    seq = a_keep_sorted[:-n_drop_tail] if n_drop_tail else list(a_keep_sorted)
    while len(seq) > 3 and last_three_have_q(seq):
        n_drop_tail += 1
        seq = a_keep_sorted[:-n_drop_tail]
        segs, edit_s = render(intro_keep, q_keep, seq)
    if n_drop_tail > 0:
        a_keep = seq
        trims.append(f"drop_answer_tail:{n_drop_tail}")

    # Tier 2: drop question sentences from the FRONT (keep what's nearest
    # the answer). Floor at 1.
    q_sorted = sorted(q_keep, key=lambda sid: sentences[sid].start)
    n_drop_q = 0
    while edit_s > MAX_SECONDS and len(q_sorted) - n_drop_q > 1:
        n_drop_q += 1
        trimmed_q = q_sorted[n_drop_q:]
        segs, edit_s = render(intro_keep, trimmed_q, a_keep)
    if n_drop_q > 0:
        q_keep = q_sorted[n_drop_q:]
        trims.append(f"drop_question_sentences:{n_drop_q}")

    # Tier 3: shave intro from the FRONT (drop earliest intro sentences).
    # Keep at least 1 — the "who I am / what I sell" hook.
    intro_sorted = sorted(intro_keep, key=lambda sid: sentences[sid].start)
    n_drop_i = 0
    while edit_s > MAX_SECONDS and len(intro_sorted) - n_drop_i > 1:
        n_drop_i += 1
        trimmed_i = intro_sorted[n_drop_i:]
        segs, edit_s = render(trimmed_i, q_keep, a_keep)
    if n_drop_i > 0:
        intro_keep = intro_sorted[n_drop_i:]
        trims.append(f"shave_intro_front:{n_drop_i}")

    # Final viability check
    if edit_s < MIN_SECONDS:
        return None

    # Reject clips whose answer is dominated by Alex's diagnostic questions
    # (no substantive advice). Heuristics:
    #   - >30% of non-backchannel answer sentences end with '?'
    #   - tiny average sentence length signals rapid-fire diagnostic exchange
    answer_real_sents = [
        s for s in qa.answer_sents if not is_backchannel_sentence(s.text)
    ]
    if len(answer_real_sents) >= 4 and not allow_diagnostic:
        n_q = sum(1 for s in answer_real_sents if s.text.strip().endswith("?"))
        q_ratio = n_q / len(answer_real_sents)
        avg_words = sum(len(s.text.split()) for s in answer_real_sents) / len(answer_real_sents)
        if q_ratio > 0.30 or (q_ratio > 0.15 and avg_words < 5.5):
            return None

    # Score the clip:
    # +1.0 for hitting the 30-55s sweet spot
    # -1.0 if over budget (we still output, but flag)
    score = 1.0
    if edit_s > MAX_SECONDS:
        score -= 0.5 * (edit_s - MAX_SECONDS) / 5.0
    elif edit_s > TARGET_SECONDS:
        score -= 0.15 * (edit_s - TARGET_SECONDS) / 5.0  # soft penalty over 90s
    if 45.0 <= edit_s <= 90.0:
        score += 0.5  # sweet spot
    if qa.context_dependent:
        score -= 0.7

    notes = list(qa.notes)
    if edit_s > MAX_SECONDS:
        notes.append(f"over_budget:{edit_s:.1f}s")

    # Text reflects exactly what's in the audio after sentence-level trims.
    def sents_text(sids: list[int]) -> str:
        ordered = sorted(sids, key=lambda x: sentences[x].start)
        return " ".join(sentences[sid].text for sid in ordered)

    intro_text = sents_text(intro_keep)
    question_text = sents_text(q_keep)
    answer_text = sents_text(a_keep)
    raw_seconds = (words[a_w[1]].end - words[intro_w[0]].start)

    return ClipCandidate(
        intro_range=intro_w,
        q_range=q_w if q_w is not None else (intro_w[1], intro_w[1]),
        a_range=a_w,
        kept_segments=segs,
        intro_text=intro_text,
        question_text=question_text,
        answer_text=answer_text,
        raw_seconds=raw_seconds,
        edit_seconds=edit_s,
        trims_applied=trims,
        score=score,
        notes=notes,
        context_dependent=qa.context_dependent,
    )


# ---- per-video pipeline --------------------------------------------------

def process_video(transcript_path: Path) -> dict:
    words, sentences, paragraphs, duration = load_transcript(transcript_path)
    guest_spk, scores = detect_guest_speaker(paragraphs)
    host_candidates = [
        spk for spk in scores
        if spk != guest_spk and scores[spk]["words"] / max(1, sum(s["words"] for s in scores.values())) >= 0.05
    ]
    host_spk = host_candidates[0] if host_candidates else (1 - guest_spk if guest_spk in (0, 1) else 0)

    intro_range = extract_intro_span(words, paragraphs, guest_spk)
    if intro_range is None:
        return {
            "video_id": transcript_path.stem.replace(".deepgram", ""),
            "duration": duration,
            "guest_speaker": guest_spk,
            "host_speaker": host_spk,
            "clips": [],
            "error": "no_intro_detected",
        }

    intro_text_full = " ".join(
        w.text for w in words[intro_range[0]:intro_range[1] + 1]
    )
    qa_pairs = find_qa_pairs(
        paragraphs, guest_spk, intro_range[1], words, intro_text=intro_text_full,
    )

    # CONSTRAINT: one clip per video. The intro's question (or the first
    # question right after the intro) must be answered in that single clip.
    # Later Q&A pairs in the source video are intentionally discarded — they
    # don't share the intro's question, so reusing the intro would be
    # misleading. We only consider the FIRST viable Q&A pair (earliest by
    # question start), so the intro stays tied to its own question.
    qa_pairs.sort(key=lambda q: q.q_word_range[0])

    # Sentence map for sentence-level trim pipeline.
    sentence_map = {s.idx: s for s in sentences}

    clips: list[ClipCandidate] = []
    for qa in qa_pairs:
        clip = build_clip(
            words, intro_range, qa, guest_spk, host_spk,
            sentences=sentence_map,
        )
        if clip is not None:
            clips.append(clip)
            break  # only the first viable pair
    # Fallback: if every Q&A pair got filtered out (e.g. all flagged as
    # diagnostic), retry with diagnostic filter relaxed so the video isn't
    # left with zero clips. Diagnostic clips are still valuable context.
    if not clips and qa_pairs:
        for qa in qa_pairs:
            clip = build_clip(
                words, intro_range, qa, guest_spk, host_spk,
                sentences=sentence_map, allow_diagnostic=True,
            )
            if clip is not None:
                clip.notes.append("diagnostic_fallback")
                clips.append(clip)
                break

    return {
        "video_id": transcript_path.stem.replace(".deepgram", ""),
        "duration": duration,
        "guest_speaker": guest_spk,
        "host_speaker": host_spk,
        "speaker_scores": scores,
        "n_qa_pairs": len(qa_pairs),
        "clips": [asdict(c) for c in clips],
    }


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    transcripts = sorted(TRANS_DIR.glob("*.deepgram.json"))
    summary = []
    for tp in transcripts:
        result = process_video(tp)
        out_path = OUT_DIR / f"{result['video_id']}.clips.json"
        out_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
        n = len(result.get("clips", []))
        cd = sum(1 for c in result.get("clips", []) if c["context_dependent"])
        over = sum(1 for c in result.get("clips", []) if c["edit_seconds"] > MAX_SECONDS)
        summary.append((result["video_id"], n, cd, over, result["duration"]))
        print(f"{result['video_id']:18s} clips={n:2d}  ctx_dep={cd}  over{int(MAX_SECONDS)}s={over}  dur={result['duration']:.0f}s")
    print()
    print(f"Total videos processed: {len(transcripts)}")
    total_clips = sum(s[1] for s in summary)
    total_cd = sum(s[2] for s in summary)
    total_over = sum(s[3] for s in summary)
    print(f"Total clips: {total_clips}, context-dependent: {total_cd}, "
          f"over {int(MAX_SECONDS)}s: {total_over}")


if __name__ == "__main__":
    main()
