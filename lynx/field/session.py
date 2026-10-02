"""Session logging of telemetry, pings and link events (JSON Lines, optionally gzip).

One record per line, UTF-8 JSON::

    {"kind": "meta",  "t": 1790000000.1, "src": "relay", "version": 1, "host": "lynx-n01", ...}
    {"kind": "msg",   "t": 1790000000.2, "src": "relay", "msg": {<lynx.net JSON encoding>}}
    {"kind": "pose",  "t": ..., "src": "node:3", "node": 3, "callsign": "CHARLIE", "team": "blue",
                      "x": .., "y": .., "z": .., "heading": .., "pitch": .., "roll": .., "link": true}
    {"kind": "event", "t": ..., "src": "node:3", "event": "disconnected", "url": "ws://..."}

``t`` is the recording host's wall clock (``time.time()``). Squad hosts take their time from the
router's NTP server (``deploy/jetson/setup.sh``), so logs from several hosts line up; replay also
accepts a per-source offset. ``msg`` uses the protocol's JSON debug encoding, so
``lynx.net.schema.decode_json`` rebuilds the message.

Telemetry is decimated per node to ``telemetry_hz`` (default 5 Hz: ten nodes ~10 KB/s, 36 MB/h);
pings, cancels and leaves are always written. Writes are line-buffered and flushed at least every
``flush_s``, so a power cut loses at most that much; readers skip a truncated last line or gzip tail.

Standard library only (runs in the relay on a router).
"""

from __future__ import annotations

import gzip
import io
import json
import logging
import os
import socket
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, Iterator, List, Optional, Union

from lynx.net.schema import Message, Telemetry

log = logging.getLogger("lynx.session")

LOG_VERSION = 1


def session_filename(source: str, when: Optional[float] = None, compress: bool = False) -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime(time.time() if when is None else when))
    safe = source.replace(":", "-").replace("/", "-")
    return f"{safe}-{stamp}.jsonl" + (".gz" if compress else "")


class SessionRecorder:
    def __init__(self, path: Union[str, Path], source: str, *, telemetry_hz: float = 5.0,
                 meta: Optional[dict] = None, flush_s: float = 1.0, compress: Optional[bool] = None,
                 wall: Callable[[], float] = time.time, mono: Callable[[], float] = time.monotonic) -> None:
        p = Path(path)
        if p.is_dir() or str(path).endswith(os.sep):
            p.mkdir(parents=True, exist_ok=True)
            p = p / session_filename(source, wall(), bool(compress))
        p.parent.mkdir(parents=True, exist_ok=True)
        self.path = p
        self.source = source
        self.telemetry_period = 1.0 / telemetry_hz if telemetry_hz > 0 else 0.0
        self.flush_s = flush_s
        self.wall = wall
        self.mono = mono
        self._lock = threading.Lock()
        if p.suffix == ".gz":
            self._fh: io.TextIOBase = gzip.open(p, "at", encoding="utf-8")
        else:
            self._fh = open(p, "a", encoding="utf-8", buffering=1)
        self._last_tel: Dict[int, float] = {}
        self._last_flush = mono()
        self.records = 0
        self.closed = False
        self._write({"kind": "meta", "version": LOG_VERSION, "host": socket.gethostname(), **(meta or {})})

    def _write(self, rec: dict) -> None:
        rec.setdefault("t", self.wall())
        rec.setdefault("src", self.source)
        line = json.dumps(rec, separators=(",", ":"), allow_nan=False)
        with self._lock:
            if self.closed:
                return
            self._fh.write(line + "\n")
            self.records += 1
            now = self.mono()
            if now - self._last_flush >= self.flush_s:
                self._fh.flush()
                self._last_flush = now

    def record_message(self, msg: Message) -> bool:
        """Log a relay message (telemetry decimated per node). Returns True if written."""
        if isinstance(msg, Telemetry) and self.telemetry_period:
            now = self.mono()
            last = self._last_tel.get(msg.node_id)
            if last is not None and now - last < self.telemetry_period:
                return False
            self._last_tel[msg.node_id] = now
        try:
            self._write({"kind": "msg", "msg": msg.to_dict()})
        except ValueError:  # non-finite float; schema validation normally prevents this
            return False
        return True

    def record_pose(self, node: int, callsign: str, team: str, x: float, y: float, z: float,
                    heading: float, pitch: float, roll: float, link: bool = True) -> bool:
        now = self.mono()
        last = self._last_tel.get(-node)
        if self.telemetry_period and last is not None and now - last < self.telemetry_period:
            return False
        self._last_tel[-node] = now
        self._write({"kind": "pose", "node": node, "callsign": callsign, "team": team, "x": round(x, 3),
                     "y": round(y, 3), "z": round(z, 3), "heading": round(heading, 2), "pitch": round(pitch, 2),
                     "roll": round(roll, 2), "link": bool(link)})
        return True

    def record_event(self, event: str, **fields) -> None:
        self._write({"kind": "event", "event": event, **fields})

    def flush(self) -> None:
        with self._lock:
            if not self.closed:
                self._fh.flush()

    def close(self) -> None:
        with self._lock:
            if self.closed:
                return
            self.closed = True
            self._fh.flush()
            self._fh.close()

    def __enter__(self) -> "SessionRecorder":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


# ---------------------------------------------------------------------------------- reading


def iter_records(path: Union[str, Path]) -> Iterator[dict]:
    """Records of one log file; tolerates a truncated tail (power loss) and corrupt lines."""
    p = Path(path)
    opener = gzip.open if p.suffix == ".gz" else open
    bad = 0
    with opener(p, "rt", encoding="utf-8", errors="replace") as fh:
        while True:
            try:
                line = fh.readline()
            except (EOFError, OSError, gzip.BadGzipFile):
                log.warning("%s: truncated compressed tail", p)
                break
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                bad += 1
                continue
            if isinstance(rec, dict) and "kind" in rec and "t" in rec:
                yield rec
    if bad:
        log.warning("%s: skipped %d unreadable line(s)", p, bad)


def expand_paths(paths: Iterable[Union[str, Path]]) -> List[Path]:
    out: List[Path] = []
    for p in map(Path, paths):
        if p.is_dir():
            out.extend(sorted(q for q in p.iterdir() if q.name.endswith((".jsonl", ".jsonl.gz"))))
        else:
            out.append(p)
    return out


@dataclass
class LoadedLog:
    records: List[dict] = field(default_factory=list)
    sources: List[str] = field(default_factory=list)
    files: List[Path] = field(default_factory=list)


def load_records(paths: Iterable[Union[str, Path]], offsets: Optional[Dict[str, float]] = None) -> LoadedLog:
    """All records of all files, time-sorted, with ``offsets[src]`` seconds added to ``t``."""
    offsets = offsets or {}
    out = LoadedLog()
    for p in expand_paths(paths):
        out.files.append(p)
        for rec in iter_records(p):
            src = str(rec.get("src", ""))
            if src and src not in out.sources:
                out.sources.append(src)
            if src in offsets:
                rec["t"] = float(rec["t"]) + offsets[src]
            out.records.append(rec)
    out.records.sort(key=lambda r: float(r["t"]))
    return out
