"""Fetch Songsterr tab data starting from a song URL.

Public, unauthenticated endpoints:
  GET /api/meta/{songId}/revisions           — list of revisions
  GET /api/meta/{songId}/{revisionId}        — full meta (has `image`, `tracks`,
                                               `popularTrackDrum`)
  CDN /{songId}/{revisionId}/{image}/{partId}.json  — notes.json (gzipped)
"""

from __future__ import annotations

import gzip
import json
import re
import urllib.request
from typing import Optional

META_REVISIONS_URL = "https://www.songsterr.com/api/meta/{song_id}/revisions"
META_URL = "https://www.songsterr.com/api/meta/{song_id}/{revision_id}"

# Mirrors Songsterr's own frontend routing (vendor bundle, function Xe):
# stage images come off a dedicated CDN; published images rotate through a
# pool on retry. No-image revisions fall back to the legacy /part/ endpoint.
_STAGE_HOST = "d3d3l6a6rcgkaf"
_PUBLISHED_HOSTS = ("dqsljvtekg760", "d34shlm8p2ums2", "d3cqchs6g3b5ew")
_PART_HOSTS = ("d3rrfvx08uyjp1", "dodkcbujl0ebx", "dj1usja78sinh")

_SONG_ID_RE = re.compile(r"-s(\d+)(?:\?|$|#|/)")


def parse_song_url(url: str) -> int:
    """Extract songId from a Songsterr URL like
    https://www.songsterr.com/a/wsa/beatles-yer-blues-drum-tab-s26437"""
    m = _SONG_ID_RE.search(url + "$") or re.search(r"-s(\d+)", url)
    if not m:
        raise ValueError(f"Could not parse songId from URL: {url}")
    return int(m.group(1))


def _get_json(url: str, timeout: float = 10.0) -> dict | list:
    req = urllib.request.Request(url, headers={"Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            body = gzip.decompress(body)
        elif body[:2] == b"\x1f\x8b":
            body = gzip.decompress(body)
        return json.loads(body.decode("utf-8"))


def fetch_revisions(song_id: int) -> list:
    return _get_json(META_REVISIONS_URL.format(song_id=song_id))


def fetch_meta(song_id: int, revision_id: int) -> dict:
    return _get_json(META_URL.format(song_id=song_id, revision_id=revision_id))


def fetch_notes(song_id: int, revision_id: int, image: str, part_id: int) -> dict:
    """Fetch notes JSON, routing to the CDN pool Songsterr's frontend uses.

    Stage images live on a single CDN; published images rotate through a
    three-host pool. If no image is given we hit the legacy /part/ endpoint.
    Each pool is tried in order until one responds.
    """
    errors: list[Exception] = []
    if image and image.endswith("-stage"):
        hosts = (_STAGE_HOST,)
        path = f"{song_id}/{revision_id}/{image}/{part_id}.json"
    elif image:
        hosts = _PUBLISHED_HOSTS
        path = f"{song_id}/{revision_id}/{image}/{part_id}.json"
    else:
        hosts = _PART_HOSTS
        path = f"part/{revision_id}/{part_id}"
    for host in hosts:
        try:
            return _get_json(f"https://{host}.cloudfront.net/{path}")
        except Exception as e:
            errors.append(e)
    raise errors[-1]


def sorted_revision_ids(revisions: list) -> list:
    """Revision ids from newest to oldest."""
    ids = [int(r["revisionId"]) for r in revisions if "revisionId" in r]
    return sorted(ids, reverse=True)


def latest_revision_id(revisions: list) -> int:
    """Pick the most recent revision (highest id)."""
    ids = sorted_revision_ids(revisions)
    if not ids:
        raise ValueError("No revisions returned")
    return ids[0]


def latest_published_revision(song_id: int, revisions: list) -> tuple[int, dict]:
    """Return (revisionId, meta) for the newest revision with a usable image.

    Staged images (`*-stage`) are served publicly from a dedicated CDN, so we
    treat them as first-class — matches the site's own behavior.
    """
    ids = sorted_revision_ids(revisions)
    last_err: Exception | None = None
    for rid in ids:
        try:
            meta = fetch_meta(song_id, rid)
        except Exception as e:
            last_err = e
            continue
        image = meta.get("image") or (meta.get("current") or {}).get("image") or ""
        if image:
            return rid, meta
    if last_err:
        raise last_err
    raise ValueError("No published revision found")


def popular_drum_part_id(meta: dict) -> Optional[int]:
    v = meta.get("popularTrackDrum")
    if v is None:
        return None
    if isinstance(v, dict):
        return v.get("partId")
    try:
        return int(v)
    except (TypeError, ValueError):
        return None
