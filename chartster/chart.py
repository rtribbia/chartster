"""Render a Song into a Clone Hero .chart file."""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Dict, List, Optional, Set, Tuple

from .mapping import (
    SONGSTERR_TO_CH,
    Lane,
    accent_marker,
    classify_velocity,
    ghost_marker,
)
from .songsterr import Beat, Song

TICKS_PER_BEAT = 480  # quarter note


@dataclass
class Hit:
    tick: int
    fret: int
    velocity: int


def estimate_duration(song: Song) -> float:
    """Face-value duration in seconds from the tab's tempos + signatures."""
    _, tempos, _ = _flatten(song, two_tom_kit=False, mapping=SONGSTERR_TO_CH)
    if not tempos:
        return 0.0

    total_ticks = 0
    sig = (4, 4)
    for measure in song.measures:
        sig = measure.signature
        total_ticks += int(Fraction(sig[0], sig[1]) * 4 * TICKS_PER_BEAT)

    tempos_sorted = sorted(tempos, key=lambda t: t[0])
    total_seconds = 0.0
    for i, (tick, bpm) in enumerate(tempos_sorted):
        next_tick = tempos_sorted[i + 1][0] if i + 1 < len(tempos_sorted) else total_ticks
        span_ticks = max(0, next_tick - tick)
        total_seconds += span_ticks * 60.0 / (TICKS_PER_BEAT * bpm)
    return total_seconds


def render(
    song: Song,
    output_path: str,
    name: str = "",
    artist: str = "",
    charter: str = "Chartster",
    bpm_scale: float = 1.0,
    two_tom_kit: bool = False,
    tempos_override: Optional[List[Tuple[int, float]]] = None,
    mapping: Optional[Dict[int, Lane]] = None,
    chart_dynamics: bool = True,
    dynamics_enabled: Optional[Set[Tuple[int, bool, str]]] = None,
) -> dict:
    """Write a Clone Hero .chart file. Returns a summary dict.

    `mapping`: optional Songsterr-fret → CH-Lane dict. Frets absent from the
    dict are skipped. Defaults to SONGSTERR_TO_CH.

    `chart_dynamics`: emit ENABLE_CHART_DYNAMICS so CH renders ghost/accent
    markers. If False, markers are skipped altogether.

    `dynamics_enabled`: optional set of (lane, is_cymbal, kind) combos whose
    ghost/accent markers should be emitted. Combos not present are rendered
    as normal notes (no marker). None = enable all combos.
    """
    m = mapping if mapping is not None else SONGSTERR_TO_CH
    hits, tempos, time_sigs = _flatten(song, two_tom_kit, m)

    if tempos_override is not None:
        tempos = tempos_override

    if bpm_scale != 1.0:
        tempos = [(tick, bpm * bpm_scale) for tick, bpm in tempos]

    lines: List[str] = []
    _write_song(lines, name, artist, charter)
    _write_sync(lines, tempos, time_sigs)
    _write_events(lines, chart_dynamics)
    hand_warnings = _write_expert_drums(lines, hits, m, dynamics_enabled)

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
        f.write("\n")

    return {
        "hits": len(hits),
        "tempo_changes": len(tempos),
        "time_sig_changes": len(time_sigs),
        "hand_warnings": hand_warnings,
        "tempos": tempos,
    }


