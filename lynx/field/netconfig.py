"""OpenWrt squad-router configuration from a roster (``lynx-field netconfig``).

Input: a squad roster JSON (``deploy/squad.example.json``). Output directory:

* ``router/lynx-router-setup.sh``  idempotent UCI script, run once on the router over SSH. It
  keeps the radio's hardware sections (``path``/``type`` differ per model) and rewrites only what
  TeamLynx needs: one offline WPA3-SAE AP on the 2.4 GHz radio (PMF required, 802.11b rates off,
  no low-ACK disassociation), the LAN subnet, a static DHCP lease per node, ``relay.lynx`` /
  ``relay2.lynx`` DNS names, NTP server for the squad, firewall rules and the relay service;
* ``nodes/lynx-nNN.json``          node profile per headset for ``lynx-field headset``;
* ``hosts.md``                     the address plan.

Address plan: router ``.1``, node *N* at ``.(10 + N)`` (N = 1..99), DHCP pool for anything else at
``.100``-``.149``. The secondary relay name points at the leader's node address.
"""

from __future__ import annotations

import ipaddress
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Union

from lynx.net.schema import Team

MAC_RE = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")
CALLSIGN_RE = re.compile(r"^[A-Z0-9-]{1,8}$")
BANDS = {"2g": ("2g", "11g"), "5g": ("5g", "11a")}


class RosterError(ValueError):
    pass


@dataclass(frozen=True)
class RosterNode:
    id: int
    callsign: str
    mac: str
    team: str = "blue"
    role: str = "operator"  # operator | leader
    imu: str = "/dev/lynx-imu"
    gnss: Optional[str] = None


@dataclass
class Roster:
    squad: str
    ssid: str
    psk: str
    nodes: List[RosterNode]
    country: str = "US"
    band: str = "2g"
    channel: int = 6
    htmode: str = "HT20"
    subnet: str = "192.168.8.0/24"
    router_name: str = "lynx-router"
    relay_port: int = 8765
    secondary_relay: Optional[int] = None  # node id hosting relay2 (default: the leader)
    disable_other_radios: bool = True
    txpower_dbm: Optional[int] = None
    site: str = "/etc/lynx/site.json"

    # -- validation ------------------------------------------------------------------------------

    @classmethod
    def from_dict(cls, d: dict) -> "Roster":
        known = {"squad", "ssid", "psk", "nodes", "country", "band", "channel", "htmode", "subnet", "router_name",
                 "relay_port", "secondary_relay", "disable_other_radios", "txpower_dbm", "site"}
        unknown = set(d) - known
        if unknown:
            raise RosterError(f"unknown roster fields {sorted(unknown)}")
        try:
            nodes = [RosterNode(int(n["id"]), str(n["callsign"]).upper(), str(n["mac"]).lower(),
                                str(n.get("team", "blue")).lower(), str(n.get("role", "operator")),
                                str(n.get("imu", "/dev/lynx-imu")), n.get("gnss"))
                     for n in d["nodes"]]
            r = cls(squad=str(d["squad"]), ssid=str(d["ssid"]), psk=str(d["psk"]), nodes=nodes,
                    **{k: d[k] for k in known - {"squad", "ssid", "psk", "nodes"} if k in d})
        except KeyError as exc:
            raise RosterError(f"roster is missing {exc}") from exc
        r.validate()
        return r

    @classmethod
    def load(cls, path: Union[str, Path]) -> "Roster":
        return cls.from_dict(json.loads(Path(path).read_text()))

    @property
    def network(self) -> ipaddress.IPv4Network:
        return ipaddress.IPv4Network(self.subnet, strict=True)

    @property
    def router_ip(self) -> str:
        return str(self.network.network_address + 1)

    def node_ip(self, node_id: int) -> str:
        return str(self.network.network_address + 10 + node_id)

    @property
    def leader(self) -> Optional[RosterNode]:
        if self.secondary_relay is not None:
            return next((n for n in self.nodes if n.id == self.secondary_relay), None)
        return next((n for n in self.nodes if n.role == "leader"), None)

    def validate(self) -> None:
        if not 1 <= len(self.ssid.encode()) <= 32:
            raise RosterError("ssid must be 1..32 bytes")
        if not 12 <= len(self.psk) <= 63 or not self.psk.isprintable():
            raise RosterError("psk must be 12..63 printable characters (WPA3-SAE passphrase)")
        if "'" in self.psk or "'" in self.ssid:
            raise RosterError("ssid/psk must not contain single quotes")
        if self.band not in BANDS:
            raise RosterError(f"band must be one of {sorted(BANDS)}")
        if not re.fullmatch(r"[A-Z]{2}", self.country):
            raise RosterError("country must be a 2-letter ISO code (sets legal channels/power)")
        if self.network.prefixlen > 24:
            raise RosterError("subnet must be /24 or larger")
        if not self.nodes or len(self.nodes) > 16:
            raise RosterError("roster needs 1..16 nodes")
        ids, macs, calls = set(), set(), set()
        for n in self.nodes:
            if not 1 <= n.id <= 89:
                raise RosterError(f"node id {n.id} outside 1..89")
            if not MAC_RE.match(n.mac):
                raise RosterError(f"node {n.id}: bad MAC {n.mac!r} (aa:bb:cc:dd:ee:ff)")
            if not CALLSIGN_RE.match(n.callsign):
                raise RosterError(f"node {n.id}: callsign must be 1..8 of A-Z 0-9 -")
            if n.team.upper() not in Team.__members__:
                raise RosterError(f"node {n.id}: team must be one of {[t.name.lower() for t in Team]}")
            if n.role not in ("operator", "leader"):
                raise RosterError(f"node {n.id}: role must be operator or leader")
            for value, seen, what in ((n.id, ids, "id"), (n.mac, macs, "MAC"), (n.callsign, calls, "callsign")):
                if value in seen:
                    raise RosterError(f"duplicate node {what} {value}")
                seen.add(value)
        if self.secondary_relay is not None and self.secondary_relay not in ids:
            raise RosterError("secondary_relay must be a node id from the roster")


