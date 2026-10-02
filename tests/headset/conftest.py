import asyncio
import os
import sys
import threading

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

pytest.importorskip("cv2", reason="install the [vision] or [vision-headless] extra")
pytest.importorskip("scipy", reason="install the [vision] or [vision-headless] extra")

from lynx.net.server import RelayConfig, RelayServer  # noqa: E402


class RelayThread:
    """A real relay on 127.0.0.1 (ephemeral port) on its own event loop, for synchronous tests."""

    def __init__(self, **config) -> None:
        self.server = RelayServer(RelayConfig(host="127.0.0.1", port=0, **config))
        self.loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, name="relay", daemon=True)

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self.server.start())
        self._ready.set()
        self.loop.run_forever()

    def start(self) -> "RelayThread":
        self._thread.start()
        if not self._ready.wait(5.0):
            raise RuntimeError("relay did not start")
        return self

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.server.port}"

    def stop(self) -> None:
        asyncio.run_coroutine_threadsafe(self.server.stop(), self.loop).result(5.0)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(5.0)
        self.loop.close()


@pytest.fixture
def relay():
    r = RelayThread(sweep_interval_s=0.05).start()
    yield r
    r.stop()
