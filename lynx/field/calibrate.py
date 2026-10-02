"""Staging-area datum calibration: operator position + IMU heading tare from bearing markers.

Workflow (``docs/field/net-calibration.md``): the operator stands on a surveyed *station* (or at a
GNSS-measured spot), faces a *bearing marker* whose position is known in the site frame, holds
still and taps the rail switch. The head tracker's attitude history around the press gives the
measured line-of-sight azimuth ``m`` (with declination/convergence applied, heading offset 0). The
true bearing from the eye to the marker is ``b = atan2(dE, dN)``. Then

    heading offset  theta = wrap(b - m)

which is exactly what :meth:`lynx.hw.orientation.OrientationConverter.tare` computes for a single
bearing. With several markers the offsets are combined as a weighted circular mean and their
residuals ``r_i = wrap(m_i + theta - b_i)`` expose a wrong station, a mis-surveyed marker or
magnetic disturbance.

Error model per sighting ``i`` at range ``rho_i``:

    sigma_i^2 = sigma_imu^2 + (sigma_marker / rho_i)^2        (radians)

and the station position error (common to all sightings, so *not* averaged down) propagates as

    d theta / dE = sum w_i (-dN_i / rho_i^2) / sum w_i,   d theta / dN = sum w_i (dE_i / rho_i^2) / sum w_i
    sigma_theta^2 = 1 / sum w_i  +  sigma_E^2 (d theta/dE)^2 + sigma_N^2 (d theta/dN)^2

This is why GNSS-positioned tares need distant markers: a 1.5 m GNSS error against a marker 30 m
away is a 2.9 deg heading error; at 150 m it is 0.6 deg.

Resection (no station): with >= 3 markers, solve (E, N, theta) by Gauss-Newton on
``r_i = wrap(m_i + theta - b_i(E, N))`` with Jacobian rows ``[dN_i / rho_i^2, -dE_i / rho_i^2, 1]``.
The covariance is ``sigma^2 (J^T J)^-1``; it blows up when the operator stands on the circle through
three markers (the classic "danger circle"), which is reported instead of silently accepted.
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple, Union

import numpy as np

from lynx.hw.orientation import ImuCalibration, OrientationConverter, TareError
from lynx.spatial.rotations import matrix_to_euler, quat_to_matrix, wrap_deg_180

from .geodesy import circular_mean_deg
from .site import Site, bearing_deg, elevation_deg

DEFAULT_SIGMA_IMU_DEG = 0.5  # pointing repeatability of a head held on a marker
DEFAULT_SIGMA_MARKER_M = 0.05


class CalibrationError(RuntimeError):
    pass


# ------------------------------------------------------------------------------------ sightings


@dataclass(frozen=True)
class Sighting:
    """One tap on a marker: the untared line-of-sight azimuth and pitch, averaged while still."""

    marker: str
    azimuth_deg: float
    pitch_deg: float
    spread_deg: float
    samples: int
    host_time: float = 0.0


def untared_converter(cal: ImuCalibration) -> OrientationConverter:
    return OrientationConverter(ImuCalibration(**{**asdict(cal), "heading_offset_deg": 0.0, "tared": False}))


def sighting_from_quats(marker: str, quats: Iterable[Sequence[float]], cal: ImuCalibration,
                        max_pitch_deg: float = 45.0, max_spread_deg: float = 1.5,
                        host_time: float = 0.0) -> Sighting:
    """Average raw sensor quaternions captured while the operator held still on ``marker``."""
    conv = untared_converter(cal)
    az: List[float] = []
    pitches: List[float] = []
    for q in quats:
        h, p, _ = matrix_to_euler(quat_to_matrix(conv.body_quat(q)))
        if abs(p) > max_pitch_deg:
            raise TareError(f"head pitched {p:.0f} deg on {marker}; keep |pitch| <= {max_pitch_deg:.0f}")
        az.append(h)
        pitches.append(p)
    if len(az) < 3:
        raise TareError(f"only {len(az)} IMU samples around the tap on {marker}")
    mean, spread = circular_mean_deg(az)
    if spread > max_spread_deg:
        raise TareError(f"head moved during the {marker} sighting (std {spread:.1f} deg > {max_spread_deg})")
    return Sighting(marker, mean, float(np.mean(pitches)), spread, len(az), host_time)


def capture_quats(link, press_t_us: int, window_s: float = 0.5, guard_s: float = 0.06,
                  step_us: int = 10_000) -> List[Tuple[float, float, float, float]]:
    """Raw quaternions from ``link``'s pose history in ``[press - guard - window, press - guard]``.

    The window ends before the press so the jolt of the thumb on the switch is excluded.
    """
    t1 = press_t_us - int(guard_s * 1e6)
    t0 = t1 - int(window_s * 1e6)
    seen = set()
    out = []
    for t in range(t0, t1 + 1, step_us):
        hp = link.pose_at(t, tolerance_us=step_us)
        if hp is not None and hp.t_us not in seen:
            seen.add(hp.t_us)
            out.append(hp.q_raw)
    return out


# ------------------------------------------------------------------------------------ solutions


@dataclass
class SightingResult:
    marker: str
    measured_deg: float
    true_bearing_deg: float
    residual_deg: float
    range_m: float
    pitch_deg: float
    expected_pitch_deg: float

    @property
    def pitch_residual_deg(self) -> float:
        return self.pitch_deg - self.expected_pitch_deg


@dataclass
class TareSolution:
    method: str  # station | gnss | resection
    e: float
    n: float
    heading_offset_deg: float
    heading_std_deg: float
    position_std_m: Tuple[float, float]
    results: List[SightingResult]
    station: Optional[str] = None

    @property
    def max_residual_deg(self) -> float:
        return max((abs(r.residual_deg) for r in self.results), default=0.0)


def _sighting_sigma_rad(rho: float, sigma_imu_deg: float, sigma_marker_m: float) -> float:
    return math.sqrt(math.radians(sigma_imu_deg) ** 2 + (sigma_marker_m / max(rho, 0.1)) ** 2)


def _results(site: Site, e: float, n: float, eye: float, sightings: Sequence[Sighting],
             theta: float) -> List[SightingResult]:
    out = []
    for s in sightings:
        m = site.marker(s.marker)
        b = bearing_deg((e, n), m.en)
        rho = math.hypot(m.e - e, m.n - n)
        out.append(SightingResult(s.marker, s.azimuth_deg, b, wrap_deg_180(s.azimuth_deg + theta - b), rho,
                                  s.pitch_deg, elevation_deg((e, n, eye), (m.e, m.n, m.u))))
    return out


def solve_known_position(site: Site, e: float, n: float, sightings: Sequence[Sighting], *,
                         eye_height: Optional[float] = None, position_std_m: Tuple[float, float] = (0.02, 0.02),
                         method: str = "station", station: Optional[str] = None,
                         sigma_imu_deg: float = DEFAULT_SIGMA_IMU_DEG, sigma_marker_m: float = DEFAULT_SIGMA_MARKER_M,
                         min_range_m: float = 10.0, max_residual_deg: float = 2.0) -> TareSolution:
    """Heading offset from >= 1 sighting taken at a known position (station or GNSS average)."""
    if not sightings:
        raise CalibrationError("no sightings")
    eye = site.eye_height_m if eye_height is None else eye_height
    sx = sy = wsum = 0.0
    dth_de = dth_dn = 0.0
    for s in sightings:
        m = site.marker(s.marker)
        de, dn = m.e - e, m.n - n
        rho2 = de * de + dn * dn
        if rho2 < min_range_m ** 2:
            raise CalibrationError(f"marker {s.marker} is {math.sqrt(rho2):.1f} m away; need >= {min_range_m:.0f} m")
        w = 1.0 / _sighting_sigma_rad(math.sqrt(rho2), sigma_imu_deg, sigma_marker_m) ** 2
        off = math.radians(bearing_deg((e, n), m.en) - s.azimuth_deg)
        sx += w * math.sin(off)
        sy += w * math.cos(off)
        dth_de += w * (-dn / rho2)
        dth_dn += w * (de / rho2)
        wsum += w
    theta = math.degrees(math.atan2(sx, sy))
    dth_de /= wsum
    dth_dn /= wsum
    var = 1.0 / wsum + (position_std_m[0] * dth_de) ** 2 + (position_std_m[1] * dth_dn) ** 2
    sol = TareSolution(method, e, n, wrap_deg_180(theta), math.degrees(math.sqrt(var)), position_std_m,
                       _results(site, e, n, eye, sightings, theta), station)
    if len(sightings) > 1 and sol.max_residual_deg > max_residual_deg:
        worst = max(sol.results, key=lambda r: abs(r.residual_deg))
        raise CalibrationError(
            f"markers disagree by up to {sol.max_residual_deg:.1f} deg (worst {worst.marker}); check the "
            f"station, the marker coordinates or nearby steel/vehicles (magnetic disturbance)")
    return sol


def solve_resection(site: Site, sightings: Sequence[Sighting], initial: Tuple[float, float] = (0.0, 0.0), *,
                    eye_height: Optional[float] = None, sigma_imu_deg: float = DEFAULT_SIGMA_IMU_DEG,
                    max_position_std_m: float = 1.0, max_residual_deg: float = 2.0,
                    iterations: int = 25) -> TareSolution:
    """Position and heading offset from >= 3 marker sightings (three-point resection)."""
    if len({s.marker for s in sightings}) < 3:
        raise CalibrationError("resection needs sightings on at least 3 different markers")
    eye = site.eye_height_m if eye_height is None else eye_height
    pts = [site.marker(s.marker) for s in sightings]
    meas = np.radians([s.azimuth_deg for s in sightings])
    x = np.array([initial[0], initial[1], 0.0])
    b0 = [bearing_deg((x[0], x[1]), p.en) for p in pts]
    x[2] = math.radians(circular_mean_deg([b - s.azimuth_deg for b, s in zip(b0, sightings)])[0])

    def residuals(x):
        r = np.empty(len(pts))
        J = np.empty((len(pts), 3))
        for i, p in enumerate(pts):
            de, dn = p.e - x[0], p.n - x[1]
            rho2 = max(de * de + dn * dn, 1e-6)
            b = math.atan2(de, dn)
            r[i] = math.atan2(math.sin(meas[i] + x[2] - b), math.cos(meas[i] + x[2] - b))
            J[i] = (dn / rho2, -de / rho2, 1.0)
        return r, J

    for _ in range(iterations):
        r, J = residuals(x)
        try:
            step = np.linalg.lstsq(J, -r, rcond=None)[0]
        except np.linalg.LinAlgError as exc:  # pragma: no cover - lstsq rarely raises
            raise CalibrationError("resection geometry is singular") from exc
        x = x + step
        if np.linalg.norm(step[:2]) < 1e-6 and abs(step[2]) < 1e-9:
            break
    r, J = residuals(x)
    dof = len(pts) - 3
    sigma = math.radians(sigma_imu_deg)
    if dof > 0:
        sigma = max(sigma, math.sqrt(float(r @ r) / dof))
    JtJ = J.T @ J
    if np.linalg.cond(JtJ) > 1e12:
        raise CalibrationError("resection geometry is singular (standing on the circle through the markers?)")
    cov = sigma ** 2 * np.linalg.inv(JtJ)
    std_e, std_n, std_th = (math.sqrt(max(0.0, cov[i, i])) for i in range(3))
    if math.hypot(std_e, std_n) > max_position_std_m:
        raise CalibrationError(
            f"resection position uncertainty {math.hypot(std_e, std_n):.1f} m > {max_position_std_m} m; pick markers "
            f"spread round the horizon (>= 60 deg apart) and not on a circle through your position")
    theta = math.degrees(x[2])
    sol = TareSolution("resection", float(x[0]), float(x[1]), wrap_deg_180(theta), math.degrees(std_th),
                       (std_e, std_n), _results(site, float(x[0]), float(x[1]), eye, sightings, theta))
    if dof > 0 and sol.max_residual_deg > max_residual_deg:
        raise CalibrationError(f"resection residual {sol.max_residual_deg:.1f} deg > {max_residual_deg} deg")
    return sol


# ------------------------------------------------------------------------------------ result file


@dataclass
class DriftCheck:
    time_unix: float
    marker: str
    error_deg: float


@dataclass
class FieldCalibration:
    """Result of ``lynx-field calibrate``: what the headset needs at game start."""

    node: int
    method: str
    position: Tuple[float, float, float]
    position_std_m: Tuple[float, float]
    heading_offset_deg: float
    heading_std_deg: float
    imu: ImuCalibration
    station: Optional[str] = None
    sightings: List[dict] = field(default_factory=list)
    time_unix: float = 0.0
    checks: List[DriftCheck] = field(default_factory=list)
    site: str = ""
    version: int = 1

    @classmethod
    def from_solution(cls, sol: TareSolution, node: int, imu: ImuCalibration, eye_height: float,
                      site_name: str = "", now: Optional[float] = None) -> "FieldCalibration":
        cal = ImuCalibration(**{**asdict(imu), "heading_offset_deg": sol.heading_offset_deg, "tared": True})
        return cls(node=node, method=sol.method, position=(sol.e, sol.n, eye_height),
                   position_std_m=tuple(sol.position_std_m), heading_offset_deg=sol.heading_offset_deg,
                   heading_std_deg=sol.heading_std_deg, imu=cal, station=sol.station,
                   sightings=[{**asdict(r), "pitch_residual_deg": r.pitch_residual_deg} for r in sol.results],
                   time_unix=time.time() if now is None else now, site=site_name)

    @property
    def time_iso(self) -> str:
        return datetime.fromtimestamp(self.time_unix, tz=timezone.utc).isoformat(timespec="seconds")

    def to_dict(self) -> dict:
        return {
            "version": self.version, "node": self.node, "site": self.site, "method": self.method,
            "station": self.station, "position": list(self.position), "position_std_m": list(self.position_std_m),
            "heading_offset_deg": self.heading_offset_deg, "heading_std_deg": self.heading_std_deg,
            "time_unix": self.time_unix, "time_utc": self.time_iso, "imu": json.loads(self.imu.to_json()),
            "sightings": self.sightings, "checks": [asdict(c) for c in self.checks],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "FieldCalibration":
        return cls(node=int(d["node"]), method=str(d["method"]), position=tuple(float(v) for v in d["position"]),
                   position_std_m=tuple(float(v) for v in d["position_std_m"]),
                   heading_offset_deg=float(d["heading_offset_deg"]), heading_std_deg=float(d["heading_std_deg"]),
                   imu=ImuCalibration.from_json(json.dumps(d["imu"])), station=d.get("station"),
                   sightings=list(d.get("sightings", [])), time_unix=float(d.get("time_unix", 0.0)),
                   checks=[DriftCheck(**c) for c in d.get("checks", [])], site=str(d.get("site", "")),
                   version=int(d.get("version", 1)))

    def save(self, directory: Union[str, Path]) -> Tuple[Path, Path]:
        """Write ``field-cal.json`` and ``imu.json`` (the latter for ``--pose serial:PORT?cal=``)."""
        out = Path(directory)
        out.mkdir(parents=True, exist_ok=True)
        fc = out / "field-cal.json"
        tmp = fc.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.to_dict(), indent=2) + "\n")
        tmp.replace(fc)
        imu = out / "imu.json"
        self.imu.save(imu)
        return fc, imu

    @classmethod
    def load(cls, directory_or_file: Union[str, Path]) -> "FieldCalibration":
        p = Path(directory_or_file)
        if p.is_dir():
            p = p / "field-cal.json"
        return cls.from_dict(json.loads(p.read_text()))


def check_error_deg(site: Site, cal: FieldCalibration, sighting: Sighting) -> float:
    """Heading error of the *tared* head against a marker: positive = head reads clockwise of truth."""
    m = site.marker(sighting.marker)
    b = bearing_deg(cal.position[:2], m.en)
    return wrap_deg_180(sighting.azimuth_deg + cal.heading_offset_deg - b)


# ------------------------------------------------------------------------------------ drift


@dataclass(frozen=True)
class DriftEstimate:
    minutes_since_tare: float
    rate_deg_per_min: float
    predicted_error_deg: float
    status: str  # ok | warn | retare
    source: str  # checks | prior
    cog_bias_deg: Optional[float] = None

    def alert(self) -> Optional[str]:
        if self.status == "ok":
            return None
        word = "RE-TARE" if self.status == "retare" else "HDG DRIFT"
        return f"! {word} ~{self.predicted_error_deg:.0f} DEG ({self.minutes_since_tare:.0f} MIN)"


class DriftMonitor:
    """Heading-drift estimate since the datum tare.

    Sources, best first:

    1. *Marker checks* (``lynx-field calibrate --check``): signed heading error ``e_k`` at
       ``tau_k`` minutes after the tare. Error at the tare is zero by construction, so the drift
       rate is the through-origin least-squares slope ``a = sum e_k tau_k / sum tau_k^2``.
    2. A *prior* bound when there are no checks: ``prior_rate_deg_per_min`` (game rotation vector
       gyro-only drift; 0 for the magnetometer-referenced rotation vector) plus the IMU's own
       accuracy estimate when provided.

    Optional advisory: walking heading vs GNSS course over ground. While the operator walks
    (speed >= ``cog_min_speed``) with the head roughly level, the median of
    ``wrap(heading - course)`` over the last ``cog_window_s`` is reported as ``cog_bias_deg``;
    people look around while walking, so it never drives the status on its own.
    """

    def __init__(self, tare_time_unix: float, prior_rate_deg_per_min: float = 0.0, warn_deg: float = 3.0,
                 retare_deg: float = 6.0, cog_min_speed: float = 1.2, cog_window_s: float = 120.0,
                 cog_min_samples: int = 30) -> None:
        self.tare_time = tare_time_unix
        self.prior_rate = prior_rate_deg_per_min
        self.warn_deg = warn_deg
        self.retare_deg = retare_deg
        self.checks: List[DriftCheck] = []
        self.cog_min_speed = cog_min_speed
        self.cog_window_s = cog_window_s
        self.cog_min_samples = cog_min_samples
        self._cog: List[Tuple[float, float]] = []

    @classmethod
    def for_calibration(cls, cal: FieldCalibration, report: str = "rv", **kwargs) -> "DriftMonitor":
        prior = 0.5 if report == "grv" else 0.0
        mon = cls(cal.time_unix, prior_rate_deg_per_min=kwargs.pop("prior_rate_deg_per_min", prior), **kwargs)
        for c in cal.checks:
            mon.add_check(c)
        return mon

    def add_check(self, check: DriftCheck) -> None:
        self.checks.append(check)

    def add_course_sample(self, now_unix: float, heading_deg: float, pitch_deg: float,
                          course_deg: Optional[float], speed_mps: Optional[float]) -> None:
        if course_deg is None or speed_mps is None or speed_mps < self.cog_min_speed or abs(pitch_deg) > 20.0:
            return
        self._cog.append((now_unix, wrap_deg_180(heading_deg - course_deg)))
        cutoff = now_unix - self.cog_window_s
        while self._cog and self._cog[0][0] < cutoff:
            self._cog.pop(0)

    def estimate(self, now_unix: float, imu_accuracy_deg: float = float("nan")) -> DriftEstimate:
        minutes = max(0.0, (now_unix - self.tare_time) / 60.0)
        taus = [(c.time_unix - self.tare_time) / 60.0 for c in self.checks]
        pairs = [(t, c.error_deg) for t, c in zip(taus, self.checks) if t > 0.0]
        if pairs:
            rate = sum(t * e for t, e in pairs) / sum(t * t for t, _ in pairs)
            predicted = abs(rate * minutes)
            source = "checks"
        else:
            rate = self.prior_rate
            predicted = abs(rate) * minutes
            source = "prior"
        if math.isfinite(imu_accuracy_deg):
            predicted = math.hypot(predicted, imu_accuracy_deg)
        status = "retare" if predicted >= self.retare_deg else "warn" if predicted >= self.warn_deg else "ok"
        cog = None
        if len(self._cog) >= self.cog_min_samples:
            cog = float(np.median([d for _, d in self._cog]))
        return DriftEstimate(minutes, rate, predicted, status, source, cog)
