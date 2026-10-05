"""Staging-area site file: datum, operator stations and bearing markers.

Example (``deploy/lynx/site.example.json``)::

    {
      "name": "Compound A staging",
      "datum": {"lat": 51.501234, "lon": -1.234567, "h": 85.2},
      "north_reference": "true",
      "convergence_deg": 0.0,
      "declination_deg": 1.1,
      "eye_height_m": 1.7,
      "stations": {"S1": {"e": 0.0, "n": 0.0}, "S2": {"e": 3.0, "n": 0.0}},
      "markers": {
        "FLAG-N": {"e": 0.0, "n": 60.0, "u": 1.5},
        "MAST":   {"lat": 51.50160, "lon": -1.23300, "h": 90.0}
      }
    }

Coordinates are metres in the TeamLynx world frame W (ENU from the datum, ground at z = 0). A
point may instead be given as ``lat``/``lon``/``h`` (ellipsoidal) and is converted through the
datum's :class:`~lynx.field.geodesy.LocalFrame`; then the site needs a ``datum``. ``u`` is height
above the datum plane (default 0). ``north_reference`` is ``"true"`` (ENU, GNSS-native) or
``"grid"`` (site plan drawn on a UTM/OSGB grid; ``convergence_deg`` then rotates GNSS ENU onto it).
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional, Tuple, Union

from .geodesy import LocalFrame


class SiteError(ValueError):
    pass


@dataclass(frozen=True)
class Datum:
    lat: float
    lon: float
    h: float = 0.0


@dataclass(frozen=True)
class SitePoint:
    name: str
    e: float
    n: float
    u: float = 0.0

    @property
    def en(self) -> Tuple[float, float]:
        return (self.e, self.n)


@dataclass
class Site:
    name: str = "site"
    datum: Optional[Datum] = None
    north_reference: str = "true"
    convergence_deg: float = 0.0
    declination_deg: float = 0.0
    eye_height_m: float = 1.7
    stations: Dict[str, SitePoint] = field(default_factory=dict)
    markers: Dict[str, SitePoint] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.north_reference not in ("true", "grid"):
            raise SiteError("north_reference must be 'true' or 'grid'")
        if self.north_reference == "true" and self.convergence_deg:
            raise SiteError("convergence_deg only applies to north_reference 'grid'")

    @property
    def frame(self) -> LocalFrame:
        if self.datum is None:
            raise SiteError("site has no datum lat/lon; GNSS mode needs one (lynx-field survey)")
        return LocalFrame(self.datum.lat, self.datum.lon, self.datum.h, self.convergence_deg)

    def station(self, name: str) -> SitePoint:
        try:
            return self.stations[name]
        except KeyError as exc:
            raise SiteError(f"unknown station {name!r}; site has {sorted(self.stations)}") from exc

    def marker(self, name: str) -> SitePoint:
        try:
            return self.markers[name]
        except KeyError as exc:
            raise SiteError(f"unknown marker {name!r}; site has {sorted(self.markers)}") from exc

    # -- serialisation ------------------------------------------------------------------------

    @classmethod
    def from_dict(cls, d: dict) -> "Site":
        known = {"name", "datum", "north_reference", "convergence_deg", "declination_deg", "eye_height_m",
                 "stations", "markers"}
        unknown = set(d) - known
        if unknown:
            raise SiteError(f"unknown site fields {sorted(unknown)}")
        datum = None
        if d.get("datum") is not None:
            dd = d["datum"]
            datum = Datum(float(dd["lat"]), float(dd["lon"]), float(dd.get("h", 0.0)))
        site = cls(name=str(d.get("name", "site")), datum=datum,
                   north_reference=str(d.get("north_reference", "true")),
                   convergence_deg=float(d.get("convergence_deg", 0.0)),
                   declination_deg=float(d.get("declination_deg", 0.0)),
                   eye_height_m=float(d.get("eye_height_m", 1.7)))
        for key, target in (("stations", site.stations), ("markers", site.markers)):
            for name, p in (d.get(key) or {}).items():
                target[name] = site._point(name, p)
        if not site.markers:
            raise SiteError("site needs at least one bearing marker")
        return site

    def _point(self, name: str, p: dict) -> SitePoint:
        if "e" in p or "n" in p:
            return SitePoint(name, float(p.get("e", 0.0)), float(p.get("n", 0.0)), float(p.get("u", 0.0)))
        if "lat" in p and "lon" in p:
            e, n, u = self.frame.to_enu(float(p["lat"]), float(p["lon"]), float(p.get("h", self.frame.h0)))
            return SitePoint(name, e, n, u)
        raise SiteError(f"point {name!r} needs e/n or lat/lon")

    def to_dict(self) -> dict:
        out: dict = {"name": self.name}
        if self.datum is not None:
            out["datum"] = {"lat": self.datum.lat, "lon": self.datum.lon, "h": self.datum.h}
        out.update(north_reference=self.north_reference, convergence_deg=self.convergence_deg,
                   declination_deg=self.declination_deg, eye_height_m=self.eye_height_m)
        out["stations"] = {k: {"e": p.e, "n": p.n, "u": p.u} for k, p in self.stations.items()}
        out["markers"] = {k: {"e": p.e, "n": p.n, "u": p.u} for k, p in self.markers.items()}
        return out

    @classmethod
    def load(cls, path: Union[str, Path]) -> "Site":
        try:
            return cls.from_dict(json.loads(Path(path).read_text()))
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, SiteError):
                raise
            raise SiteError(f"{path}: {exc}") from exc

    def save(self, path: Union[str, Path]) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.to_dict(), indent=2) + "\n")


def bearing_deg(frm: Tuple[float, float], to: Tuple[float, float]) -> float:
    """Compass bearing (clockwise from the frame's North, [0, 360)) from ``frm`` to ``to`` (E, N)."""
    return math.degrees(math.atan2(to[0] - frm[0], to[1] - frm[1])) % 360.0


def elevation_deg(frm: Tuple[float, float, float], to: Tuple[float, float, float]) -> float:
    rng = math.hypot(to[0] - frm[0], to[1] - frm[1])
    return math.degrees(math.atan2(to[2] - frm[2], rng))
