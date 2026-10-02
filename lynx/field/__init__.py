"""Phase 4 squad deployment: field network, robustness, datum calibration, logging and replay.

Modules (imported lazily; the relay path needs only the standard library and ``websockets`` so it
runs on an OpenWrt travel router without numpy):

* :mod:`lynx.field.relay`        field relay: tick-batched downlink, DSCP, beacon, logging, bridge
* :mod:`lynx.field.discovery`    UDP beacon + mDNS relay discovery, candidate URL ordering
* :mod:`lynx.field.failover`     multi-relay client: jittered backoff, failover and failback
* :mod:`lynx.field.holdover`     link-loss behaviour: dead-reckoned, stale-styled friendlies
* :mod:`lynx.field.session`      session logging (JSONL / JSONL.gz) and reading
* :mod:`lynx.field.replay`       top-down after-action review renderer
* :mod:`lynx.field.loadtest`     N simulated headsets against a relay: latency, drops, bandwidth
* :mod:`lynx.field.bandwidth`    analytic byte-rate and 802.11 airtime model
* :mod:`lynx.field.netconfig`    OpenWrt (UCI) router setup generator from a squad roster
* :mod:`lynx.field.geodesy`      WGS84 geodetic <-> ECEF <-> local ENU, NMEA and UBX helpers
* :mod:`lynx.field.gnss`         u-blox NMEA reader, mock receiver, position averaging
* :mod:`lynx.field.site`         staging-area site file (datum, stations, bearing markers)
* :mod:`lynx.field.calibrate`    datum calibration (position + heading tare), resection, drift
* :mod:`lynx.field.pose_source`  ``gnss:`` headset pose source (IMU attitude + GNSS position)
* :mod:`lynx.field.launcher`     ``lynx-field headset``: profile-driven field launcher
* :mod:`lynx.field.cli`          ``lynx-field`` command line

Docs: ``docs/field/net-*.md``.
"""
