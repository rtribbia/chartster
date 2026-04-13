"""Songsterr drum code → Clone Hero lane mapping.

Songsterr's `fret` values on the drum track use GM MIDI note numbers (35-59)
plus Songsterr-specific extensions (91-98). Semantics for a few standard GM
notes differ from the published GM spec — notably GM 50, which Songsterr
labels HIGH_FLOOR_TOM (Green) rather than "High Tom" (Yellow).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional

# Clone Hero lanes
KICK = 0
RED = 1
YELLOW = 2
BLUE = 3
GREEN = 4

# Cymbal marker note numbers (CH convention)
CYMBAL_MARKER = {YELLOW: 66, BLUE: 67, GREEN: 68}


@dataclass(frozen=True)
class Lane:
    lane: int
    is_cymbal: bool

    @property
    def cymbal_marker(self) -> Optional[int]:
        return CYMBAL_MARKER.get(self.lane) if self.is_cymbal else None


# Songsterr fret value -> CH lane.
# Based on the Songsterr JS drum-id constants (HIGH_FLOOR_TOM=50,
# LOW_TOM=45, MID_TOM=47, FLOOR_TOM=41, VERY_LOW_TOM=43, etc).
SONGSTERR_TO_CH: Dict[int, Lane] = {
    # Kick
    35: Lane(KICK, False),    # Acoustic Bass Drum
    36: Lane(KICK, False),    # Bass Drum 1
    # Snare / Red
    37: Lane(RED, False),     # Side Stick
    38: Lane(RED, False),     # Acoustic Snare
    39: Lane(RED, False),     # Hand Clap
    40: Lane(RED, False),     # Electric Snare
    91: Lane(RED, False),     # Snare Rim Shot (Songsterr)
    # Hi-hat / Yellow cymbal
    42: Lane(YELLOW, True),   # Closed Hi-Hat
    44: Lane(YELLOW, True),   # Pedal Hi-Hat
    46: Lane(YELLOW, True),   # Open Hi-Hat
    92: Lane(YELLOW, True),   # Half Hi-Hat (Songsterr)
    94: Lane(YELLOW, True),   # Hi-Hat choke
    # High toms / Yellow
    48: Lane(YELLOW, False),  # HIGH_TOM (Hi-Mid Tom)
    # Mid toms / Blue
    45: Lane(BLUE, False),    # LOW_TOM
    47: Lane(BLUE, False),    # MID_TOM / LOW_MID_TOM
    # Ride / Blue cymbal
    51: Lane(BLUE, True),     # Ride Cymbal 1
    53: Lane(BLUE, True),     # Ride Bell
    59: Lane(BLUE, True),     # Ride Cymbal 2
    93: Lane(BLUE, True),     # Ride Edge (Songsterr)
    96: Lane(BLUE, True),     # Ride choke
    # Floor toms / Green
    41: Lane(GREEN, False),   # FLOOR_TOM (GM Low Floor Tom)
    43: Lane(GREEN, False),   # VERY_LOW_TOM (GM High Floor Tom)
    50: Lane(GREEN, False),   # HIGH_FLOOR_TOM (Songsterr semantics)
    # Crash / Green cymbal
    49: Lane(GREEN, True),    # Crash Cymbal 1
    52: Lane(GREEN, True),    # Chinese Cymbal
    55: Lane(GREEN, True),    # Splash Cymbal
    57: Lane(GREEN, True),    # Crash Cymbal 2
    95: Lane(GREEN, True),    # Crash choke
    97: Lane(GREEN, True),    # Chinese choke
    98: Lane(GREEN, True),    # Splash choke
    # Auxiliary
    54: Lane(YELLOW, True),   # Tambourine
    56: Lane(BLUE, True),     # Cowbell
    58: Lane(GREEN, False),   # Vibraslap
    76: Lane(BLUE, True),     # High Wood Block
    77: Lane(BLUE, True),     # Low Wood Block
}


DRUM_NAMES: Dict[int, str] = {
    35: "Kick (acoustic)", 36: "Kick",
    37: "Side stick", 38: "Snare", 39: "Hand clap", 40: "Electric snare",
    91: "Snare rim shot",
    42: "Closed hi-hat", 44: "Pedal hi-hat", 46: "Open hi-hat",
    92: "Half hi-hat", 94: "Hi-hat choke",
    48: "High tom",
    45: "Low tom", 47: "Mid tom",
    51: "Ride", 53: "Ride bell", 59: "Ride 2", 93: "Ride edge", 96: "Ride choke",
    41: "Floor tom", 43: "Very low tom", 50: "High floor tom",
    49: "Crash 1", 52: "Chinese cymbal", 55: "Splash", 57: "Crash 2",
    95: "Crash choke", 97: "Chinese choke", 98: "Splash choke",
    54: "Tambourine", 56: "Cowbell", 58: "Vibraslap",
    76: "High wood block", 77: "Low wood block",
}

# CH lane options users can pick in the mapping UI.
LANE_OPTIONS: list = [
    ("Kick", Lane(KICK, False)),
    ("Red (snare)", Lane(RED, False)),
    ("Yellow tom", Lane(YELLOW, False)),
    ("Yellow cymbal", Lane(YELLOW, True)),
    ("Blue tom", Lane(BLUE, False)),
    ("Blue cymbal", Lane(BLUE, True)),
    ("Green tom", Lane(GREEN, False)),
    ("Green cymbal", Lane(GREEN, True)),
    ("— Remove —", None),
]


def drum_name(fret: int) -> str:
    return DRUM_NAMES.get(fret, f"Fret {fret}")


# Songsterr velocity tokens -> MIDI-style 1-127
VELOCITY_MAP: Dict[str, int] = {
    "ppp": 16, "pp": 33, "p": 49, "mp": 64,
    "mf": 80, "f": 96, "ff": 112, "fff": 127,
}
DEFAULT_VELOCITY = 96


def classify_velocity(velocity: int) -> str:
    """ghost / normal / accent — matches CH dynamic marker thresholds."""
    if velocity <= 50:
        return "ghost"
    if velocity >= 111:
        return "accent"
    return "normal"


def ghost_marker(lane: int) -> int:
    """CH chart note for ghost marker on a given lane."""
    return 39 + lane


def accent_marker(lane: int) -> int:
    """CH chart note for accent marker on a given lane."""
    return 33 + lane
