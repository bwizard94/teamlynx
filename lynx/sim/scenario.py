"""Default three-operator scenario shared by the launcher and the self-check.

ALPHA stands at the staging datum looking North and 10 deg down, so its reticle raycast lands
on the ground ~9.6 m North of the datum. BRAVO (NE of the ping, facing SW) and CHARLIE (West of
the ping, facing East) both have that point in view from very different perspectives.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List


@dataclass(frozen=True)
class OperatorSpec:
    node: int
    callsign: str
    team: str
    x: float
    y: float
    heading: float
    pitch: float = -10.0
    eye_height: float = 1.7


DEFAULT_SCENARIO: List[OperatorSpec] = [
    OperatorSpec(1, "ALPHA", "blue", 0.0, 0.0, 0.0),
    OperatorSpec(2, "BRAVO", "green", 15.0, 25.0, 225.0),
    OperatorSpec(3, "CHARLIE", "blue", -20.0, 10.0, 90.0),
]
