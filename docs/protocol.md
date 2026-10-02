# TeamLynx wire protocol v1

The implementation is in `lynx/net/schema.py`, the relay in `lynx/net/server.py`, and the client
in `lynx/net/client.py`.

## Transport

* WebSocket over the squad LAN, typically an offline travel router at `ws://<router>:8765`.
* The client picks its downlink encoding: binary by default, or `?encoding=json` for debugging.
  The relay accepts either encoding on the uplink. A text frame or a frame starting with `{` is
  treated as JSON; anything else is binary.
* Per-message deflate is disabled. Frames are 25–56 bytes, so compression only costs router CPU.

## Binary framing (little-endian)

```
header <2sBBHIQ> (18 B) | body (fixed per type) | CRC32 <I> over header+body (4 B)
```

| field | type | notes |
|---|---|---|
| magic | `2s` | `b"LX"` |
| version | `u8` | `1` |
| msg_type | `u8` | 1 TELEMETRY, 2 PING, 3 PING_CANCEL, 4 NODE_LEAVE |
| node_id | `u16` | sender. `0` is the relay |
| seq | `u32` | per-sender, wraps. Compared with RFC 1982 serial arithmetic |
| ts_us | `u64` | sender wall clock in µs since epoch. **Informational only** |

| type | body | body B | frame B |
|---|---|---|---|
| TELEMETRY | `<BB8s6f` team, flags, callsign[8], x, y, z (m ENU), heading, pitch, roll (deg) | 34 | 56 |
| PING | `<IHBB3fI` ping_id, owner, ping_type, flags, x, y, z, ttl_ms | 24 | 46 |
| PING_CANCEL | `<IHB` ping_id, owner, reason (0 owner, 1 expired, 2 replaced) | 7 | 29 |
| NODE_LEAVE | `<HB` node, reason (0 disconnect, 1 stale) | 3 | 25 |

Enums:

* Team: 0 blue, 1 green, 2 red, 3 amber.
* Ping type: 0 mark, 1 contact, 2 move, 3 danger, 4 rally.
* Telemetry flags: bit 0 rail switch held, bit 1 low battery, bit 2 IMU degraded.

Ten nodes at 20 Hz telemetry produce 10 × 20 × 56 B ≈ 11 KB/s of uplink. The relay fans this out
as about 100 KB/s of downlink, which is trivial for any 802.11n router.

## JSON debug encoding

```json
{"v":1,"type":"telemetry","node_id":2,"seq":17,"ts_us":1790000000000000,
 "team":"green","flags":0,"callsign":"BRAVO","x":15.0,"y":25.0,"z":1.7,
 "heading":225.0,"pitch":-10.0,"roll":0.0}
```

Enum fields are lower-case names; integers are also accepted on input. Unknown fields are rejected.

## Semantics

* **Clocks.** Nodes share no clock. All liveness and TTL timing uses the receiver's monotonic
  clock. `ttl_ms` means "remaining when sent". When the relay replays active pings to a late
  joiner, it rewrites `ttl_ms` to the time remaining.
* **Ping identity** is `(owner, ping_id)`. Re-sending the same id moves or refreshes the ping.
  Only the owner may create or cancel its pings: the relay checks `owner == node_id`.
* **Node-id ownership.** The first connection to use a node id owns it until it disconnects or
  goes stale. Frames from other connections claiming that id are dropped.
* **Sequence filtering.** Telemetry that is not newer than the last accepted frame from that node
  is dropped. The counter is accepted from any value after the node rebinds or has been silent for
  2 s, which covers a sender restart.

## Relay behaviour

| concern | policy (defaults) |
|---|---|
| rebroadcast | to every client except the sender |
| stale nodes | no telemetry for 5 s → evict, broadcast NODE_LEAVE(stale) |
| disconnect | broadcast NODE_LEAVE(disconnect) for the nodes that connection owned |
| ping TTL | expiry → broadcast PING_CANCEL(expired). TTL 0 becomes the default 60 s. TTL is clamped to 600 s |
| ping cap | 8 active per owner. The oldest is evicted with PING_CANCEL(replaced) |
| pings outlive owner | yes, until TTL. A marked contact stays useful after the marker drops off |
| inbound rate | token bucket per node: telemetry 60 Hz with burst 20, pings 2 Hz with burst 5. Excess is dropped |
| outbound telemetry | latest value wins per (client, node), so slow clients never build a backlog |
| outbound events | reliable FIFO per client. On overflow (512) the client is closed with 1013 and resyncs from the snapshot on reconnect |
| late join | snapshot of the latest telemetry and active pings (with remaining TTL) |
| bad frames | ignored. After 50 consecutive bad frames the client is closed with 1007 |
| keepalive | WebSocket ping every 5 s, 10 s timeout |

## Client behaviour

* Reconnects with exponential backoff, from 0.25 s up to 5 s.
* Records its own pings locally and **re-publishes them with their remaining TTL after a
  reconnect**. The relay keeps state only in RAM, so a power-cycled router is rebuilt from the
  headsets within one reconnect.
* Expires pings and evicts nodes silent for 6 s on its own, so the HUD stays correct if the relay
  disappears.
* `BackgroundClient` hosts the asyncio client on a daemon thread for synchronous render loops.
