"""chartster CLI — one command, full CH song folder."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

from .chart import TICKS_PER_BEAT, estimate_duration, render
from .songsterr import parse, parse_dict
from . import fetch as sfetch
from . import video_points as vp


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="chartster",
        description="Convert Songsterr JSON drum tabs to Clone Hero expert drum charts.",
    )
    parser.add_argument("tab", help="Songsterr JSON file (notes.json) OR a Songsterr song URL")
    parser.add_argument(
        "--revision", type=int,
        help="With a URL: use this revisionId (default: latest).",
    )
    parser.add_argument(
        "--track", type=int,
        help="With a URL: use this partId (skip interactive picker).",
    )
    parser.add_argument(
        "--list-tracks", action="store_true",
        help="With a URL: list available tracks and exit.",
    )
    parser.add_argument(
        "-o", "--output-dir", required=False,
        help="Output directory for the CH song folder (will be created)",
    )
    parser.add_argument("--mp3", help="Audio file to copy in as song.mp3")
    parser.add_argument("--name", default="", help="Song name")
    parser.add_argument("--artist", default="", help="Artist")
    parser.add_argument("--album", default="", help="Album")
    parser.add_argument("--year", default="", help="Year")
    parser.add_argument("--genre", default="", help="Genre")
    parser.add_argument("--charter", default="Chartster", help="Charter name")
    parser.add_argument(
        "--bpm-scale", type=float, default=1.0,
        help="Multiply all tempos (e.g. 0.95 to slow by 5%%). Ignored if --fit-audio is set.",
    )
    parser.add_argument(
        "--fit-audio", action="store_true",
        help="Auto-compute bpm_scale so the chart's total duration matches the mp3 "
             "(requires --mp3 and ffprobe on PATH). Respects --delay / --trailing-silence.",
    )
    parser.add_argument(
        "--delay", type=float, default=0.0,
        help="Seconds of audio before the tab's tick 0 (e.g. silent intro). "
             "Written to song.ini as delay=<ms>.",
    )
    parser.add_argument(
        "--trailing-silence", type=float, default=0.0,
        help="Seconds of audio after the tab ends (e.g. outro silence). "
             "Only affects --fit-audio computation.",
    )
    parser.add_argument(
        "--two-tom-kit", action="store_true",
        help="Interpret LOW_TOM (fret 45) as floor tom (Green) instead of mid tom (Blue)",
    )
    parser.add_argument(
        "--video-points", action="store_true",
        help="Fetch Songsterr's per-measure video-sync table and derive the tempo "
             "map from it (audio-accurate). Uses songId/revisionId from notes.json. "
             "Overrides declared tempos entirely; --bpm-scale / --fit-audio are ignored.",
    )
    parser.add_argument(
        "--video-id",
        help="With --video-points: prefer the alignment for this YouTube videoId.",
    )
    parser.add_argument(
        "--feature",
        help="With --video-points: prefer alignment with this feature tag "
             "(e.g. 'alternative', 'backing').",
    )
    parser.add_argument(
        "--list-alignments", action="store_true",
        help="Fetch and print all available video-point alignments for this tab, "
             "then exit without writing a chart.",
    )

    args = parser.parse_args()

    if args.tab.startswith("http://") or args.tab.startswith("https://"):
        song = _song_from_url_interactive(args)
    else:
        try:
            song = parse(args.tab)
        except FileNotFoundError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)

    if args.list_alignments:
        _list_alignments(song)
        return

    if not args.output_dir:
        print("Error: -o/--output-dir is required", file=sys.stderr)
        sys.exit(1)

    tempos_override = None
    chosen_video_id = args.video_id
    if args.video_points:
        if not song.song_id or not song.revision_id:
            print("Error: notes.json lacks songId/revisionId; cannot fetch "
                  "video-points.", file=sys.stderr)
            sys.exit(1)
        records = vp.fetch(song.song_id, song.revision_id)
        record = vp.select(records, video_id=args.video_id, feature=args.feature)
        chosen_video_id = record.get("videoId") or chosen_video_id
        points = record["points"]
        tempos_override, vp_delay = vp.derive_tempos(song, points, TICKS_PER_BEAT)
        print(f"--video-points: using alignment id={record['id']} "
              f"videoId={record['videoId']} feature={record.get('feature')} "
              f"({len(points)} points)")
        if args.delay == 0.0 and vp_delay > 0:
            args.delay = vp_delay
            print(f"  auto-delay: {vp_delay:.3f}s (points[0])")

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    chart_path = out_dir / "notes.chart"

    bpm_scale = args.bpm_scale
    if args.fit_audio:
        if not args.mp3:
            print("Error: --fit-audio requires --mp3", file=sys.stderr)
            sys.exit(1)
        audio_sec = _audio_duration(args.mp3)
        effective_audio = audio_sec - args.delay - args.trailing_silence
        chart_sec = estimate_duration(song)
        if chart_sec <= 0 or effective_audio <= 0:
            print("Error: could not compute durations for fit-audio", file=sys.stderr)
            sys.exit(1)
        bpm_scale = chart_sec / effective_audio
        print(f"--fit-audio: chart={chart_sec:.2f}s, "
              f"audio={audio_sec:.2f}s (effective {effective_audio:.2f}s "
              f"after delay={args.delay}s + trailing={args.trailing_silence}s), "
              f"bpm_scale={bpm_scale:.4f}")

    name = args.name or song.name
    summary = render(
        song,
        str(chart_path),
        name=name,
        artist=args.artist,
        charter=args.charter,
        bpm_scale=bpm_scale,
        two_tom_kit=args.two_tom_kit,
        tempos_override=tempos_override,
    )

    if args.mp3:
        mp3_src = Path(args.mp3)
        if not mp3_src.exists():
            print(f"Warning: --mp3 file not found: {mp3_src}", file=sys.stderr)
        else:
            mp3_dst = out_dir / "song.mp3"
            if mp3_src.resolve() != mp3_dst.resolve():
                shutil.copy(mp3_src, mp3_dst)

    _write_song_ini(out_dir / "song.ini", name, args.artist, args.charter,
                    args.album, args.year, args.genre,
                    delay_ms=int(round(args.delay * 1000)))

    source_url = args.tab if args.tab.startswith(("http://", "https://")) else ""
    from .mapping import SONGSTERR_TO_CH
    _write_readme(out_dir / "README.txt", source_url,
                  song.song_id, song.revision_id, chosen_video_id,
                  SONGSTERR_TO_CH)

    print(f"Wrote {summary['hits']} notes to {chart_path}")
    print(f"  Tempo changes: {summary['tempo_changes']}")
    print(f"  Time signature changes: {summary['time_sig_changes']}")
    if summary["hand_warnings"]:
        print(f"  Warning: {summary['hand_warnings']} tick(s) have >2 simultaneous hand notes")


def _ytdlp_download(video_id: str, out_dir: Path) -> Optional[str]:
    """Download audio from YouTube via yt-dlp as song.mp3. Returns path or None."""
    dst = out_dir / "song.mp3"
    url = f"https://www.youtube.com/watch?v={video_id}"
    try:
        subprocess.run(
            ["yt-dlp", "-x", "--audio-format", "mp3", "--audio-quality", "0",
             "--no-playlist", "-o", str(dst.with_suffix(".%(ext)s")), url],
            check=True,
        )
    except FileNotFoundError:
        print("Error: yt-dlp not found on PATH. Install with `pip install yt-dlp` "
              "or `brew install yt-dlp`.", file=sys.stderr)
        return None
    except subprocess.CalledProcessError as e:
        print(f"Error: yt-dlp failed ({e}).", file=sys.stderr)
        return None
    return str(dst) if dst.exists() else None


def _audio_duration(path: str) -> float:
    """Return mp3 duration in seconds via ffprobe."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, check=True,
        )
        return float(r.stdout.strip())
    except (FileNotFoundError, subprocess.CalledProcessError, ValueError) as e:
        print(f"Error: ffprobe failed ({e}). Install ffmpeg to use --fit-audio.",
              file=sys.stderr)
        sys.exit(1)


