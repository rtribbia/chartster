"""Parse Songsterr JSON drum tabs into a native measure/voice/beat model.

This keeps the tab's musical structure intact (measures, signatures, tempo
automations, voices, beat durations) instead of flattening to a MIDI-like
event stream. Downstream consumers can reason in musical terms.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from fractions import Fraction
from typing import List, Optional, Tuple


@dataclass
class Note:
    fret: int
    string: Optional[float] = None
    ghost: bool = False
    accent: int = 0  # Songsterr "accentuated": 0=none, 1=accent, 2=ghost-alt


@dataclass
class Beat:
    duration: Fraction  # fraction of a whole note
    velocity: int       # MIDI 1-127
    rest: bool = False
    grace: bool = False  # "beforeBeat" grace notes steal time from next beat
    notes: List[Note] = field(default_factory=list)


@dataclass
class Voice:
    beats: List[Beat] = field(default_factory=list)


@dataclass
class Measure:
    index: int
    signature: Tuple[int, int]     # (num, den)
    triplet_feel: str = "off"      # "off" | "8th" | "16th" — propagates forward
    bpm: Optional[float] = None    # if a tempo change starts at measure start
    tempo_automations: List[Tuple[Fraction, float]] = field(default_factory=list)
    # ^ (beat-position within measure, bpm) — position is already fraction of a whole note
    section: Optional[str] = None  # CH section marker text (Songsterr `marker.text`)
    voices: List[Voice] = field(default_factory=list)


@dataclass
class Song:
    name: str
    measures: List[Measure]
    song_id: Optional[int] = None
    revision_id: Optional[int] = None


def parse(path: str) -> Song:
    """Parse a Songsterr JSON tab file into a Song."""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return parse_dict(data)


def parse_dict(data: dict) -> Song:
    """Parse an already-loaded Songsterr JSON dict into a Song."""
    from .mapping import DEFAULT_VELOCITY, VELOCITY_MAP

    name = data.get("name", "")

    # Pre-scan signatures so tempo automations can be slotted by measure index.
    sig_num, sig_den = 4, 4
    sigs: List[Tuple[int, int]] = []
    feel = "off"
    feels: List[str] = []
    for m in data["measures"]:
        if "signature" in m:
            sig_num, sig_den = m["signature"][0], m["signature"][1]
        sigs.append((sig_num, sig_den))
        if "tripletFeel" in m:
            feel = m["tripletFeel"]
        feels.append(feel)

    # Bucket tempo automations by measure index.
    tempo_by_measure: dict[int, List[Tuple[Fraction, float]]] = {}
    for t in data.get("automations", {}).get("tempo", []):
        m_idx = t["measure"]
        # Songsterr position: beats (quarter notes) offset within measure.
        # Convert to fraction-of-whole-note (our Beat.duration unit).
        pos_beats = t.get("position", 0)
        pos_frac = Fraction(pos_beats) / 4
        tempo_by_measure.setdefault(m_idx, []).append((pos_frac, float(t["bpm"])))

    measures: List[Measure] = []
    for m_idx, m in enumerate(data["measures"]):
        automations = sorted(tempo_by_measure.get(m_idx, []), key=lambda x: x[0])
        # Split: tempo at position 0 goes on measure.bpm, rest stay as automations.
        measure_bpm: Optional[float] = None
        mid_measure: List[Tuple[Fraction, float]] = []
        for pos, bpm in automations:
            if pos == 0:
                measure_bpm = bpm
            else:
                mid_measure.append((pos, bpm))

        marker = m.get("marker") or {}
        section = (marker.get("text") or "").strip() or None

        measure = Measure(
            index=m_idx,
            signature=sigs[m_idx],
            triplet_feel=feels[m_idx],
            bpm=measure_bpm,
            tempo_automations=mid_measure,
            section=section,
        )

        for v in m.get("voices", []):
            voice = Voice()
            for beat in v.get("beats", []):
                duration = _beat_duration(beat)
                vel = VELOCITY_MAP.get(beat.get("velocity", ""), DEFAULT_VELOCITY)

                notes: List[Note] = []
                if not beat.get("rest", False):
                    for n in beat.get("notes", []):
                        if n.get("rest", False):
                            continue
                        if n.get("fret") is None:
                            continue
                        notes.append(Note(
                            fret=n["fret"],
                            string=float(n["string"]) if n.get("string") is not None else None,
                            ghost=bool(n.get("ghost", False)),
                            accent=int(n.get("accentuated", 0)),
                        ))

                voice.beats.append(Beat(
                    duration=duration,
                    velocity=vel,
                    rest=beat.get("rest", False) or not notes,
                    grace=beat.get("graceNote") is not None,
                    notes=notes,
                ))
            measure.voices.append(voice)

        measures.append(measure)

    # Ensure at least one tempo exists at song start (default 120 if nothing set).
    if not any(m.bpm is not None for m in measures) and not any(m.tempo_automations for m in measures):
        if measures:
            measures[0].bpm = 120.0

    return Song(
        name=name,
        measures=measures,
        song_id=data.get("songId"),
        revision_id=data.get("revisionId"),
    )


def _beat_duration(beat: dict) -> Fraction:
    """Resolve a beat's duration as fraction-of-whole-note.

    When `duration` is explicit it already encodes dots/tuplets. Only apply
    dots manually when falling back to the `type` field.
    """
    dur = beat.get("duration")
    if dur:
        return Fraction(dur[0], dur[1])

    beat_type = beat.get("type", 4)
    frac = Fraction(1, beat_type)
    dots = beat.get("dots", 0)
    add = frac
    for _ in range(dots):
        add = add / 2
        frac += add
    return frac