# ---------------------------------------------------------------------------------- rendering


def _q(value: object) -> str:
    return "'" + str(value).replace("'", "'\\''") + "'"


def router_script(r: Roster) -> str:
    net = r.network
    want, legacy = BANDS[r.band]
    lines = [
        "#!/bin/sh",
        "# TeamLynx squad router setup - generated by `lynx-field netconfig`; re-generate, do not edit.",
        f"# Squad {r.squad}: SSID {r.ssid}, {len(r.nodes)} nodes, LAN {net}.",
        "# Run on the router (OpenWrt 21.02+ or GL.iNet 4.x):  sh lynx-router-setup.sh",
        "set -eu",
        "",
        "# --- system: hostname; time follows the squad leader (the router has no RTC) -----------",
        f"uci set system.@system[0].hostname={_q(r.router_name)}",
        "uci set system.ntp.enabled='1'",
        "uci set system.ntp.enable_server='1'",
        "uci -q delete system.ntp.server || true",
    ]
    if r.leader is not None:
        lines.append(f"uci add_list system.ntp.server={_q(r.node_ip(r.leader.id))}")
    lines += [
        "",
        "# --- LAN -------------------------------------------------------------------------------",
        f"uci set network.lan.ipaddr={_q(r.router_ip)}",
        f"uci set network.lan.netmask={_q(net.netmask)}",
        "",
        "# --- radio: keep the hardware section, set channel/regdomain/rates ----------------------",
        "RADIO=''",
        "OTHERS=''",
        "for r in $(uci -q show wireless | sed -n 's/^wireless\\.\\([^.=]*\\)=wifi-device$/\\1/p'); do",
        "  band=$(uci -q get wireless.$r.band || true)",
        "  hw=$(uci -q get wireless.$r.hwmode || true)",
        f"  if [ -z \"$RADIO\" ] && {{ [ \"$band\" = {_q(want)} ] || [ \"$hw\" = {_q(legacy)} ]; }}; then",
        "    RADIO=$r",
        "  else",
        "    OTHERS=\"$OTHERS $r\"",
        "  fi",
        "done",
        f"[ -n \"$RADIO\" ] || {{ echo 'no {r.band} radio found' >&2; exit 1; }}",
        "uci set wireless.$RADIO.disabled='0'",
        f"uci set wireless.$RADIO.channel={_q(r.channel)}",
        f"uci set wireless.$RADIO.htmode={_q(r.htmode)}",
        f"uci set wireless.$RADIO.country={_q(r.country)}",
        "uci set wireless.$RADIO.legacy_rates='0'",
        "uci set wireless.$RADIO.cell_density='1'",
    ]
    if r.txpower_dbm is not None:
        lines.append(f"uci set wireless.$RADIO.txpower={_q(r.txpower_dbm)}")
    if r.disable_other_radios:
        lines += ["for r in $OTHERS; do uci set wireless.$r.disabled='1'; done"]
    lines += [
        "",
        "# --- one offline AP: WPA3-SAE, PMF required, WMM on, no low-ACK kick -----------------",
        "while uci -q delete wireless.@wifi-iface[0]; do :; done",
        "uci set wireless.lynx=wifi-iface",
        "uci set wireless.lynx.device=\"$RADIO\"",
        "uci set wireless.lynx.network='lan'",
        "uci set wireless.lynx.mode='ap'",
        f"uci set wireless.lynx.ssid={_q(r.ssid)}",
        "uci set wireless.lynx.encryption='sae'",
        f"uci set wireless.lynx.key={_q(r.psk)}",
        "uci set wireless.lynx.ieee80211w='2'",
        "uci set wireless.lynx.wmm='1'",
        "uci set wireless.lynx.isolate='0'",
        "uci set wireless.lynx.disassoc_low_ack='0'",
        "uci set wireless.lynx.dtim_period='1'",
        "uci set wireless.lynx.max_inactivity='30'",
        "",
        "# --- DHCP: static lease per node, DNS names for the relays --------------------------",
        "uci set dhcp.lan.start='100'",
        "uci set dhcp.lan.limit='50'",
        "uci set dhcp.lan.leasetime='12h'",
        "uci set dhcp.@dnsmasq[0].domain='lynx'",
        "uci set dhcp.@dnsmasq[0].local='/lynx/'",
        "uci -q delete dhcp.@dnsmasq[0].address || true",
        f"uci add_list dhcp.@dnsmasq[0].address={_q(f'/relay.lynx/{r.router_ip}')}",
    ]
    leader = r.leader
    if leader is not None:
        lines.append(f"uci add_list dhcp.@dnsmasq[0].address={_q(f'/relay2.lynx/{r.node_ip(leader.id)}')}")
    lines.append("while uci -q delete dhcp.@host[0]; do :; done")
    for n in sorted(r.nodes, key=lambda n: n.id):
        lines += [
            "uci add dhcp host >/dev/null",
            f"uci set dhcp.@host[-1].name={_q(f'lynx-n{n.id:02d}')}",
            f"uci set dhcp.@host[-1].mac={_q(n.mac)}",
            f"uci set dhcp.@host[-1].ip={_q(r.node_ip(n.id))}",
            "uci set dhcp.@host[-1].dns='1'",
        ]
    lines += [
        "",
        "# --- firewall: relay, beacon, mDNS from the LAN ---------------------------------------",
    ]
    for name, proto, port in (("lynx_relay", "tcp", r.relay_port), ("lynx_beacon", "udp", 8766),
                              ("lynx_mdns", "udp", 5353)):
        lines += [
            f"uci -q delete firewall.{name} || true",
            f"uci set firewall.{name}=rule",
            f"uci set firewall.{name}.name={_q(name.replace('_', '-'))}",
            f"uci set firewall.{name}.src='lan'",
            f"uci set firewall.{name}.proto={_q(proto)}",
            f"uci set firewall.{name}.dest_port={_q(port)}",
            f"uci set firewall.{name}.target='ACCEPT'",
        ]
    lines += [
        "",
        "uci commit",
        "# --- services ---------------------------------------------------------------------------",
        "[ -x /etc/init.d/lynx-relay ] && /etc/init.d/lynx-relay enable || echo 'lynx-relay missing: install-relay.sh'",
        "[ -x /etc/init.d/umdns ] && /etc/init.d/umdns enable || true",
        "/etc/init.d/sysntpd restart || true",
        "/etc/init.d/dnsmasq restart",
        "/etc/init.d/firewall reload",
        "/etc/init.d/network reload",
        "wifi reload",
        "[ -x /etc/init.d/lynx-relay ] && /etc/init.d/lynx-relay restart || true",
        f"echo 'TeamLynx router ready: SSID {r.ssid} on '\"$RADIO\"', relay ws://{r.router_ip}:{r.relay_port}'",
        "",
    ]
    return "\n".join(lines)


