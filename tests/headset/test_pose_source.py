import sys

import pytest

from lynx.headset import pose as pose_mod
from lynx.headset.pose import (
    KeyboardPoseSource,
    PoseSource,
    PoseSourceUnavailableError,
    StaticPoseSource,
    create_pose_source,
)
from lynx.spatial import Pose

START = Pose.from_euler(0.0, 0.0, 1.7, 0.0, 0.0, 0.0)


def test_keyboard_steps_and_trigger_edge():
    src = KeyboardPoseSource(START)
    assert isinstance(src, PoseSource)
    for k in "dd":
        assert src.handle_key(ord(k))
    src.handle_key(ord("w"))
    src.handle_key(ord("]"))
    h, p, r = src.read(0.0).pose.euler
    assert (h, p, r) == pytest.approx((10.0, 2.0, 2.0))

    src.handle_key(ord("r"))
    src.handle_key(ord("i"))  # 1 m along heading 10 deg
    s = src.read(0.0)
    assert s.pose.position[:2] == pytest.approx((0.17365, 0.98481), abs=1e-4)
    assert s.pose.euler[1:] == pytest.approx((0.0, 0.0))
    assert not s.trigger

    assert src.handle_key(32)
    assert src.read(0.0).trigger
    assert not src.read(0.0).trigger, "trigger is a one-shot rising edge"
    assert not src.handle_key(ord("z"))


def test_static_spec_and_unknown_scheme():
    s = create_pose_source("static:1,2,1.5,45,-3,0", START)
    assert isinstance(s, StaticPoseSource)
    assert s.read(0.0).pose.euler == pytest.approx((45.0, -3.0, 0.0))
    assert create_pose_source("keyboard", START).read(0.0).pose.position == pytest.approx((0, 0, 1.7))
    with pytest.raises(PoseSourceUnavailableError, match="available"):
        create_pose_source("lidar", START)


FAKE_HW = '''
from lynx.headset.pose import PoseSample, register_pose_source


class FakeImu:
    name = "serial"

    def __init__(self, port, initial):
        self.port, self.pose = port, initial

    def read(self, now):
        return PoseSample(self.pose, trigger=True, flags=0x04)

    def handle_key(self, key):
        return False

    def close(self):
        pass


register_pose_source("serial", FakeImu)
'''


def test_serial_source_is_a_lazy_plugin(monkeypatch, tmp_path):
    monkeypatch.setattr(pose_mod, "_REGISTRY", {k: v for k, v in pose_mod._REGISTRY.items() if k != "serial"})
    monkeypatch.setattr(pose_mod, "_PLUGIN_MODULES", {"serial": "lynx_test_absent_hw"})
    with pytest.raises(PoseSourceUnavailableError, match="lynx_test_absent_hw"):
        create_pose_source("serial:/dev/ttyUSB0", START)

    (tmp_path / "lynx_test_fake_hw.py").write_text(FAKE_HW)
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, "lynx_test_fake_hw", raising=False)
    monkeypatch.setattr(pose_mod, "_PLUGIN_MODULES", {"serial": "lynx_test_fake_hw"})
    src = create_pose_source("serial:/dev/ttyACM0", START)
    assert isinstance(src, PoseSource) and src.port == "/dev/ttyACM0"
    s = src.read(0.0)
    assert s.trigger and s.flags == 0x04 and s.pose.position == pytest.approx((0, 0, 1.7))
