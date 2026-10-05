"""HUD palette (BGR) and typography."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

import cv2

BGR = Tuple[int, int, int]

TEAM_COLORS = {
    "green": (60, 255, 60),
    "blue": (255, 170, 40),
    "cyan": (255, 255, 0),
    "white": (240, 240, 240),
}
AMBER: BGR = (0, 190, 255)
RED: BGR = (40, 40, 255)
HUD_GREEN: BGR = (120, 255, 140)
HUD_DIM: BGR = (70, 150, 85)
PING_YELLOW: BGR = (0, 235, 255)
SHADOW: BGR = (0, 0, 0)
FONT = cv2.FONT_HERSHEY_SIMPLEX
FONT_MONO = cv2.FONT_HERSHEY_PLAIN


def team_bgr(name: Optional[str]) -> BGR:
    return TEAM_COLORS.get((name or "green").lower(), TEAM_COLORS["green"])


@dataclass(frozen=True)
class HudStyle:
    primary: BGR = HUD_GREEN
    dim: BGR = HUD_DIM
    unverified: BGR = AMBER
    tango: BGR = RED
    ping: BGR = PING_YELLOW
    panel_alpha: float = 0.45
    line: int = 1
    compass_span_deg: float = 120.0
    radar_range_m: float = 75.0
    pip_frac: float = 0.24
    show_bft_markers: bool = True
