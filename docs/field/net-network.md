# Squad network (Phase 4)

This document covers the squad's offline Wi-Fi network, where the relay runs, how headsets find
it, and how much air the traffic needs. It covers [`lynx/field`](../../lynx/field/) and
[`deploy/`](../../deploy/). Related docs:

* [net-robustness.md](net-robustness.md): reconnect, failover, link-loss behaviour
* [net-mesh.md](net-mesh.md): the optional 802.11s / batman-adv variant
* [net-calibration.md](net-calibration.md): the staging datum
* [net-deployment.md](net-deployment.md): Jetson setup, services, logging and replay
* [net-loadtest.md](net-loadtest.md): measured numbers and how to reproduce them

The wire protocol itself does not change. It is still [protocol.md](../protocol.md) v1.

## Topology

```
                    2.4 GHz, ch 6, HT20, WPA3-SAE (PMF required), SSID LYNX-SQUAD, offline
   ┌─────────────────────────────────────────────────────────────────────────────────────┐
   │  GL.iNet travel router  192.168.8.1  relay.lynx                                     │
   │    AP + DHCP (static lease per node) + DNS (dnsmasq) + umdns                        │
   │    lynx-field relay --role primary     ws :8765, beacon udp :8766                   │
   └──────────────┬──────────────────────────────────────────────┬───────────────────────┘
                  │ Wi-Fi                                        │ Wi-Fi
   ┌──────────────┴───────────────┐            ┌─────────────────┴─────────────┐
   │ ALPHA (leader) 192.168.8.11  │            │ BRAVO .. JULIET  .12 .. .20   │
   │ relay2.lynx                  │            │ lynx-field headset            │
   │ lynx-field headset           │            │   FailoverClient:             │
   │ lynx-field relay --role      │◄── bridge ─┤   relay.lynx → relay2.lynx    │
   │   secondary --upstream       │  (1 WS)    │                               │
   │   ws://relay.lynx:8765 --log │            └───────────────────────────────┘
   │ chrony: squad time reference │
   └──────────────────────────────┘
```

* **The primary relay runs on the router.** Frames between a headset and the relay then cross
  the air once. A relay on a Wi-Fi client would add a second hop (station → AP → relay station)
  for every frame and double the airtime (see the table below).
* **The secondary relay runs on the squad leader's Jetson.** It bridges to the primary, so
  headsets split across the two relays during a partial outage still share one picture. It also
  writes the session log, since the router has no storage.
* **The leader's Jetson is the squad's time source** (chrony `local stratum 8`). The router has
  no RTC and no internet in the field, so it follows the leader too. This keeps log timestamps
  from different hosts aligned.

## Router

Any OpenWrt 21.02+ router with Python 3 works. GL.iNet 4.x firmware is OpenWrt-based and works
too. Recommended options:

| Model | SoC / RAM | Notes |
|---|---|---|
| GL-MT3000 (Beryl AX) | 2× A53 1.3 GHz / 512 MB | USB-C 5 V/3 A from a power bank, about 4–6 W. Recommended |
| GL-AXT1800 (Slate AX) | 4× A53 1.2 GHz / 512 MB | Same, bigger |
| GL-MT300N-V2 (Mango) | MIPS 580 MHz / 128 MB | Too small for Python. Use the Jetson relay with this one |

A 20 000 mAh (74 Wh) USB-PD bank runs an MT3000 for roughly 12 h.

### Radio settings (generated, see below)

| Setting | Value | Why |
|---|---|---|
| band / channel / width | 2.4 GHz, ch 1/6/11, HT20 | 2.4 GHz gets through foliage and bodies better than 5 GHz, and 20 MHz gives the best sensitivity |
| `country` | your ISO code | sets the legal channels and transmit power |
| `legacy_rates 0`, `cell_density 1` | 802.11b off | beacons, ACKs and broadcasts go at ≥ 6 Mbps instead of 1 Mbps |
| `encryption sae`, `ieee80211w 2` | WPA3-SAE, PMF required | offline does not mean open: another team's devices stay out |
| `disassoc_low_ack 0` | off | the AP keeps a station that misses ACKs at range instead of kicking it |
| `dtim_period 1`, `wmm 1` | | low latency for power-saving clients; WMM enables the DSCP → access-category mapping |
| `isolate 0` | | headsets must reach the leader's secondary relay |
| clients: `wifi.powersave = 2` | off | power save adds up to a beacon interval (about 100 ms) of downlink latency |

### Addressing

