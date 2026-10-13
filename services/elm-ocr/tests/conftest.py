import io
import os
import sys
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from conf.config import Config  # noqa: E402
from service.api.database import DatabaseService  # noqa: E402
from service.api.metadata_service import MetadataStore  # noqa: E402
from service.vision import MeterReader, ReaderSettings  # noqa: E402


def answer(display="0019712.7", *, status="READ", screen="cumulative_energy", unit="kWh", conf=0.98,
           make="HPL", model_code="PPSM 01", faceplate=(), illegible=(), count_certain=True, dot_visible=None,
           best_guess=None, decimal_guess=None):
    """A model answer in the prompt's schema, built from the display text (chars in `illegible` are marked unclear)."""
    chars = list(display or "")
    return {
        "screen_type": screen, "unit": unit,
        "digits": [{"char": c, "legible": i not in illegible} for i, c in enumerate(chars)],
        "digit_count_certain": count_certain,
        "decimal_point_visible": ("." in chars) if dot_visible is None else dot_visible,
        "display_text": display, "meter_make": make, "meter_model_code": model_code,
        "faceplate_text": list(faceplate), "status": status, "confidence": conf, "reason": "test",
        "best_guess": best_guess if best_guess is not None else display, "decimal_places_guess": decimal_guess,
    }


class FakeClient:
    """Returns scripted answers in order (cycling), like OpenAIVisionClient.read."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = 0

    def read(self, image_bytes, mime="image/jpeg", deadline=None):
        a = self.answers[self.calls % len(self.answers)]
        self.calls += 1
        return {"answer": a, "usage": {"prompt_tokens": 2500, "completion_tokens": 300}, "model": "fake-sol", "seconds": 0.01}


def make_jpeg(color=(120, 160, 90)):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), color).save(buf, format="JPEG")
    return buf.getvalue()


@pytest.fixture
def jpeg_bytes():
    return make_jpeg()


@pytest.fixture
def another_jpeg():
    """A different photo (different bytes): not answered from an earlier result as a duplicate."""
    return make_jpeg((30, 60, 200))


@pytest.fixture
def config(tmp_path):
    """The shipped config.yaml with log files and saved photos redirected to a temp folder."""
    data = yaml.safe_load((SRC / "conf" / "config.yaml").read_text(encoding="utf-8"))
    for logger in data["logs"].values():
        logger["path"] = str(tmp_path / "logs" / Path(logger["path"]).name)
    data["storage"]["images_dir"] = str(tmp_path / "images")
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return Config(path=str(path))


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch):
    """Tests never see the local .env or METER_* settings of the machine they run on."""
    import conf.config
    monkeypatch.setattr(conf.config, "load_dotenv", lambda *a, **k: None)
    for name in list(os.environ):
        if name.startswith("METER_") and name not in ("METER_TEST_DB_URL", "METER_DB_NULL_POOL"):
            monkeypatch.delenv(name)


@pytest.fixture
def db_url():
    """In-memory SQLite, or the PostgreSQL database named by METER_TEST_DB_URL (CI), emptied for each test."""
    url = os.getenv("METER_TEST_DB_URL")
    if not url:
        return "sqlite://"
    from service.api.database import metadata
    engine = DatabaseService(url).engine
    with engine.begin() as conn:
        for table in reversed(metadata.sorted_tables):
            conn.execute(table.delete())
    engine.dispose()
    return url


@pytest.fixture
def store(config, db_url):
    return MetadataStore(config, DatabaseService(db_url))


@pytest.fixture
def make_reader():
    def _make(*answers, store=None, **overrides):
        settings = replace(ReaderSettings(openai_api_key="test"), **overrides)
        return MeterReader(settings, client=FakeClient(*answers), store=store)
    return _make
