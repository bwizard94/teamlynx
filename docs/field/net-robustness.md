# Robustness: reconnect, failover and link loss

This covers what happens on each headset when the relay or the radio link fails, and why.
Implementation: [failover.py](../../lynx/field/failover.py),
[relay.py](../../lynx/field/relay.py) (bridge), [holdover.py](../../lynx/field/holdover.py) and
[launcher.py](../../lynx/field/launcher.py).

## Connection policy (`FailoverClient`)

`FailoverClient` is a drop-in `LynxClient`: it keeps the same world state, send API and
`BackgroundClient` hosting. Only the connection policy changes:

| Concern | Policy (default) |
|---|---|
| candidates | ordered list from discovery ([net-network.md](net-network.md)), primary first; every cycle restarts at the primary |
| dead-link detection | WebSocket keepalive every 1 s with a 2 s timeout, so a silent drop is noticed within 3 s (the library default is 40 s) |
| connect timeout | 2 s per candidate |
| backoff | after a cycle in which no candidate answered: full jitter, `U(0, min(4 s, 0.25 s × 2^k))` |
| short sessions | a session under 2 s counts as a failure, so a relay that accepts and drops (overload, 1013) cannot cause a tight loop |
| failback | while on a secondary, probe the primary every 5 s; after 2 consecutive successful handshakes, leave the secondary |
| rediscovery | when a whole cycle fails, refresh candidates off-loop (beacon, DNS, gateway) |
| own pings | recorded locally and re-published with their remaining TTL on every (re)connect, whichever relay that is |
| QoS | socket marked DSCP EF |

Link events (`connected`, `disconnected`, `connect-failed`, `failback`, `rediscovered`) go to
listeners. The launcher writes them to the headset's session log, and the AAR draws them as
link-down intervals.

Jitter matters when the router reboots: ten headsets lose it at the same instant, and without
jitter they would retry in lock-step, all at the same moments. `test_dead_link_detected_by_keepalive_and_all_down_backoff_is_jittered`
checks the bounds and that no two delays coincide.

## Secondary relay and the bridge

The secondary relay (`lynx-field relay --role secondary --upstream ws://relay.lynx:8765`, on the
leader's Jetson) holds one WebSocket to the primary and exchanges traffic in both directions.

* **Local → upstream:** only frames that came from the secondary's own clients are forwarded
  (telemetry latest-value-wins, events in FIFO order). Server-originated messages are not
  forwarded, because each relay runs its own TTL and stale sweeps.
* **Upstream → local:** frames are applied to the secondary's state and fanned out to all local
  clients. Upstream nodes are owned by a pseudo-session, so local clients cannot spoof them.
* **Loop-free:** the primary never echoes a frame back to its sender (the bridge session), and the
  secondary never forwards upstream-originated frames.
* **Clean leaves cross the bridge.** A headset that disconnects from the secondary is announced to
  the primary as a NODE_LEAVE immediately, instead of after the 5 s stale sweep.
* **Failover reclaim.** A headset that fails over to the secondary takes its node id back from
  the bridge's copy right away. The primary still holds the old session until its stale sweep, at
  most 5 s, and after that the bridge's frames for that node are accepted there too.
* **Primary lost:** the bridge reconnects with jittered backoff. The secondary keeps serving its
  local headsets, and upstream nodes age out through the normal stale sweep (the holdover on each
  headset keeps them visible as stale).
* **Sync on (re)connect:** the secondary pushes its local nodes' latest telemetry and their active
  pings, with remaining TTL, to the primary.

So whichever relay each headset is on, all headsets that can reach either relay see one squad
picture. `test_bridge_joins_headsets_split_across_relays` and
`test_bridge_failover_headset_reclaims_node_and_primary_loss` cover this end to end.

## Failure modes

| Failure | Detection | What happens | Squad picture |
|---|---|---|---|
| relay process crashes on the router | TCP reset, immediate | procd respawns it in 2 s; headsets fail over to `relay2` within one connect (< 0.5 s on a LAN) and fail back after 2 probes, about 10 s | continuous: own pings re-published, the bridge merges both sides |
| router loses power | keepalive, ≤ 3 s | the Wi-Fi is gone too. Headsets hold friendlies (below) and retry with jittered backoff. With a hot-spare router (same SSID and PSK) or a mesh, they fail over to whatever relay the network still has | holdover; re-converges within one cycle of the router returning |
| headset out of range | keepalive, ≤ 3 s; relay stale sweep at 5 s | headset: LINK DOWN alert, holdover. Others: the node goes stale after 2 s (grey, `STALE`, age label) and is held up to 120 s | the out-of-range headset keeps its own pings and re-publishes them on return |
| leader Jetson (secondary) down | bridge closes | no effect on headsets on the primary | unchanged |
| a headset reboots | TCP close | the relay announces a NODE_LEAVE; others keep the teammate as stale for 30 s (`leave_hold_s`) | the node reappears when telemetry resumes |
| relay overload (slow consumer) | the relay closes the client with 1013 | the client backs off (the short-session rule) and resyncs from the snapshot | brief |

## Headset behaviour when the link drops (`FriendlyHoldover`)

Policy:

* **Friendlies only, never contacts.** The holdover remembers teammate telemetry and nothing
  else. Detections, IFF tracks and TANGO positions are never stored, so a contact that leaves the
  camera's view is gone: no dead-enemy tracking. Pings keep their own TTL (a marked CONTACT stays
  for its TTL, as before).