def _flatten(
    song: Song,
    two_tom_kit: bool,
    mapping: Dict[int, Lane],
) -> Tuple[List[Hit], List[Tuple[int, float]], List[Tuple[int, int, int]]]:
    """Walk the Song and emit flat tick-stamped hit/tempo/sig lists."""
    hits: List[Hit] = []
    tempos: List[Tuple[int, float]] = []
    time_sigs: List[Tuple[int, int, int]] = []

    tick = 0
    last_sig: Optional[Tuple[int, int]] = None

    for measure in song.measures:
        measure_start = tick
        if measure.signature != last_sig:
            time_sigs.append((measure_start, measure.signature[0], measure.signature[1]))
            last_sig = measure.signature

        if measure.bpm is not None:
            tempos.append((measure_start, measure.bpm))
        for pos_frac, bpm in measure.tempo_automations:
            tempos.append((measure_start + int(pos_frac * 4 * TICKS_PER_BEAT), bpm))

        # Pair unit for tripletFeel swing. None = no swing.
        pair_unit: Optional[Fraction] = None
        if measure.triplet_feel == "16th":
            pair_unit = Fraction(1, 16)
        elif measure.triplet_feel == "8th":
            pair_unit = Fraction(1, 8)

        # Each voice independently walks from measure_start.
        # beforeBeat grace notes do NOT advance the voice cursor. They're
        # stacked just before the next main beat, each at offset
        # -(sum of own + later grace durations) — matches Songsterr's
        # startTickOffset handling (common.js :1766-1771).
        for voice in measure.voices:
            voice_tick = measure_start
            grace_buffer: List[Beat] = []
            swing_roles = _swing_roles(voice.beats, pair_unit) if pair_unit else None

            for b_idx, beat in enumerate(voice.beats):
                if beat.grace:
                    grace_buffer.append(beat)
                    continue

                # Emit buffered graces stacked before voice_tick: each grace
                # at offset = -(sum of its own + following graces' durations).
                grace_dur_ticks = [int(g.duration * 4 * TICKS_PER_BEAT)
                                   for g in grace_buffer]
                trailing_sum = 0
                for i in range(len(grace_buffer) - 1, -1, -1):
                    trailing_sum += grace_dur_ticks[i]
                    g = grace_buffer[i]
                    g_tick = max(measure_start, voice_tick - trailing_sum)
                    if not g.rest:
                        for note in g.notes:
                            fret = note.fret
                            if two_tom_kit and fret == 45:
                                fret = 43
                            if fret not in mapping:
                                continue
                            vel = _apply_dynamics(g.velocity, note)
                            hits.append(Hit(g_tick, fret, vel))
                grace_buffer.clear()

                # Emit the main beat at voice_tick.
                if not beat.rest:
                    for note in beat.notes:
                        fret = note.fret
                        if two_tom_kit and fret == 45:
                            fret = 43
                        if fret not in SONGSTERR_TO_CH:
                            continue
                        vel = _apply_dynamics(beat.velocity, note)
                        hits.append(Hit(voice_tick, fret, vel))

                raw_ticks = int(beat.duration * 4 * TICKS_PER_BEAT)
                role = swing_roles[b_idx] if swing_roles else 0
                if role == 1:
                    advance_ticks = raw_ticks * 4 // 3
                elif role == 2:
                    advance_ticks = raw_ticks * 2 // 3
                else:
                    advance_ticks = raw_ticks
                voice_tick += advance_ticks

        # Measure length from signature (NOT voice position, which may drift
        # in malformed tabs).
        num, den = measure.signature
        measure_ticks = int(Fraction(num, den) * 4 * TICKS_PER_BEAT)
        tick = measure_start + measure_ticks

    if not tempos:
        tempos.append((0, 120.0))
    if not time_sigs:
        time_sigs.append((0, 4, 4))

    hits.sort(key=lambda h: (h.tick, h.fret))
    return hits, tempos, time_sigs


def _swing_roles(beats, pair_unit: Fraction) -> List[int]:
    """Identify swing-pair roles per beat: 0=straight, 1=first, 2=second.

    A pair is two consecutive non-grace beats both of duration pair_unit, where
    the first starts on a 2*pair_unit boundary within the measure. Swing is
    duration-preserving (4:2 ratio within the pair).
    """
    roles = [0] * len(beats)
    pos = Fraction(0)
    positions = []
    for b in beats:
        positions.append(pos)
        if not b.grace:
            pos += b.duration
    pair_stride = pair_unit * 2
    for i, b in enumerate(beats):
        if b.grace or b.duration != pair_unit:
            continue
        if positions[i] % pair_stride != 0:
            continue
        j = i + 1
        while j < len(beats) and beats[j].grace:
            j += 1
        if j >= len(beats) or beats[j].duration != pair_unit:
            continue
        roles[i] = 1
        roles[j] = 2
    return roles


