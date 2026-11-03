import json
import os
import sys
from pathlib import Path

import pytest

SERVICE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE / "src"))
# Config() reads src/conf/config.yaml relative to the working directory unless CONFIG_PATH is set.
os.environ.setdefault("CONFIG_PATH", str(SERVICE / "src" / "conf" / "config.yaml"))

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture(autouse=True)
def switches_start_at_their_defaults(monkeypatch):
    """A switch exported in the shell must not change what the tests see."""
    for name in ("FLOWVISION_COUNTER_ROW_ONLY", "FLOWVISION_ORIENTATION"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(scope="session")
def annex_production_detections():
    """The digit boxes the production model found on the 100 hand-checked Annexure photos, with the true reading.

    Each item: photo (number), size, truth (read by a person), production_ai (what production returned), quality_good,
    dets: [{pts (4x2 polygon), cls (digit), conf}]. Detections only: no photos.
    """
    with open(FIXTURES / "annex_production_detections.json", encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture(scope="session")
def annex_counter_drums():
    """The counter drums of the hand-checked Annexure photos: photo, truth, reading, pts, confs and p_red (the drum
    colour model's P(red)) per drum, in reading order. Detections and scores only: no photos."""
    with open(FIXTURES / "annex_counter_drums.json", encoding="utf-8") as f:
        return json.load(f)
