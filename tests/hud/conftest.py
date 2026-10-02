import os
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

pytest.importorskip("cv2", reason="install the [vision] or [vision-headless] extra")
pytest.importorskip("scipy", reason="install the [vision] or [vision-headless] extra")