| Host | Address | DNS |
|---|---|---|
| router (primary relay) | `.1` | `relay.lynx` |
| node *N* (1..89) | `.(10 + N)` (static DHCP lease by MAC) | `lynx-nNN.lynx` |
| squad leader | its node address | `relay2.lynx` |
| anything else | DHCP pool `.100`–`.149` | |

### Generate and install

The roster ([`deploy/squad.example.json`](../../deploy/squad.example.json)) lists the SSID,
passphrase, regulatory country, channel, subnet and each node's id, callsign, team, MAC, role and
sensor ports:

```bash
lynx-field netconfig deploy/squad.example.json --out build/net
#   build/net/router/lynx-router-setup.sh   UCI script for the router (idempotent)
#   build/net/nodes/lynx-nNN.json           node profile per headset (lynx-field headset)
#   build/net/hosts.md                      address plan
deploy/openwrt/install-relay.sh root@192.168.8.1 build/net/router/lynx-router-setup.sh
```

[`deploy/example/`](../../deploy/example/) is the committed output for the example roster.
`test_committed_example_is_up_to_date` fails if the generator and the example drift apart.

`install-relay.sh` needs internet on the router once, for `opkg install python3 python3-pip` and
`pip3 install websockets`. It then:

1. copies `lynx.net` + `lynx.field` to `/opt/lynx`. The relay path imports neither numpy nor
   OpenCV, and a test enforces this;
2. installs the procd service ([`/etc/init.d/lynx-relay`](../../deploy/openwrt/files/etc/init.d/lynx-relay)),
   which respawns forever 2 s apart and reads its settings from
   [`/etc/config/lynx`](../../deploy/openwrt/files/etc/config/lynx);
3. installs the umdns service file;
4. self-tests the import and starts the relay;
5. applies the generated network script last, because it reloads Wi-Fi.

The setup script keeps each radio's hardware section (`path` and `type` differ per model) and only
rewrites what TeamLynx needs. It deletes all existing `wifi-iface` and static `dhcp host`
sections, so the router must be dedicated to the squad.

## Discovery: no configuration on the headsets

`resolve_relay_urls` ([discovery.py](../../lynx/field/discovery.py)) orders the candidates. The
failover client tries them in this order:

1. explicit `--url` values or profile `relay_urls`;
2. **UDP beacon** on port 8766. The headset broadcasts a query (subnet broadcast on every up
   interface, plus 255.255.255.255) and every relay *process* answers by unicast. Relays also
   announce once a second. Each answer carries the role (primary or secondary) and a priority, and
   the relay's address is the datagram's source address;
3. **mDNS** `_lynx-relay._tcp` (via python-zeroconf if installed). OpenWrt announces it via umdns
   and the leader Jetson via avahi (or `lynx-field relay --mdns`);
4. DNS names from the router's dnsmasq: `relay.lynx`, then `relay2.lynx`;
5. the default gateway on port 8765.

```
query        {"svc":"lynx-relay","q":1,"squad":"LYNX"}
announcement {"svc":"lynx-relay","v":1,"port":8765,"role":"primary","prio":0,"squad":"LYNX","id":"lynx-router","clients":7}
```

The beacon answers only if the relay process is running, which is a stronger signal than a
resolving name. The `squad` filter keeps two TeamLynx squads on one field apart even if they share
an L2 segment. `lynx-field discover` lists what answers.

## Bandwidth and airtime

### Bytes

A binary telemetry frame is 56 B (JSON ≈ 187 B). With N nodes at rate R:

| | per node | relay total, N = 10, R = 20 Hz |
|---|---|---|
| uplink | R × 56 B = 1.1 kB/s | 11.2 kB/s in |
| downlink | (N−1) R × 56 B = 10.1 kB/s | 100.8 kB/s out |

The load test measures 11.5 kB/s in and 98.7 kB/s out
([net-loadtest.md](net-loadtest.md)), which matches. The extra uplink bytes are pings and cancels.

### Airtime matters more than bytes

Each small frame costs a full 802.11 channel access, whatever its size. The model
([bandwidth.py](../../lynx/field/bandwidth.py), `lynx-field bandwidth`) charges every
transmission

$$
T = \mathrm{AIFS} + \overline{\mathrm{backoff}} + T_{\mathrm{preamble}}
  + \Big\lceil \frac{16 + 8L + 6}{N_{\mathrm{dbps}}} \Big\rceil \cdot 4\,\mu s + \mathrm{SIFS} + T_{\mathrm{ACK}}
