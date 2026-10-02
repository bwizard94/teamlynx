"""``lynx-field`` command line: bandwidth, keyboard calibration with the mock tracker, headset dry run."""

import json

import pytest

from lynx.field import cli
from lynx.field.calibrate import FieldCalibration

from .test_calibrate import SITE


def test_bandwidth_table(capsys):
    assert cli.main(["bandwidth", "--nodes", "10", "--rate", "20"]) == 0
    out = capsys.readouterr().out
    assert "per-frame" in out and "tick 25 ms" in out and "STA" in out
    assert cli.main(["bandwidth", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["nodes"] == 10


def test_calibrate_keyboard_with_mock_tracker_then_check(tmp_path, monkeypatch, capsys):
    site = tmp_path / "site.json"
    site.write_text(json.dumps(SITE))
    presses = iter([False] * 60 + [True] + [False] * 1000)
    monkeypatch.setattr(cli, "_stdin_enter", lambda: next(presses))
    out = tmp_path / "cal"
    # mock://still faces true North (heading 0) without sensor bias; FLAG-N is due North of S1
    rc = cli.main(["calibrate", "--site", str(site), "--imu", "mock://still", "--station", "S1", "--marker", "FLAG-N",
                   "--out", str(out), "--node", "2", "--keyboard"])
    assert rc == 0
    cal = FieldCalibration.load(out)
    assert cal.node == 2 and cal.station == "S1" and cal.imu.declination_deg == SITE["declination_deg"]
    # the host adds the site declination to the sensor's North, so the tare removes exactly that
    assert cal.heading_offset_deg == pytest.approx(-SITE["declination_deg"], abs=0.2)
    assert (out / "imu.json").exists()
    presses2 = iter([False] * 30 + [True] + [False] * 1000)
    monkeypatch.setattr(cli, "_stdin_enter", lambda: next(presses2))
    assert cli.main(["calibrate", "--site", str(site), "--imu", "mock://still", "--out", str(out),
                     "--check", "FLAG-N", "--keyboard"]) == 0
    checked = FieldCalibration.load(out)
    assert len(checked.checks) == 1 and abs(checked.checks[0].error_deg) < 0.3
    assert "DRIFT CHECK FLAG-N" in capsys.readouterr().out


def test_calibrate_reports_bad_site(tmp_path, capsys):
    bad = tmp_path / "site.json"
    bad.write_text(json.dumps({"markers": {}}))
    assert cli.main(["calibrate", "--site", str(bad), "--imu", "mock://"]) == 2
    assert "site error" in capsys.readouterr().err


def test_headset_dry_run(tmp_path, capsys):
    prof = tmp_path / "node.json"
    prof.write_text(json.dumps({"node": 5, "callsign": "ECHO", "imu": "mock://", "site": None,
                                "cal_dir": str(tmp_path), "log_dir": None, "calibrate": "never"}))
    assert cli.main(["headset", "--profile", str(prof), "--url", "ws://10.0.0.1:8765", "--dry-run"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["urls"] == ["ws://10.0.0.1:8765"] and "--pose" in plan["argv"]
    assert plan["argv"][plan["argv"].index("--pose") + 1] == "serial:mock://"
