"""OpenWrt config generator, the committed example output, and the deploy scripts / units."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from lynx.field.launcher import NodeProfile
from lynx.field.netconfig import Roster, RosterError, render, write

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy"
EXAMPLE = json.loads((DEPLOY / "squad.example.json").read_text())


def roster(**over):
    d = json.loads(json.dumps(EXAMPLE))
    d.update(over)
    return Roster.from_dict(d)


def test_router_script_content():
    files = render(roster())
    sh = files["router/lynx-router-setup.sh"]
    for needle in ("uci set wireless.lynx.encryption='sae'", "uci set wireless.lynx.ieee80211w='2'",
                   "uci set wireless.lynx.disassoc_low_ack='0'", "uci set wireless.$RADIO.legacy_rates='0'",
                   "uci set wireless.$RADIO.country='GB'", "uci set wireless.lynx.ssid='LYNX-SQUAD'",
                   "address='/relay.lynx/192.168.8.1'", "address='/relay2.lynx/192.168.8.11'",
                   "uci set firewall.lynx_relay.dest_port='8765'", "uci set firewall.lynx_beacon.dest_port='8766'",
                   "uci add_list system.ntp.server='192.168.8.11'", "wifi reload"):
        assert needle in sh, needle
    assert sh.count("uci add dhcp host") == 10
    assert "uci set dhcp.@host[-1].ip='192.168.8.20'" in sh and "mac='48:b0:2d:00:00:0a'" in sh
    assert "| lynx-n01 (relay2.lynx) ALPHA | 192.168.8.11 | leader |" in files["hosts.md"]


@pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX shell")
def test_router_script_is_valid_sh(tmp_path):
    write(roster(), tmp_path)
    subprocess.run(["sh", "-n", str(tmp_path / "router" / "lynx-router-setup.sh")], check=True)


def test_node_profiles_load_in_launcher(tmp_path):
    write(roster(), tmp_path)
    alpha = NodeProfile.load(str(tmp_path / "nodes" / "lynx-n01.json"))
    assert alpha.relay_role == "secondary" and alpha.gnss == "/dev/lynx-gnss"
    india = NodeProfile.load(str(tmp_path / "nodes" / "lynx-n09.json"))
    assert india.callsign == "INDIA" and india.team == "green" and india.relay_role is None


@pytest.mark.parametrize("over,match", [
    ({"psk": "short"}, "psk"),
    ({"ssid": "x" * 33}, "ssid"),
    ({"band": "6g"}, "band"),
    ({"country": "gb"}, "country"),
    ({"subnet": "192.168.8.0/25"}, "subnet"),
    ({"secondary_relay": 42}, "secondary_relay"),
    ({"bogus": 1}, "unknown"),
])
def test_roster_validation(over, match):
    with pytest.raises(RosterError, match=match):
        roster(**over)


def test_roster_node_validation():
    bad_mac = json.loads(json.dumps(EXAMPLE))
    bad_mac["nodes"][0]["mac"] = "48-b0-2d-00-00-01"
    with pytest.raises(RosterError, match="MAC"):
        Roster.from_dict(bad_mac)
    dup = json.loads(json.dumps(EXAMPLE))
    dup["nodes"][1]["callsign"] = "ALPHA"
    with pytest.raises(RosterError, match="duplicate"):
        Roster.from_dict(dup)
    team = json.loads(json.dumps(EXAMPLE))
    team["nodes"][2]["team"] = "purple"
    with pytest.raises(RosterError, match="team"):
        Roster.from_dict(team)


def test_committed_example_is_up_to_date():
    for rel, text in render(Roster.load(DEPLOY / "squad.example.json")).items():
        committed = DEPLOY / "example" / rel
        assert committed.exists(), f"{rel} missing: lynx-field netconfig deploy/squad.example.json --out deploy/example"
        assert committed.read_text() == text, f"{rel} is stale: re-run lynx-field netconfig"


def test_site_example_loads():
    from lynx.field.site import Site

    site = Site.load(DEPLOY / "lynx" / "site.example.json")
    assert site.datum is not None and len(site.markers) == 3 and "S1" in site.stations


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_jetson_setup_dry_run(tmp_path):
    script = DEPLOY / "jetson" / "setup.sh"
    subprocess.run(["bash", "-n", str(script)], check=True)
    prof = tmp_path / "node.json"
    prof.write_text((DEPLOY / "example" / "nodes" / "lynx-n01.json").read_text())
    out = subprocess.run(["bash", str(script), "--dry-run", "--profile", str(prof), "--site",
                          str(DEPLOY / "lynx" / "site.example.json"), "--ssid", "LYNX-SQUAD", "--psk",
                          "change-this-squad-passphrase", "--leader", "--user", "lynxop"],
                         check=True, capture_output=True, text=True).stdout
    for needle in ("python3 -m venv --system-site-packages /opt/lynx/venv", "wifi-sec.key-mgmt sae",
                   "wifi-sec.pmf required", "802-11-wireless.powersave 2", "write /etc/udev/rules.d/99-lynx.rules",
                   "write /etc/systemd/system/lynx-relay.service", "--upstream ws://relay.lynx:8765",
                   "systemctl enable lynx-headset.service lynx-relay.service", "local stratum 8",
                   "User=lynxop", "ExecStart=/opt/lynx/venv/bin/lynx-field headset --profile /etc/lynx/node.json"):
        assert needle in out, needle
    assert "@USER@" not in out and "@VENV@" not in out
    bad = subprocess.run(["bash", str(script), "--dry-run"], capture_output=True, text=True)
    assert bad.returncode == 2 and "--profile" in bad.stderr


def test_deploy_assets_are_consistent():
    units = DEPLOY / "systemd"
    headset = (units / "lynx-headset.service").read_text()
    assert "Type=notify" in headset and "WatchdogSec=" in headset and "@VENV@/bin/lynx-field headset" in headset
    assert "$LYNX_RELAY_ARGS" in (units / "lynx-relay.service").read_text()
    rules = (DEPLOY / "udev" / "99-lynx.rules").read_text()
    assert rules.count('SYMLINK+="lynx-imu"') == 4 and 'SYMLINK+="lynx-gnss"' in rules
    init = DEPLOY / "openwrt" / "files" / "etc" / "init.d" / "lynx-relay"
    assert init.read_text().startswith("#!/bin/sh /etc/rc.common") and "procd_set_param respawn" in init.read_text()
    assert json.loads((DEPLOY / "openwrt" / "files" / "etc" / "umdns" / "lynx-relay.json").read_text())
    if shutil.which("sh"):
        subprocess.run(["sh", "-n", str(DEPLOY / "openwrt" / "install-relay.sh")], check=True)
        subprocess.run(["sh", "-n", str(init)], check=True)


def test_netconfig_cli(tmp_path, capsys):
    from lynx.field.cli import main

    assert main(["netconfig", str(DEPLOY / "squad.example.json"), "--out", str(tmp_path)]) == 0
    assert (tmp_path / "nodes" / "lynx-n10.json").exists()
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({**EXAMPLE, "psk": "x"}))
    assert main(["netconfig", str(bad), "--out", str(tmp_path)]) == 2
    assert "roster error" in capsys.readouterr().err
