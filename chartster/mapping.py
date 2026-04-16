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


# Songsterr fret value -> CH lane. Based on the Songsterr JS drum-id
# constants in vendor.js (HIGH_Q=27, BASS_DRUM=36, HIGH_FLOOR_TOM=50, etc).
# Percussion with no natural kit fit (metronome, scratches, whistles) is
# intentionally omitted so the mapping UI defaults them to "— Remove —".
SONGSTERR_TO_CH: Dict[int, Lane] = {
    # Kick
    35: Lane(KICK, False),    # ACOUSTIC_BASS_DRUM
    36: Lane(KICK, False),    # BASS_DRUM
    # Snare / Red
    37: Lane(RED, False),     # SIDE_STICK
    38: Lane(RED, False),     # SNARE / ACOUSTIC_SNARE
    39: Lane(RED, False),     # HAND_CLAP
    40: Lane(RED, False),     # ELECTRIC_SNARE
    91: Lane(RED, False),     # SNARE_RIM_SHOT
    # Hi-hat / Yellow cymbal
    42: Lane(YELLOW, True),   # CLOSED_HI_HAT
    44: Lane(YELLOW, True),   # FOOT_HI_HAT (pedal)
    46: Lane(YELLOW, True),   # OPEN_HI_HAT
    92: Lane(YELLOW, True),   # HALF_HI_HAT
    # High toms / Yellow
    48: Lane(YELLOW, False),  # HIGH_TOM
    # Mid toms / Blue
    45: Lane(BLUE, False),    # LOW_TOM
    47: Lane(BLUE, False),    # MID_TOM / LOW_MID_TOM
    # Ride / Blue cymbal
    51: Lane(BLUE, True),     # RIDE_CYMBAL / RIDE_CYMBAL_1
    53: Lane(BLUE, True),     # RIDE_BELL
    59: Lane(BLUE, True),     # RIDE_CYMBAL_2
    93: Lane(BLUE, True),     # RIDE_EDGE
    94: Lane(BLUE, True),     # RIDE_CYMBAL_CHOKE
    # Floor toms / Green
    41: Lane(GREEN, False),   # FLOOR_TOM
    43: Lane(GREEN, False),   # VERY_LOW_TOM
    50: Lane(GREEN, False),   # HIGH_FLOOR_TOM (Songsterr semantics)
    # Crash / Green cymbal
    49: Lane(GREEN, True),    # CRASH_CYMBAL / CRASH_CYMBAL_1
    52: Lane(GREEN, True),    # CHINESE_CYMBAL
    55: Lane(GREEN, True),    # SPLASH_CYMBAL
    57: Lane(GREEN, True),    # CRASH_CYMBAL_2
    95: Lane(GREEN, True),    # SPLASH_CYMBAL_CHOKE
    96: Lane(GREEN, True),    # CHINESE_CYMBAL_CHOKE
    97: Lane(GREEN, True),    # CRASH_CYMBAL_CHOKE
    98: Lane(GREEN, True),    # CRASH_CYMBAL_2_CHOKE
    # Hand percussion — tambourine / wood / rattles
    54: Lane(YELLOW, True),   # TAMBOURINE
    56: Lane(BLUE, True),     # COWBELL
    58: Lane(GREEN, False),   # VIBRASLAP
    76: Lane(BLUE, True),     # HI_WOOD_BLOCK
    77: Lane(BLUE, True),     # LOW_WOOD_BLOCK
    # Latin toms (bongos, congas, timbales, cuica, surdo)
    60: Lane(YELLOW, False),  # HI_BONGO
    61: Lane(BLUE, False),    # LOW_BONGO
    62: Lane(YELLOW, False),  # MUTE_HI_CONGA
    63: Lane(YELLOW, False),  # OPEN_HI_CONGA
    64: Lane(BLUE, False),    # LOW_CONGA
    65: Lane(YELLOW, False),  # HIGH_TIMBALE
    66: Lane(BLUE, False),    # LOW_TIMBALE
    78: Lane(YELLOW, False),  # MUTE_CUICA
    79: Lane(BLUE, False),    # OPEN_CUICA
    86: Lane(BLUE, False),    # MUTE_SURDO
    87: Lane(BLUE, False),    # OPEN_SURDO
    # Bells / agogo / cowbells
    67: Lane(BLUE, True),     # HIGH_AGOGO
    68: Lane(BLUE, True),     # LOW_AGOGO
    83: Lane(BLUE, True),     # JINGLE_BELL
    84: Lane(BLUE, True),     # BELL_TREE
    99: Lane(BLUE, True),     # LOW_COWBELL
    102: Lane(BLUE, True),    # HIGH_COWBELL
    # Shakers / clicky wood / triangles
    69: Lane(YELLOW, True),   # CABASA
    70: Lane(YELLOW, True),   # MARACAS
    73: Lane(YELLOW, True),   # SHORT_GUIRO
    74: Lane(YELLOW, True),   # LONG_GUIRO
    75: Lane(YELLOW, True),   # CLAVES
    80: Lane(YELLOW, True),   # MUTE_TRIANGLE
    81: Lane(YELLOW, True),   # OPEN_TRIANGLE
    82: Lane(YELLOW, True),   # SHAKER
    85: Lane(YELLOW, True),   # CASTINETS
}


DRUM_NAMES: Dict[int, str] = {
    # Non-kit / click (default Remove — not in SONGSTERR_TO_CH)
    27: "High Q", 28: "Slap",
    29: "Scratch push", 30: "Scratch pull",
    31: "Sticks", 32: "Square click",
    33: "Metronome click", 34: "Metronome bell",
    71: "Short whistle", 72: "Long whistle",
    # Kick
    35: "Kick (acoustic)", 36: "Kick",
    # Snare / Red
    37: "Side stick", 38: "Snare", 39: "Hand clap", 40: "Electric snare",
    91: "Snare rim shot",
    # Hi-hat
    42: "Closed hi-hat", 44: "Pedal hi-hat", 46: "Open hi-hat",
    92: "Half hi-hat",
    # Toms
    48: "High tom",
    45: "Low tom", 47: "Mid tom",
    41: "Floor tom", 43: "Very low tom", 50: "High floor tom",
    # Ride
    51: "Ride", 53: "Ride bell", 59: "Ride 2",
    93: "Ride edge", 94: "Ride choke",
    # Crash / splash / chinese
    49: "Crash 1", 52: "Chinese cymbal", 55: "Splash", 57: "Crash 2",
    95: "Splash choke", 96: "Chinese choke",
    97: "Crash choke", 98: "Crash 2 choke",
    # Auxiliary / hand percussion
    54: "Tambourine", 56: "Cowbell", 58: "Vibraslap",
    76: "High wood block", 77: "Low wood block",
    60: "High bongo", 61: "Low bongo",
    62: "Mute hi conga", 63: "Open hi conga", 64: "Low conga",
    65: "High timbale", 66: "Low timbale",
    67: "High agogo", 68: "Low agogo",
    69: "Cabasa", 70: "Maracas",
    73: "Short guiro", 74: "Long guiro", 75: "Claves",
    78: "Mute cuica", 79: "Open cuica",
    80: "Mute triangle", 81: "Open triangle",
    82: "Shaker", 83: "Jingle bell", 84: "Bell tree", 85: "Castanets",
    86: "Mute surdo", 87: "Open surdo",
    99: "Low cowbell", 102: "High cowbell",
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