$$

The terms are:

* \(L\) is the MPDU length: payload + WebSocket header (6 B up, 2 B down) + TCP/IPv4 with
  timestamps (52 B) + 802.11 QoS header, LLC/SNAP, CCMP and FCS (46 B);
* the HT-mixed preamble is 36 µs;
* the ACK is 14 B at 6 or 24 Mbps;
* \(\mathrm{AIFS} = 16 + \mathrm{AIFSN} \cdot 9\,\mu s\) and
  \(\overline{\mathrm{backoff}} = \mathrm{CW_{min}}/2 \cdot 9\,\mu s\), using the AC_VI
  parameters because traffic is marked DSCP EF (see below).

The traffic counted is uplink telemetry, downlink segments, TCP pure ACKs (delayed ACK, ratio 0.5)
and 1 Hz WebSocket keepalives, plus 10 beacons/s. Retries are ignored, so treat the result as a
floor.

10 nodes × 20 Hz:

```
relay  downlink     PHY    relay in  relay out  frames/s  airtime
AP     per-frame    mcs0     11.2kB    100.8kB      3020   101.2%
AP     per-frame    mcs3     11.2kB    100.8kB      3020    58.0%
AP     per-frame    mcs7     11.2kB    100.8kB      3020    50.0%
AP     tick 25 ms   mcs0     11.2kB    100.8kB       920    41.3%
AP     tick 25 ms   mcs3     11.2kB    100.8kB       920    20.3%
AP     tick 25 ms   mcs7     11.2kB    100.8kB       920    16.7%
AP     tick 50 ms   mcs3     11.2kB    100.8kB       620    15.0%
STA    per-frame    mcs3     11.2kB    100.8kB      6040   115.6%
STA    tick 25 ms   mcs3     11.2kB    100.8kB      1840    40.3%
```

The conclusions behind the defaults:

* **With one downlink frame per message, 10 × 20 Hz saturates the channel at MCS0** (6.5 Mbps,
  which is what a link at 100+ m through trees negotiates) and uses about 58 % at MCS3. Phase 1's
  "trivial for any 802.11n router" holds for bytes but not for airtime.
* **Tick batching** (`FieldRelay`, default 25 ms) flushes each client's latest-value-wins
  telemetry on a fixed tick and writes the batch under Linux `TCP_CORK`, so it leaves as one
  segment and one 802.11 frame. Downlink frames per headset drop from 180/s to ≤ 40/s and airtime
  from 58 % to 20 % at MCS3. The cost is +12.5 ms mean and +25 ms worst-case telemetry latency.
  Pings, cancels and leaves are never delayed: an event wakes the sender at once and carries any
  pending telemetry with it.
* The measured host TCP segment rate on loopback falls from 2923/s to 867/s with a 25 ms tick
  ([net-loadtest.md](net-loadtest.md)), which confirms the batching.
* **A relay on a Wi-Fi client doubles airtime.** This is why the primary is on the router and the
  Jetson relay is the secondary.
* A 50 ms tick saves a little more air but coalesces about 0.2 % of poses at 20 Hz, because the
  tick equals the telemetry period. 25 ms is the default; use `--tick-ms 50` only for a link that
  is struggling.
* Keep the predicted airtime under about 50 % to leave headroom for retries and the PiP video
  feed. At 16 nodes or 30 Hz, check `lynx-field bandwidth --nodes 16 --rate 30`.

### QoS marking

Every relay and client socket is marked **DSCP EF** (46). Linux and hostapd map
`DSCP >> 3 = 5` to user priority 5, which is AC_VI: AIFSN 2 and CWmin 7 instead of best effort's
3 and 15. That cuts the mean channel-access time (AIFS + mean backoff) per frame from about 110 µs to 66 µs and puts
squad traffic ahead of bulk transfers such as log copies and PiP video on the same radio.

## Relay CPU

On the 4-core Xeon test host, the relay process uses 5–7 % of one core for 10 × 20 Hz
([net-loadtest.md](net-loadtest.md)). A Cortex-A53 at 1.3 GHz runs CPython roughly 6–8× slower,
so expect about 30–50 % of one MT3000 core. That fits, but it is an estimate: measure on the
router before game day with `lynx-field loadtest --url ws://192.168.8.1:8765` from a laptop on
the squad SSID, watching `top` on the router. If the router cannot keep up, swap roles: run the
primary on the leader Jetson and accept the 2× airtime, or drop telemetry to 10 Hz.
