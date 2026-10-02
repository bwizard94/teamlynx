import asyncio
import os
import socket
import sys
import threading
import time

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from lynx.field.relay import FieldRelay, FieldRelayConfig  # noqa: E402
from lynx.net.server import RelayConfig  # noqa: E402


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def free_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FieldRelayThread:
    """A real :class:`FieldRelay` on 127.0.0.1 on its own event loop; restartable on the same port."""

    def __init__(self, port: int = 0, field: FieldRelayConfig | None = None, **config) -> None:
        self.port = port or free_port()
        self.field = field or FieldRelayConfig(beacon=False)
        self.config = config
        self.server: FieldRelay | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> "FieldRelayThread":
        ready = threading.Event()
        self.loop = asyncio.new_event_loop()
        self.server = FieldRelay(RelayConfig(host="127.0.0.1", port=self.port, **self.config), self.field)

        def run():
            asyncio.set_event_loop(self.loop)
            self.loop.run_until_complete(self.server.start())
            ready.set()
            self.loop.run_forever()

        self._thread = threading.Thread(target=run, name=f"field-relay-{self.port}", daemon=True)
        self._thread.start()
        if not ready.wait(5.0):
            raise RuntimeError("relay did not start")
        return self

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}"

    def call(self, fn, *args):
        """Run ``fn(*args)`` on the relay loop and return its result."""
        async def wrapper():
            return fn(*args)
        return asyncio.run_coroutine_threadsafe(wrapper(), self.loop).result(5.0)

    def stop(self) -> None:
        if self.loop is None:
            return
        asyncio.run_coroutine_threadsafe(self.server.stop(), self.loop).result(10.0)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(5.0)
        self.loop.close()
        self.loop = None


def wait_for(pred, timeout: float = 5.0, step: float = 0.02) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(step)
    return pred()


@pytest.fixture
def field_relay():
    r = FieldRelayThread(sweep_interval_s=0.05).start()
    yield r
    r.stop()
