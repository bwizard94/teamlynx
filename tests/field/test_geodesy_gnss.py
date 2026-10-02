"""WGS84 transforms, NMEA/UBX encoding, the GNSS reader and correlation-aware averaging."""

import math

import pytest

from lynx.field.geodesy import (
    WGS84_A,
    Gga,
    LocalFrame,
    NmeaError,
    circular_mean_deg,
    ecef_to_geodetic,
    format_gga,
    geodetic_to_ecef,
    nmea_checksum,
    parse_nmea,
    rotate_true_to_grid,
    ubx_cfg_rate,
    ublox_setup_frames,
)
from lynx.field.gnss import GnssFix, GnssReader, MockGnssReceiver, average_fixes, collect_fixes


@pytest.mark.parametrize("lat,lon,h", [(0, 0, 0), (51.5, -1.25, 85.0), (-33.9, 151.2, 30.0), (89.9, 10, 100),
                                       (-89.99, -170, -50)])
def test_geodetic_ecef_round_trip(lat, lon, h):
    x, y, z = geodetic_to_ecef(lat, lon, h)
    lat2, lon2, h2 = ecef_to_geodetic(x, y, z)
    assert lat2 == pytest.approx(lat, abs=1e-10)
    assert lon2 == pytest.approx(lon, abs=1e-10)
    assert h2 == pytest.approx(h, abs=1e-6)


def test_enu_matches_closed_form_at_equator():
    f = LocalFrame(0.0, 0.0, 0.0)
    e, n, u = f.to_enu(0.0, 0.001, 0.0)
    assert e == pytest.approx(WGS84_A * math.radians(0.001), rel=1e-6)
    assert abs(n) < 1e-6 and u == pytest.approx(0.0, abs=0.01)  # curvature drop d^2/2R ~ 1 mm at 111 m


def test_enu_round_trip_and_axes():
    f = LocalFrame(51.501234, -1.234567, 85.2)
    for enu in [(0, 0, 0), (100, 0, 0), (0, 250, 0), (-300, 120, 4), (1500, -900, -10)]:
        lat, lon, h = f.to_geodetic(*enu)
        assert f.to_enu(lat, lon, h) == pytest.approx(enu, abs=1e-6)
    lat, lon, _ = f.to_geodetic(0, 100, 0)
    assert lat > f.lat0 and lon == pytest.approx(f.lon0, abs=1e-9)  # +n is north


def test_grid_rotation_sign_matches_imu_convergence():
    # grid North lies at true bearing +gamma, so a point on it has grid bearing 0
    g = 2.5
    e, n = rotate_true_to_grid(math.sin(math.radians(g)), math.cos(math.radians(g)), g)
    assert e == pytest.approx(0.0, abs=1e-12) and n == pytest.approx(1.0)
    fg = LocalFrame(51.5, -1.25, 80.0, convergence_deg=g)
    ft = LocalFrame(51.5, -1.25, 80.0)
    lat, lon, h = ft.to_geodetic(30.0, 40.0, 0.0)
    eg, ng, _ = fg.to_enu(lat, lon, h)
    b_true = math.degrees(math.atan2(30.0, 40.0))
    assert math.degrees(math.atan2(eg, ng)) == pytest.approx(b_true - g, abs=1e-9)
    assert fg.to_geodetic(eg, ng, 0.0) == pytest.approx((lat, lon, h), abs=1e-9)


def test_parse_reference_gga_and_checksum():
    g = parse_nmea("$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,*47")
    assert isinstance(g, Gga) and g.valid
    assert g.lat == pytest.approx(48 + 7.038 / 60) and g.lon == pytest.approx(11 + 31.0 / 60)
    assert (g.quality, g.num_sats, g.hdop) == (1, 8, 0.9)
    assert g.h_ellipsoid == pytest.approx(545.4 + 46.9)
    assert g.utc_s == 12 * 3600 + 35 * 60 + 19
    with pytest.raises(NmeaError):
        parse_nmea("$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,*48")
    with pytest.raises(NmeaError):
        parse_nmea("GPGGA,no-dollar*00")
    assert parse_nmea("$GPGSV,1,1,00*79") is None


def test_format_parse_round_trip_southern_western():
    line = format_gga(3600.5, -33.8688197, -151.2092955, 12.25, 4, 17, 0.6, 22.1)
    g = parse_nmea(line)
    assert g.lat == pytest.approx(-33.8688197, abs=1e-7) and g.lon == pytest.approx(-151.2092955, abs=1e-7)
    assert g.quality == 4 and g.h_ellipsoid == pytest.approx(34.35)
    body = line[1:line.index("*")]
    assert int(line[line.index("*") + 1:line.index("*") + 3], 16) == nmea_checksum(body)


