"""Fetch Songsterr video-point alignments and derive per-measure tempos.

Songsterr publishes per-song, per-video sync tables at a public endpoint. Each
record is an array of per-measure start times (seconds) aligned to a specific
YouTube video. Using these instead of the tab's declared tempos produces charts
that stay locked to the audio for the whole song.
"""

from __future__ import annotations

import json
import urllib.request
from fractions import Fraction
from typing import List, Optional, Tuple

from .songsterr import Song

API_URL = "https://www.songsterr.com/api/video-points/{song_id}/{revision_id}/list"


def fetch(song_id: int, revision_id: int, timeout: float = 10.0) -> list:
    url = API_URL.format(song_id=song_id, revision_id=revision_id)
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def select(
    records: list,
    video_id: Optional[str] = None,
    feature: Optional[str] = None,
) -> dict:
    """Pick one alignment. Exact video_id wins; else feature match; else first."""
    done = [r for r in records if r.get("status") == "done" and r.get("points")]
    if not done:
        raise ValueError("No usable video-point alignments (status=done with points)")
    if video_id:
        matches = [r for r in done if r.get("videoId") == video_id]
        if not matches:
            raise ValueError(f"No alignment for videoId={video_id}")
        # When a videoId has multiple alignments, prefer Songsterr's primary
        # (feature=null — the site's default audio source).
        for r in matches:
            if r.get("feature") is None:
                return r
        return matches[0]
    if feature:
        for r in done:
            if r.get("feature") == feature:
                return r
        raise ValueError(f"No alignment for feature={feature}")
    # No filters: prefer the site-default (feature=null), else first.
    for r in done:
        if r.get("feature") is None:
            return r
    return done[0]


def derive_tempos(
    song: Song,
    points: List[float],
    ticks_per_beat: int,
) -> Tuple[List[Tuple[int, float]], float]:
    """Per-measure start seconds → (tempos, delay_seconds).

    tempos: one (tick, bpm) per measure. bpm derived so that the measure's
    signature-implied duration equals the elapsed seconds between points.
    delay: seconds of audio before tab tick 0 (write to song.ini as delay_ms).

    Pads short point arrays by repeating the last delta, matching Songsterr's
    client behavior (common.js:3591-3595).
    """
    n = len(song.measures)
    pts = list(points)
    if len(pts) < 2:
        raise ValueError("Need at least 2 points to derive tempos")
    delta = pts[-1] - pts[-2]
    while len(pts) < n + 1:
        pts.append(pts[-1] + delta)

    tempos: List[Tuple[int, float]] = []
    tick = 0
    for i, m in enumerate(song.measures):
        num, den = m.signature
        dur_s = pts[i + 1] - pts[i]
        if dur_s > 0:
            # measure_s = (60/bpm) * 4 * num/den  ⇒  bpm = 240*num / (den*dur_s)
            bpm = 240.0 * num / (den * dur_s)
            tempos.append((tick, bpm))
        tick += int(Fraction(num, den) * 4 * ticks_per_beat)

    return tempos, pts[0]
