"""Pair Songsterr `newLyrics` syllables with vocal-track beats.

Songsterr stores lyrics as a flat string of pre-syllabified tokens inside
each vocal track's notes JSON. Timing comes from the track's beats: each
non-rest, non-tied beat consumes from a token stream that interleaves
lyric tokens with whitespace tokens. Two consecutive whitespace tokens
produce an empty syllable on a beat — that's how `__` markers in the
source act as held-note placeholders. Replicating this is required for
syllables to land on the right beats when the source uses `__`.

Phrase boundaries are derived from explicit gaps: either a rest >= one
quarter note in the vocal track, or a run of empty syllables (from `__`
markers).
"""

from __future__ import annotations

import re
from fractions import Fraction
from typing import List, Tuple

# Match TICKS_PER_BEAT in chart.py — we don't import to avoid a cycle.
_TICKS_PER_BEAT = 480

# Songsterr's tokenizer match pattern (SD in common.js): comments in
# brackets, dashes (with optional surrounding whitespace), or whitespace.
_SD_PATTERN = re.compile(r"(\[.*?\]|\s?-\s?)|\r\n|\n|\s")


def _sd_tokenize(text: str) -> List[dict]:
    """Tokenize a `newLyrics` blob into typed tokens.

    Returns a list of {type, text} tokens where type is `lyric`, `space`,
    or `gap`. `gap` tokens are produced by `__+` markers in the source —
    explicit silence placeholders that should be paired with one beat
    each. Other whitespace yields plain `space` tokens.
    """
    if not text:
        return []
    e = text.replace("\t", " ")
    e = re.sub(r"[—–]", "-", e)
    # Collapse runs of underscores to a single sentinel so we can detect
    # them as `gap` tokens. \x00 won't appear in real lyrics.
    e = re.sub(r"_+", "\x00", e)
    e = e + "\n"
    out: List[dict] = []
    r = 0
    pattern = re.compile(r"(\[.*?\]|\s?-\s?)|\x00|\r\n|\n|\s")
    for m in pattern.finditer(e):
        match_text = m.group(0)
        if match_text == "\x00":
            ttype = "gap"
        elif match_text.startswith("["):
            ttype = "comment"
        else:
            ttype = "space"
        i = {"text": match_text, "pos": m.start(), "type": ttype}
        if i["pos"] > r:
            a_text = e[r:i["pos"]]
            if ttype == "space":
                i["text"] = i["text"].replace("\r", "")
                if "-" in i["text"]:
                    a_text += "-"
                    i["text"] = re.sub(r"\s?-\s?", "", i["text"])
            a_text = re.sub(r"^.*\]", "", a_text)
            a_text = a_text.replace("(", "").replace(")", "")
            out.append({"text": a_text, "type": "lyric"})
        r = i["pos"] + len(match_text)
        if ttype != "comment":
            out.append(i)
    return out


def tokenize(text: str) -> List[str]:
    """Return the flat list of lyric strings (for `has_lyrics` and tests)."""
    return [t["text"] for t in _sd_tokenize(text) if t["type"] == "lyric" and t["text"]]


def has_lyrics(notes_dict: dict) -> bool:
    """True iff this vocal track has usable lyric data."""
    if not notes_dict.get("withLyrics"):
        return False
    blob = notes_dict.get("newLyrics") or []
    text = " ".join((item.get("text") or "") for item in blob)
    return bool(tokenize(text))


