"""Pair Songsterr `newLyrics` syllables with vocal-track beats.

Songsterr stores lyrics as a flat string of pre-syllabified tokens (with
trailing `-` for hyphenation) inside each vocal track's notes JSON. Timing
comes from the track's beats: each non-rest, non-tied beat consumes the
next syllable in order. Phrase boundaries are derived from rests >= one
quarter note.
"""

from __future__ import annotations

import re
from fractions import Fraction
from typing import List, Tuple

# Match TICKS_PER_BEAT in chart.py — we don't import to avoid a cycle.
_TICKS_PER_BEAT = 480

def tokenize(text: str) -> List[str]:
    """Split a `newLyrics` text blob into syllables in chart order.

    Mirrors Songsterr's frontend tokenizer (`yD` in common.js): bare `-`
    splits a word into two syllables (the left half keeps a trailing `-`
    for CH's hyphenation marker), `_` is treated as a separator, parens
    are stripped but their content is kept, and whitespace separates
    tokens. Normalizes fancy dashes (`—`, `–`) to plain `-` first.
    """
    if not text:
        return []
    cleaned = text.replace("\t", " ")
    cleaned = re.sub(r"[—–]", "-", cleaned)
    cleaned = cleaned.replace("(", "").replace(")", "")
    cleaned = cleaned.replace("_", " ")
    out: List[str] = []
    for word in cleaned.split():
        pieces = word.split("-")
        if len(pieces) == 1:
            if pieces[0]:
                out.append(pieces[0])
            continue
        for i, p in enumerate(pieces):
            if not p:
                continue
            if i < len(pieces) - 1:
                out.append(p + "-")
            else:
                out.append(p)
    return out


def has_lyrics(notes_dict: dict) -> bool:
    """True iff this vocal track has usable lyric data."""
    if not notes_dict.get("withLyrics"):
        return False
    blob = notes_dict.get("newLyrics") or []
    text = " ".join((item.get("text") or "") for item in blob)
    return bool(tokenize(text))


def walk(notes_dict: dict) -> Tuple[List[Tuple[int, str]], List[Tuple[int, int]]]:
    """Return (lyric_events, phrase_ranges) for a vocal track's notes JSON.

    lyric_events: [(tick, syllable), ...] in tick order.
    phrase_ranges: [(start_tick, end_tick), ...] derived from vocal rests
      of >= one quarter note. Each range covers a contiguous run of
      syllables; the chart writer emits phrase_start/_end at its bounds.
    """
    blob = notes_dict.get("newLyrics") or []
    text = " ".join((item.get("text") or "") for item in blob)
    tokens = tokenize(text)
    if not tokens:
        return [], []

    measures = notes_dict.get("measures", [])
    events: List[Tuple[int, str]] = []
    rest_breaks: List[int] = []  # tick where a >= 1-beat rest starts

    cursor = 0
    sig = (4, 4)
    measure_start = 0
    for measure in measures:
        if "signature" in measure:
            sig = (measure["signature"][0], measure["signature"][1])
        for voice in measure.get("voices", []):
            voice_tick = measure_start
            for beat in voice.get("beats", []):
                dur_ticks = _beat_ticks(beat)
                is_rest = bool(beat.get("rest"))
                tied = any(n.get("tie") for n in beat.get("notes", []) or [])
                if not is_rest and not tied and cursor < len(tokens):
                    events.append((voice_tick, tokens[cursor]))
                    cursor += 1
                if is_rest and dur_ticks >= _TICKS_PER_BEAT:
                    rest_breaks.append(voice_tick)
                voice_tick += dur_ticks
        measure_start += int(Fraction(sig[0], sig[1]) * 4 * _TICKS_PER_BEAT)

    if not events:
        return [], []

    phrases: List[Tuple[int, int]] = []
    phrase_start = events[0][0]
    for i in range(1, len(events)):
        prev = events[i - 1][0]
        cur = events[i][0]
        if any(prev < bt <= cur for bt in rest_breaks):
            phrases.append((phrase_start, prev))
            phrase_start = cur
    phrases.append((phrase_start, events[-1][0]))

    return events, phrases


def _beat_ticks(beat: dict) -> int:
    dur = beat.get("duration")
    if dur:
        return int(Fraction(dur[0], dur[1]) * 4 * _TICKS_PER_BEAT)
    btype = beat.get("type", 4)
    return (4 * _TICKS_PER_BEAT) // btype if btype else _TICKS_PER_BEAT