* **Live** (telemetry ≤ 2 s old, the same threshold as `IffConfig.max_telemetry_age_s`): the
  track passes through unchanged.
* **Stale** (older than 2 s, link up or down):
  * position is dead-reckoned from the last fix with a velocity estimated from recent fixes
    (instantaneous velocity clamped to 7 m/s, smoothed with \(\alpha = 1 - e^{-\Delta t/0.6\,s}\)),
    for at most 3 s and 6 m, then held;
  * the track is drawn in the stale colour (white in the tape and radar; the HUD halves it to grey
    for the BFT diamond and adds `STALE`), and the callsign carries its age, e.g. `BRAVO 14s`;
  * the track keeps its *original* telemetry timestamp, so the IFF associator treats it as stale.
    It can never confirm a FRIENDLY box, but it still suppresses TANGO near the teammate's
    last-known bearing (`stale_gate_deg` 15°). **Blue-on-blue protection outlives the link.**
* **Forget** after 120 s without telemetry, or 30 s after a NODE_LEAVE(disconnect). A crashed
  headset and a clean shutdown look the same on the wire, and dropping a teammate who is still on
  the field is the worse error, so a leave only shortens the hold.
* **HUD:** while the link is down, the first alert line reads `! LINK DOWN 12s - 4 FRIENDLIES HELD`
  (replacing `! RELAY LINK DOWN`), and the telemetry block shows
  `HOLD 4 STALE  OLDEST 40s  (DR <= 3s)`.

Why dead-reckon only 3 s: a walking operator covers about 1.4 m/s, so 3 s of dead-reckoning puts
the marker about 4 m ahead of the last fix. Over that horizon a straight-line prediction is
usually better than the raw last fix. Beyond it the operator has probably turned or stopped, and a
held, ageing marker is more honest than one that keeps sliding.

`test_headset_holds_friendly_through_relay_loss` runs a real relay and two headsets, kills the
relay, and checks the held, dead-reckoned, stale-styled teammate, the alert and the IFF staleness.

## Watchdog

`lynx-headset.service` is `Type=notify` with `WatchdogSec=10`. The field hook sends `READY=1` at
the first HUD frame and `WATCHDOG=1` once a second from the render loop. If the loop hangs on the
camera, CUDA or the display, systemd kills the headset and restarts it 2 s later. The restart
re-uses the saved calibration, so there is no new tare.