def walk(notes_dict: dict) -> Tuple[List[Tuple[int, str]], List[Tuple[int, int]]]:
    """Return (lyric_events, phrase_ranges) for a vocal track's notes JSON.

    Replicates Songsterr's per-beat consumption (CD in common.js): every
    non-rest beat advances through the token stream until a lyric token is
    found or two consecutive spaces yield an empty syllable. Tied beats
    run the same loop but never advance past a lyric token (extension
    credits, infinite when `lyricsOnTieNotes=false`). Empty syllables
    consume a beat without emitting a lyric event.
    """
    blob = notes_dict.get("newLyrics") or []
    text = " ".join((item.get("text") or "") for item in blob)
    tokens = _sd_tokenize(text)
    if not any(t["type"] == "lyric" and t["text"] for t in tokens):
        return [], []

    measures = notes_dict.get("measures", [])
    events: List[Tuple[int, str]] = []
    rest_breaks: List[int] = []
    empty_breaks: List[int] = []  # tick of last real syllable before an empty run

    cursor = 0
    prev_tok = None
    last_real_tick: int | None = None
    pending_empty = False

    sig = (4, 4)
    measure_start = 0
    for measure in measures:
        if "signature" in measure:
            sig = (measure["signature"][0], measure["signature"][1])
        for voice_idx, voice in enumerate(measure.get("voices", [])):
            if voice_idx > 0:
                continue  # Songsterr only walks voices[0] for lyrics
            voice_tick = measure_start
            for beat in voice.get("beats", []):
                dur_ticks = _beat_ticks(beat)
                is_rest = bool(beat.get("rest"))
                notes_arr = beat.get("notes") or []
                all_tied = bool(notes_arr) and all(n.get("tie") for n in notes_arr)
                if is_rest and dur_ticks >= _TICKS_PER_BEAT:
                    rest_breaks.append(voice_tick)
                if not is_rest:
                    syllable, cursor, prev_tok, gap = _consume_one_beat(
                        tokens, cursor, prev_tok, tied=all_tied)
                    if syllable:
                        if pending_empty and last_real_tick is not None:
                            empty_breaks.append(last_real_tick)
                            pending_empty = False
                        events.append((voice_tick, syllable))
                        last_real_tick = voice_tick
                    elif syllable == "" and gap and last_real_tick is not None:
                        pending_empty = True
                voice_tick += dur_ticks
        measure_start += int(Fraction(sig[0], sig[1]) * 4 * _TICKS_PER_BEAT)

    if not events:
        return [], []

    breaks = sorted(set(rest_breaks + empty_breaks))
    phrases: List[Tuple[int, int]] = []
    phrase_start = events[0][0]
    for i in range(1, len(events)):
        prev = events[i - 1][0]
        cur = events[i][0]
        # rest_breaks: a rest BETWEEN two syllables; empty_breaks: tick of last
        # real syllable before an empty run, so trigger when prev <= bt < cur.
        if any(prev < bt <= cur for bt in rest_breaks) or \
           any(prev <= bt < cur for bt in empty_breaks):
            phrases.append((phrase_start, prev))
            phrase_start = cur
    phrases.append((phrase_start, events[-1][0]))

    return events, phrases


def _consume_one_beat(tokens: List[dict], cursor: int, prev: dict | None,
                      tied: bool = False):
    """Consume tokens for a single beat.

    Returns (syllable, new_cursor, new_prev, gap). syllable is the lyric
    text, "" for an empty syllable absorbed from a `__` marker, or None
    if the stream is exhausted.

    Sounding beats always consume the next lyric token, skipping spaces
    and any double-space triggers (those are visual silence markers and
    don't take real beats). Tied/extension beats can absorb a double-
    space trigger in place of a syllable — that's how `__` markers slow
    the cursor when there are held notes covering the silence.
    """
    syllable = None
    gap = False
    while cursor < len(tokens) - 1:
        tok = tokens[cursor]
        if tok["type"] == "lyric":
            if tied:
                syllable = ""
                # Don't advance cursor — lyric stays for the next beat.
                break
            syllable = tok["text"]
            cursor += 1
            prev = tok
            break
        if tok["type"] == "gap":
            # Each `__` marker consumes exactly one beat (sounding or tied)
            # as silence. Phrase boundary.
            syllable = ""
            gap = True
            cursor += 1
            prev = tok
            break
        cursor += 1
        prev = tok
    return syllable, cursor, prev, gap


def _beat_ticks(beat: dict) -> int:
    dur = beat.get("duration")
    if dur:
        return int(Fraction(dur[0], dur[1]) * 4 * _TICKS_PER_BEAT)
    btype = beat.get("type", 4)
    return (4 * _TICKS_PER_BEAT) // btype if btype else _TICKS_PER_BEAT
