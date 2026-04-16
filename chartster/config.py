"""Local config for remembering user-picked paths between runs.

Stored next to the executable when frozen (same folder as Chartster.app /
Chartster.exe), else in the current working directory.
"""
from __future__ import annotations

import configparser
import sys
from pathlib import Path


def _config_dir() -> Path:
    if getattr(sys, "frozen", False):
        exe = Path(sys.executable).resolve()
        # On macOS the exe lives inside Chartster.app/Contents/MacOS/ — walk up
        # to the folder containing the .app bundle so config sits next to it.
        for parent in exe.parents:
            if parent.suffix == ".app":
                return parent.parent
        return exe.parent
    return Path.cwd()


CONFIG_PATH = _config_dir() / "chartster-config.ini"
SECTION = "paths"
MAPPINGS_SECTION = "mappings"


def load() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    cp = configparser.ConfigParser()
    try:
        cp.read(CONFIG_PATH, encoding="utf-8")
        if SECTION in cp:
            return dict(cp[SECTION])
    except Exception:
        pass
    return {}


def save(values: dict) -> None:
    _write_section(SECTION, {k: str(v) for k, v in values.items() if v})


def load_mappings() -> dict[int, str]:
    """fret -> LANE_OPTIONS label."""
    if not CONFIG_PATH.exists():
        return {}
    cp = configparser.ConfigParser()
    try:
        cp.read(CONFIG_PATH, encoding="utf-8")
        if MAPPINGS_SECTION not in cp:
            return {}
        out = {}
        for k, v in cp[MAPPINGS_SECTION].items():
            try:
                out[int(k)] = v
            except ValueError:
                continue
        return out
    except Exception:
        return {}


def bootstrap_if_missing() -> None:
    """On first run, seed the config with every known drum ID and its default
    lane label. Never overwrites an existing file — the user owns it from then
    on.
    """
    if CONFIG_PATH.exists():
        return
    from .mapping import DRUM_NAMES, LANE_OPTIONS, SONGSTERR_TO_CH
    label_for_lane = {}
    for name, lane in LANE_OPTIONS:
        if lane is not None:
            label_for_lane[(lane.lane, lane.is_cymbal)] = name
    defaults = {}
    # Include every known drum ID — frets without a kit default (metronome,
    # whistles, scratches) get "— Remove —".
    for fret in sorted(set(DRUM_NAMES) | set(SONGSTERR_TO_CH)):
        lane = SONGSTERR_TO_CH.get(fret)
        if lane is None:
            defaults[str(fret)] = "— Remove —"
        else:
            defaults[str(fret)] = label_for_lane.get(
                (lane.lane, lane.is_cymbal), "— Remove —")
    _write_section(MAPPINGS_SECTION, defaults, replace=True)


def _write_section(section: str, values: dict, replace: bool = False) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    cp = configparser.ConfigParser()
    if CONFIG_PATH.exists():
        cp.read(CONFIG_PATH, encoding="utf-8")
    if replace or section not in cp:
        cp[section] = {}
    for k, v in values.items():
        cp[section][k] = v
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        f.write(_header())
        cp.write(f)


def _header() -> str:
    # Local import to avoid circular imports at module load time.
    from .mapping import DRUM_NAMES
    lines = [
        "# Chartster config — edit with care.",
        "#",
        "# [paths] — remembered executable + export-folder locations.",
        "# [mappings] — per-drum-id lane overrides. Values must match one of:",
        "#   Kick, Red (snare), Yellow tom, Yellow cymbal, Blue tom,",
        "#   Blue cymbal, Green tom, Green cymbal, — Remove —",
        "#",
        "# Drum ID reference:",
    ]
    for fid in sorted(DRUM_NAMES):
        lines.append(f"#   {fid:>3} = {DRUM_NAMES[fid]}")
    lines.append("")
    lines.append("")
    return "\n".join(lines)