def _write_song_ini(
    path: Path, name: str, artist: str, charter: str,
    album: str, year: str, genre: str, delay_ms: int = 0,
) -> None:
    lines = ["[song]", f"name = {name}", f"artist = {artist}", f"charter = {charter}"]
    if album:
        lines.append(f"album = {album}")
    if genre:
        lines.append(f"genre = {genre}")
    if year:
        lines.append(f"year = {year}")
    lines.append("diff_drums = -1")
    lines.append("preview_start_time = 0")
    if delay_ms:
        lines.append(f"delay = {delay_ms}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_readme(path: Path, url: str, song_id, revision_id, video_id,
                  mapping) -> None:
    from .mapping import LANE_OPTIONS, drum_name
    def label(lane):
        if lane is None:
            return "(removed)"
        for n, o in LANE_OPTIONS:
            if o is not None and o.lane == lane.lane and o.is_cymbal == lane.is_cymbal:
                return n
        return f"lane {lane.lane}"
    yt = f"https://www.youtube.com/watch?v={video_id}" if video_id else "(none)"
    lines = [
        "Generated by Chartster",
        "",
        f"Songsterr URL:  {url or '(none)'}",
        f"songId:         {song_id if song_id is not None else '(unknown)'}",
        f"revisionId:     {revision_id if revision_id is not None else '(unknown)'}",
        f"YouTube (sync): {yt}",
        "",
        "Mappings:",
    ]
    for fret in sorted(mapping):
        lines.append(f"  {drum_name(fret)} (id: {fret}) -> {label(mapping[fret])}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _prompt(msg: str, default: Optional[str] = None) -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        val = input(f"{msg}{suffix}: ").strip()
        if val:
            return val
        if default is not None:
            return default


def _prompt_yesno(msg: str, default: bool = True) -> bool:
    d = "Y/n" if default else "y/N"
    val = input(f"{msg} [{d}]: ").strip().lower()
    if not val:
        return default
    return val.startswith("y")


def _song_from_url_interactive(args):
    song_id = sfetch.parse_song_url(args.tab)

    revision_id = args.revision
    if revision_id is None:
        revisions = sfetch.fetch_revisions(song_id)
        revision_id, meta = sfetch.latest_published_revision(song_id, revisions)
    else:
        meta = sfetch.fetch_meta(song_id, revision_id)
    print(f"songId={song_id} revisionId={revision_id}")
    current = meta.get("current") or {}
    image = meta.get("image") or current.get("image")
    tracks = meta.get("tracks") or current.get("tracks") or []
    popular = (sfetch.popular_drum_part_id(meta)
               or sfetch.popular_drum_part_id(current))
    meta_title = meta.get("title") or current.get("title") or ""
    meta_artist = meta.get("artist") or current.get("artist") or ""

    # Songsterr's meta.tracks don't carry explicit partIds — the CDN uses the
    # tracks-array index as the partId (0.json, 1.json, ...).
    for i, t in enumerate(tracks):
        t.setdefault("partId", i)
    if popular is None:
        for i, t in enumerate(tracks):
            if (t.get("instrument") or "").lower().startswith("drum"):
                popular = i
                break

    if args.list_tracks:
        _print_tracks(tracks, popular)
        sys.exit(0)

    part_id = args.track
    if part_id is None:
        labels = [_track_label(t, popular) for t in tracks]
        default_idx = popular if popular is not None else 0
        idx = _pick("Select track", labels, default_idx)
        part_id = tracks[idx].get("partId", idx)

    notes = sfetch.fetch_notes(song_id, revision_id, image, part_id)
    song = parse_dict(notes)
    if song.song_id is None:
        song.song_id = song_id
    if song.revision_id is None:
        song.revision_id = revision_id

    # Alignment selection — skip if user already passed --video-id/--feature
    # or explicitly opted in via --video-points from the CLI.
    if not args.video_points and args.video_id is None and args.feature is None:
        try:
            records = vp.fetch(song_id, revision_id)
            done = [r for r in records if r.get("status") == "done" and r.get("points")]
        except Exception as e:
            done = []
            print(f"(no alignments available: {e})")
        if done and _prompt_yesno("\nUse a video-points alignment for perfect sync?", True):
            # Songsterr's client defaults to the entry with feature=null
            # (the "primary" audio source; others are alternative/backing/solo).
            default_idx = next((i for i, r in enumerate(done)
                                if r.get("feature") is None), 0)
            labels = []
            for i, r in enumerate(done):
                pts = r.get("points") or []
                span = f"{pts[0]:.1f}..{pts[-1]:.1f}s" if len(pts) >= 2 else "—"
                vid = r.get("videoId") or ""
                feat = r.get("feature") or "primary"
                marker = "  (site default)" if i == default_idx else ""
                labels.append(f"videoId={vid:<14} feature={feat:<12} "
                              f"points={len(pts):<3} span={span:<16} "
                              f"https://youtu.be/{vid}{marker}")
            idx = _pick("Select alignment", labels, default_idx)
            args.video_points = True
            args.video_id = done[idx].get("videoId")

    if not args.output_dir:
        default_name = meta_title or song.name or f"song-{song_id}"
        default_dir = f"./{default_name}"
        args.output_dir = _prompt("Output directory", default_dir)

    if args.mp3 is None:
        if args.video_id and _prompt_yesno(
                f"\nDownload audio from youtu.be/{args.video_id} via yt-dlp?", True):
            out_dir = Path(args.output_dir)
            out_dir.mkdir(parents=True, exist_ok=True)
            args.mp3 = _ytdlp_download(args.video_id, out_dir)
        else:
            val = input("\nPath to song.mp3 (optional, enter to skip): ").strip()
            if val:
                args.mp3 = val

    if not args.name:
        args.name = _prompt("Song name", meta_title or song.name or "")
    if not args.artist:
        args.artist = _prompt("Artist", meta_artist)

    return song


def _track_label(t, popular) -> str:
    pid = t.get("partId")
    inst = t.get("instrument") or ""
    tname = t.get("name") or t.get("title") or ""
    marker = "  * popular drum" if pid == popular else ""
    return f"[{pid}] {inst:<22} {tname}{marker}"


def _pick(prompt_msg: str, options: list, default_idx: int = 0) -> int:
    """Arrow-key interactive picker. Falls back to numeric input if not a TTY."""
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print(f"\n{prompt_msg}:")
        for i, o in enumerate(options):
            print(f"  [{i}] {o}")
        while True:
            val = input(f"Enter index [{default_idx}]: ").strip() or str(default_idx)
            try:
                i = int(val)
                if 0 <= i < len(options):
                    return i
            except ValueError:
                pass
            print("  invalid")

    import termios
    import tty

    idx = default_idx if 0 <= default_idx < len(options) else 0
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)

    def render(first=False):
        if not first:
            sys.stdout.write(f"\x1b[{len(options) + 1}A")
        sys.stdout.write(f"{prompt_msg} (↑/↓ to move, Enter to select):\n")
        for i, o in enumerate(options):
            prefix = "\x1b[7m> " if i == idx else "  "
            reset = "\x1b[0m" if i == idx else ""
            sys.stdout.write(f"\x1b[2K{prefix}{o}{reset}\n")
        sys.stdout.flush()

    try:
        tty.setcbreak(fd)
        print()  # spacer above menu
        render(first=True)
        while True:
            ch = sys.stdin.read(1)
            if ch == "\x1b":
                seq = sys.stdin.read(2)
                if seq == "[A" and idx > 0:
                    idx -= 1
                elif seq == "[B" and idx < len(options) - 1:
                    idx += 1
            elif ch in ("\r", "\n"):
                break
            elif ch == "\x03":
                raise KeyboardInterrupt
            elif ch == "k" and idx > 0:
                idx -= 1
            elif ch == "j" and idx < len(options) - 1:
                idx += 1
            render()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
    return idx


def _print_tracks(tracks, popular):
    for t in tracks:
        pid = t.get("partId")
        inst = t.get("instrument") or ""
        tname = t.get("name") or t.get("title") or ""
        marker = "  * popular drum" if pid == popular else ""
        print(f"  partId={str(pid):<3} instrument={inst:<12} {tname}{marker}")


def _list_alignments(song) -> None:
    if not song.song_id or not song.revision_id:
        print("Error: notes.json lacks songId/revisionId.", file=sys.stderr)
        sys.exit(1)
    records = vp.fetch(song.song_id, song.revision_id)
    print(f"Alignments for songId={song.song_id} revisionId={song.revision_id}:")
    for r in records:
        pts = r.get("points") or []
        span = f"{pts[0]:.2f}..{pts[-1]:.2f}s" if len(pts) >= 2 else "—"
        vid = r.get("videoId") or ""
        url = f"https://youtu.be/{vid}" if vid else ""
        print(f"  videoId={vid:<14} feature={str(r.get('feature')):<12} "
              f"points={len(pts):<3} span={span:<18} {url}")
    print("\nUse --video-id <id> to select (e.g. --video-id huR__xAcUQs).")


if __name__ == "__main__":
    main()
