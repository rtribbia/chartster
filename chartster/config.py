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
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    cp = configparser.ConfigParser()
    cp.read(CONFIG_PATH, encoding="utf-8") if CONFIG_PATH.exists() else None
    if SECTION not in cp:
        cp[SECTION] = {}
    for k, v in values.items():
        if v:
            cp[SECTION][k] = str(v)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        cp.write(f)
