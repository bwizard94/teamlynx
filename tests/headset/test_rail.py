"""Rail-switch gestures from the lynx.hw serial pose source drive the headset's ping path."""

import math

import pytest

from lynx.headset.app import HeadsetClient, HeadsetConfig
from lynx.headset.sources import SyntheticSource
from lynx.hw import ButtonEvent, ImuCalibration, ImuLink, MockImuDevice, OrientationConverter, SerialImuPoseSource
from lynx.net.schema import PingType, Team
from lynx.spatial import Pose

EYE = 1.7
START = Pose.from_euler(5.0, -2.0, EYE, 0.0, 0.0, 0.0)


class ManualClock:
    t = 100.0

    def __call__(self):
        return self.t


def turning_head(t):
    """Facing 030 pitched down 10 deg until t = 1 s, then snapped round to 120, level."""
    return (30.0, -10.0, 0.0) if t < 1.0 else (120.0, 0.0, 0.0)


@pytest.fixture
def rig(relay):
    clock = ManualClock()
    dev = MockImuDevice(pose_fn=turning_head, clock=clock, realtime=False)
    link = ImuLink(dev, OrientationConverter(ImuCalibration()), clock=clock)
    src = SerialImuPoseSource("mock://", START, link=link)
    cfg = HeadsetConfig(url=relay.url, node_id=7, callsign="ECHO", team=Team.BLUE, telemetry_hz=30.0)
    client = HeadsetClient(cfg, SyntheticSource(320, 180, 78.0, seed=7, unknowns=False), src).start()
    assert client.wait_connected(5.0)

    def advance(seconds):
        for _ in range(int(round(seconds / 0.01))):
            clock.t += 0.01
            link.feed(dev.read(65536))

    yield client, dev, advance
    client.stop()


def own(client):
    return sorted(client.client.own_pings(), key=lambda ps: ps.ping.ping_id)


def test_single_click_pings_selected_type_at_press_attitude(rig):
    client, dev, advance = rig
    client.selected_ping = PingType.RALLY
    advance(0.95)
    press = dev.now_us()
    advance(0.35)  # head swings to 120 before the SINGLE is classified
    dev.gesture(ButtonEvent.SINGLE, press_t_us=press)
    advance(0.01)
    client.step()
    (ps,) = own(client)
    d = EYE / math.tan(math.radians(10))
    assert ps.ping.ping_type is PingType.RALLY
    assert (ps.ping.x, ps.ping.y, ps.ping.z) == pytest.approx(
        (5 + d * math.sin(math.radians(30)), -2 + d * math.cos(math.radians(30)), 0.0), abs=1e-3)
    client.step()
    assert len(own(client)) == 1  # exactly one ping per gesture


def test_double_click_is_contact_and_long_press_cancels_last(rig):
    client, dev, advance = rig
    advance(0.2)
    dev.gesture(ButtonEvent.SINGLE)
    advance(0.01)
    client.step()
    dev.gesture(ButtonEvent.DOUBLE)
    advance(0.01)
    client.step()
    pings = own(client)
    assert [p.ping.ping_type for p in pings] == [PingType.MARK, PingType.CONTACT]
    dev.gesture(ButtonEvent.LONG)
    advance(0.01)
    client.step()
    assert [p.ping.ping_type for p in own(client)] == [PingType.MARK]


def test_keyboard_space_still_pings_with_serial_source(rig):
    client, dev, advance = rig
    advance(0.2)
    client.request_ping()
    client.step()
    assert len(own(client)) == 1