def test_ubx_cfg_rate_matches_reference_frame():
    # u-blox reference: 5 Hz measurement rate
    assert ubx_cfg_rate(200).hex(" ") == "b5 62 06 08 06 00 c8 00 01 00 01 00 de 6a"
    frames = ublox_setup_frames(5.0, "m8")
    assert frames[0] == ubx_cfg_rate(200) and len(frames) == 8
    assert ublox_setup_frames(10.0, "m10")[0][:4] == b"\xb5\x62\x06\x8a"


def test_circular_mean_wraps():
    m, s = circular_mean_deg([359.0, 1.0, 0.5, 359.5])
    assert min(m, 360 - m) < 0.01 and s < 1.0


def test_reader_assembles_fix_with_gst_and_course():
    frame = LocalFrame(51.5, -1.25, 80.0)
    rx = MockGnssReceiver(frame, track=lambda t: (2.0 * t, 10.0), realtime=False, std_m=0.9)
    reader = GnssReader(clock=lambda: 100.0)
    first = None
    for line in rx.epoch_lines(3.0):
        first = reader.feed_line(line, now=100.0) or first
    assert isinstance(first, GnssFix)
    e, n, _ = first.enu(frame)
    assert (e, n) == pytest.approx((6.0, 10.0), abs=0.02)  # NMEA 1e-5 arc-minute ~ 1.9 cm
    assert first.speed_mps == pytest.approx(2.0, abs=0.01) and first.course_deg == pytest.approx(90.0, abs=0.1)
    assert first.std_e == pytest.approx(0.8 * 2.5 / math.sqrt(2))  # no GST yet: HDOP x UERE / sqrt 2
    fix = None
    for line in rx.epoch_lines(3.2):  # GST of the previous epoch now applies
        fix = reader.feed_line(line, now=100.2) or fix
    assert fix.std_e == pytest.approx(0.9) and fix.std_n == pytest.approx(0.9)
    assert reader.latest(100.3) is not None and reader.latest(103.0) is None  # stale after 2 s


def test_reader_hdop_fallback_and_invalid_fix():
    reader = GnssReader(uere_m=2.0)
    fix = reader.feed_line(format_gga(10.0, 51.5, -1.25, 80.0, 1, 9, 1.5), now=1.0)
    assert fix.std_e == pytest.approx(1.5 * 2.0 / math.sqrt(2))
    assert reader.feed_line(format_gga(11.0, 51.5, -1.25, 80.0, 0, 0, 99.0), now=2.0) is None
    assert reader.latest(2.0) is None
    reader.feed_line("$GPGGA,garbage*00", now=3.0)
    assert reader.bad_sentences == 1


def test_mock_receiver_realtime_readline_and_writes():
    rx = MockGnssReceiver(rate_hz=20.0, realtime=True, timeout=0.5)
    line = rx.readline()
    assert line.startswith(b"$GNRMC")
    rx.write(b"\xb5\x62")
    assert rx.written == [b"\xb5\x62"]


def _fix(t, e, n, frame, std=1.0, quality=1, hdop=0.8):
    lat, lon, h = frame.to_geodetic(e, n, 0.0)
    return GnssFix(t, lat, lon, h, quality, 12, hdop, std, std, 2 * std)


def test_average_rejects_outliers_and_is_correlation_aware():
    import random

    frame = LocalFrame(51.5, -1.25, 80.0)
    rng = random.Random(4)
    fixes = [_fix(i * 0.2, 5 + rng.gauss(0, 0.5), -3 + rng.gauss(0, 0.5), frame) for i in range(600)]  # 120 s
    fixes[100] = _fix(20.0, 60.0, 60.0, frame)  # multipath jump
    fixes[200] = _fix(40.0, 5.0, -3.0, frame, quality=0)
    avg = average_fixes(fixes, frame)
    assert (avg.e, avg.n) == pytest.approx((5.0, -3.0), abs=0.1)
    assert avg.rejected == 2
    # 120 s with tau = 60 s is 2 effective samples: sigma_mean = max(scatter, rx sigma) / sqrt(2), not / sqrt(598)
    assert avg.std_e == pytest.approx(1.0 / math.sqrt(2), rel=0.05)
    with pytest.raises(ValueError):
        average_fixes([_fix(0, 0, 0, frame, quality=0)], frame)


def test_collect_fixes_with_started_reader():
    rx = MockGnssReceiver(rate_hz=50.0)
    reader = GnssReader(rx).start()
    try:
        fixes = collect_fixes(reader, 0.3)
    finally:
        reader.stop()
    assert len(fixes) >= 5
