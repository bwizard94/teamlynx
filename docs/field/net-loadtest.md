# Relay load test

`lynx-field loadtest` simulates N headsets against a relay and reports one-way latency
percentiles, telemetry delivery, ping delivery, bytes and relay CPU
([loadtest.py](../../lynx/field/loadtest.py)).

```bash
lynx-field loadtest --spawn --tick-ms 0 --tick-ms 25 --tick-ms 50 --nodes 10 --rate 20 --duration 10
lynx-field loadtest --url ws://192.168.8.1:8765 --nodes 10 --duration 30        # the real router, from a laptop
lynx-field loadtest ... --json results.json
```

Each simulated headset is a raw WebSocket client, so the generator stays light and adds little
latency of its own. It sends 20 Hz telemetry with a random phase while walking a 20 m circle, and
a ping every 3 s that it cancels 2 s later.

* **Latency** is measured from the sender's `ts_us` to receive time on the same host clock, so it
  is true relay latency including the tick.
* **Delivery** is received / ((N − 1) × sent). Any shortfall is relay coalescing (latest-value-wins:
  the receiver got a newer pose instead) or loss.
* **Pings** must reach every other headset: zero loss.
* **Generator lag** is the p99 oversleep of a 10 ms timer in the generator. If it is large, the
  generator is the bottleneck, not the relay.
* With `--spawn`, the relay's CPU (from `/proc/<pid>`) and the host's TCP segment rate are
  reported too. On loopback the segment count includes both directions and the ACKs.

## Results

Host: 4-core Intel Xeon VM, loopback, relay in its own process (`--spawn`), 10 s window after 1 s
of warm-up.

### 10 headsets × 20 Hz, binary

| Downlink | Delivery | Latency p50 / p90 / p99 / max (ms) | Pings (loss) | Ping p99 | Relay CPU | Host TCP segs/s |
|---|---|---|---|---|---|---|
| per frame (`--tick-ms 0`) | 18000/18000 (100 %) | 0.7 / 0.9 / 1.1 / 1.8 | 306/306 (0) | 1.2 ms | 6.6 % | 2923 |
| **tick 25 ms (default)** | 18000/18000 (100 %) | 15.9 / 23.7 / 26.0 / 26.6 | 306/306 (0) | 1.5 ms | 5.2 % | **867** |
| tick 50 ms | 17959/18000 (99.77 %) | 29.2 / 46.2 / 48.4 / 49.4 | 306/306 (0) | 1.6 ms | 5.0 % | 590 |

The relay takes 11.5 kB/s in and sends 98.7 kB/s out (1780 frames/s), matching the analytic
11.2 / 100.8 kB/s in [net-network.md](net-network.md).

### Encoding and headroom

| Run | Delivery | Latency p50 / p99 (ms) | Pings (loss) | Relay app bytes in / out | Relay CPU | TCP segs/s |
|---|---|---|---|---|---|---|
| 10 × 20 Hz JSON, tick 25 ms | 100 % | 13.9 / 25.9 | 306/306 (0) | 46.2 / 398.3 kB/s | 8.6 % | 875 |
| 16 × 20 Hz binary, per frame | 100 % (48000) | 0.8 / 1.5 | 810/810 (0) | 18.3 / 263.3 kB/s | 12.7 % | 7560 |
| 16 × 20 Hz binary, tick 25 ms | 100 % (48000) | 9.6 / 25.6 | 810/810 (0) | 18.3 / 263.3 kB/s | 9.5 % | 1463 |

### Reading the numbers

* **Batching works as designed.** The 25 ms tick cuts TCP segments by 3.4× at 10 nodes and 5.2×
  at 16, with no loss of delivery, at a cost of tick/2 ≈ 12.5 ms mean and ≤ 25 ms added
  telemetry latency. Pings stay at about 1 ms because events bypass the tick. On the air, segment
  count is what costs airtime ([net-network.md](net-network.md)).
* **A 50 ms tick equals the telemetry period** (20 Hz), so jitter occasionally puts two frames
  from one sender in one tick and the older one is dropped (0.23 %). It saves only a few more
  segments, so 25 ms is the default.
* **The binary encoding is worth keeping.** JSON is 4× the bytes and about 65 % more relay CPU.
* **CPU:** about 5 % of a Xeon core at 10 nodes. On a Cortex-A53 router, budget 6–8× that and
  measure it (`--url` against the router, `top` on the router).
* **Loopback is the floor.** Over Wi-Fi, add the airtime-dependent channel-access and
  retransmission delay. At 10 × 20 Hz with a 25 ms tick, the model predicts about 20 % airtime at
  MCS3, which leaves room for retries. Run the `--url` form against the router at the field to see
  real numbers.

## In the test suite

`tests/field/test_loadtest_bandwidth.py` runs a reduced version: 10 headsets × 20 Hz for a 2 s
window, against an in-process `FieldRelay` with both the 25 ms tick and per-frame downlink. It
asserts:

* ≥ 98 % telemetry delivery;
* zero ping loss;
* p99 latency < 250 ms (generous for shared CI runners);
* application bytes within 15 % of the model.

It also runs a short `--spawn` run through the CLI path and the closed-form airtime checks.