def _apply_dynamics(base_velocity: int, note) -> int:
    if note.ghost:
        return max(1, base_velocity - 50)
    if note.accent == 1:
        return min(127, base_velocity + 20)
    if note.accent == 2:
        return max(1, base_velocity - 50)
    return base_velocity


def _write_song(lines: List[str], name: str, artist: str, charter: str) -> None:
    lines.append("[Song]")
    lines.append("{")
    lines.append(f'  Name = "{name}"')
    lines.append(f'  Artist = "{artist}"')
    lines.append(f'  Charter = "{charter}"')
    lines.append(f"  Resolution = {TICKS_PER_BEAT}")
    lines.append("  Offset = 0")
    lines.append("}")


def _write_sync(
    lines: List[str],
    tempos: List[Tuple[int, float]],
    time_sigs: List[Tuple[int, int, int]],
) -> None:
    lines.append("[SyncTrack]")
    lines.append("{")

    events: List[Tuple[int, int, str]] = []
    # secondary sort key: TS (0) before B (1) at the same tick
    for tick, num, den in time_sigs:
        exp = _den_to_exponent(den)
        body = f"TS {num}" if exp == 2 else f"TS {num} {exp}"
        events.append((tick, 0, body))
    for tick, bpm in tempos:
        events.append((tick, 1, f"B {round(bpm * 1000)}"))

    events.sort(key=lambda e: (e[0], e[1]))
    for tick, _, body in events:
        lines.append(f"  {tick} = {body}")
    lines.append("}")


def _den_to_exponent(den: int) -> int:
    exp = 0
    d = den
    while d > 1:
        d >>= 1
        exp += 1
    return exp


def _write_events(lines: List[str], chart_dynamics: bool = True) -> None:
    lines.append("[Events]")
    lines.append("{")
    # Required for CH to honor ghost/accent markers.
    if chart_dynamics:
        lines.append('  0 = E "ENABLE_CHART_DYNAMICS"')
    lines.append("}")


def _write_expert_drums(
    lines: List[str],
    hits: List[Hit],
    mapping: Dict[int, Lane],
    dynamics_enabled: Optional[Set[Tuple[int, bool, str]]] = None,
) -> int:
    lines.append("[ExpertDrums]")
    lines.append("{")

    hits = [h for h in hits if h.fret in mapping]

    by_tick: dict[int, list[int]] = {}
    for h in hits:
        lane = mapping[h.fret].lane
        by_tick.setdefault(h.tick, []).append(lane)
    hand_warnings = sum(
        1 for lanes in by_tick.values()
        if sum(1 for l in lanes if l != 0) > 2
    )

    # Collapse colliding hits on the same (tick, lane): cymbal > tom, then
    # lowest fret number as deterministic tiebreak. Keeps the .chart output
    # unambiguous and matches the "(winner)" callout in the mapping UI.
    dedup: Dict[Tuple[int, int], Hit] = {}
    for h in hits:
        lane = mapping[h.fret]
        key = (h.tick, lane.lane)
        existing = dedup.get(key)
        if existing is None:
            dedup[key] = h
            continue
        existing_lane = mapping[existing.fret]
        if lane.is_cymbal and not existing_lane.is_cymbal:
            dedup[key] = h
        elif lane.is_cymbal == existing_lane.is_cymbal and h.fret < existing.fret:
            dedup[key] = h
    hits = sorted(dedup.values(), key=lambda h: (h.tick, h.fret))

    for h in hits:
        ch = mapping[h.fret]
        lines.append(f"  {h.tick} = N {ch.lane} 0")
        if ch.cymbal_marker is not None:
            lines.append(f"  {h.tick} = N {ch.cymbal_marker} 0")
        v = classify_velocity(h.velocity)
        if v == "normal":
            continue
        if dynamics_enabled is not None and (
            ch.lane, ch.is_cymbal, v) not in dynamics_enabled:
            continue
        if v == "ghost":
            lines.append(f"  {h.tick} = N {ghost_marker(ch.lane)} 0")
        elif v == "accent":
            lines.append(f"  {h.tick} = N {accent_marker(ch.lane)} 0")

    lines.append("}")
    return hand_warnings