def node_profile(r: Roster, n: RosterNode) -> dict:
    return {
        "node": n.id,
        "callsign": n.callsign,
        "team": n.team,
        "role": n.role,
        "squad": r.squad,
        "relay_urls": [],
        "discovery": True,
        "imu": n.imu,
        "gnss": n.gnss,
        "site": r.site,
        "cal_dir": "/var/lib/lynx",
        "log_dir": "/var/log/lynx",
        "calibrate": "if-stale",
        "calibrate_max_age_h": 12.0,
        "station": None,
        "markers": [],
        "camera": {"source": "0", "hfov": 78.0, "width": 1280, "height": 720},
        "telemetry_hz": 20.0,
        "imu_report": "rv",
        "headset_args": [],
        "relay_role": "secondary" if r.leader is not None and r.leader.id == n.id else None,
    }


def hosts_table(r: Roster) -> str:
    lines = [f"# {r.squad} address plan ({r.subnet})", "", "| host | address | role | MAC |", "|---|---|---|---|",
             f"| {r.router_name} (relay.lynx) | {r.router_ip} | router + primary relay | - |"]
    for n in sorted(r.nodes, key=lambda n: n.id):
        name = f"lynx-n{n.id:02d}"
        if r.leader is not None and n.id == r.leader.id:
            name += " (relay2.lynx)"
        lines.append(f"| {name} {n.callsign} | {r.node_ip(n.id)} | {n.role} | {n.mac} |")
    lines.append("")
    return "\n".join(lines)


def render(r: Roster) -> Dict[str, str]:
    out = {"router/lynx-router-setup.sh": router_script(r), "hosts.md": hosts_table(r)}
    for n in r.nodes:
        out[f"nodes/lynx-n{n.id:02d}.json"] = json.dumps(node_profile(r, n), indent=2) + "\n"
    return out


def write(r: Roster, out_dir: Union[str, Path]) -> List[Path]:
    root = Path(out_dir)
    written = []
    for rel, text in render(r).items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        if rel.endswith(".sh"):
            p.chmod(0o755)
        written.append(p)
    return written
