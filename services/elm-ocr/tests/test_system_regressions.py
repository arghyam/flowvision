"""Regressions for the bugs found by the live system test (2026-10-01), one test per confirmed bug (ids in brackets)."""
import base64
import http.server
import importlib.util
import json
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from app_factory import create_app
from conf.config import ServiceSettings, check_production
from conftest import answer, make_jpeg
from service.api.guards import DailyBudget
from service.vision import ModelError, ReaderSettings
from service.vision.images import ImageError, fetch_image_url
from service.vision.openai_client import OpenAIVisionClient

BASE = "/meter-reader/v1"


def b64(data):
    return base64.b64encode(data).decode()


@pytest.fixture
def app_with(config, make_reader, db_url, monkeypatch):
    def _make(*answers, env=None, api_key="", **reader_overrides):
        for k, v in (env or {}).items():
            monkeypatch.setenv(k, v)
        reader = make_reader(*(answers or (answer("00197127"),)), **reader_overrides)
        return TestClient(create_app(config=config, reader=reader, db_url=db_url, service_api_key=api_key),
                          raise_server_exceptions=False)
    return _make


class _Host:
    """A small image host: /p?photoId=N serves photo N; /trickle sends a photo one byte at a time."""

    def __init__(self):
        host = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path.startswith("/trickle"):
                    data = make_jpeg((9, 9, 9))
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    for i in range(len(data)):
                        self.wfile.write(data[i:i + 1])
                        self.wfile.flush()
                        time.sleep(0.05)
                    return
                n = int(self.path.split("photoId=")[-1].split("&")[0]) if "photoId=" in self.path else 1
                data = make_jpeg((10 * n % 255, 20, 30))
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"


@pytest.fixture
def image_host():
    h = _Host()
    yield h
    h.server.shutdown()


# ---------- wrong readings ----------
def test_a_corrected_reading_is_never_reused_for_the_same_photo(app_with, jpeg_bytes, another_jpeg):    # [RO-1]
    c = app_with()
    first = c.post(f"{BASE}/extract-reading", json={"id": "R1-a", "imageBase64": b64(jpeg_bytes), "consumerId": "W",
                                                    "capturedAt": "2026-09-25T10:00:00+05:30"}).json()
    c.post(f"{BASE}/feedback", json={"correlationId": first["result"]["correlationId"],
                                     "data": {"accurate": False, "actual": "0019172.7"}})
    calls = c.app.state.service.reader.client.calls
    again = c.post(f"{BASE}/extract-reading", json={"id": "R1-b", "imageBase64": b64(jpeg_bytes), "consumerId": "W",
                                                    "capturedAt": "2026-09-26T10:00:00+05:30"})
    # not read again (the same photo would be misread the same way) and never accepted: a person looks  [round 2]
    assert again.headers["Duplicate-Of"] == "R1-a" and c.app.state.service.reader.client.calls == calls
    result = again.json()["result"]
    assert result["status"] == "NEEDS_REVIEW" and result["data"]["action"] == "REVIEW"
    assert [r["code"] for r in result["data"]["reasons"]] == ["CORRECTED_BEFORE"]
    assert result["correlationId"] != first["result"]["correlationId"]
    # and a reused copy never displaces the history: the person's correction is still the trusted previous reading
    store = c.app.state.service.metadata_store
    prev = store.last_trusted("W", before=datetime(2026, 9, 25, 12, tzinfo=timezone.utc))
    assert str(prev.value) == "19172.7"


def test_reused_duplicates_are_not_new_history(app_with, jpeg_bytes):                                       # [RO-1]
    c = app_with()
    c.post(f"{BASE}/extract-reading", json={"id": "R1-c", "imageBase64": b64(jpeg_bytes), "consumerId": "V",
                                          "capturedAt": "2026-09-25T10:00:00+05:30"})
    dup = c.post(f"{BASE}/extract-reading", json={"id": "R1-d", "imageBase64": b64(jpeg_bytes), "consumerId": "V",
                                                  "capturedAt": "2026-09-29T10:00:00+05:30"})
    assert dup.headers["Duplicate-Of"] == "R1-c"
    row = c.get(f"{BASE}/readings/R1-d").json()["result"]
    assert row["duplicate_of"] == "R1-c"
    prev = c.app.state.service.metadata_store.last_trusted("V", before=datetime(2026, 9, 30, tzinfo=timezone.utc))
    assert prev.captured_at < datetime(2026, 9, 26, tzinfo=timezone.utc)      # the original photo's time, not the copy's


def test_tod_with_a_zone_number_is_the_wrong_screen(app_with, jpeg_bytes):                                  # [RO-2]
    for label in ("TOD1", "TOD2 kWh", "ToD1", "TZ1 kWh"):
        a = answer("0019712.7")
        a["screen_label"] = label
        r = app_with(a).post(f"{BASE}/extract-reading", json={"imageBase64": b64(jpeg_bytes)}).json()["result"]
        assert r["status"] == "UNCLEAR" and r["data"]["reasons"][0]["code"] == "WRONG_SCREEN", label


def test_a_tariff_label_wins_over_unreadable(app_with, jpeg_bytes):                                         # [RO-4]
    a = answer("0019?12.7", status="UNREADABLE")
    a["screen_label"] = "T2 kVAh"
    r = app_with(a).post(f"{BASE}/extract-reading", json={"imageBase64": b64(jpeg_bytes)}).json()["result"]
    assert r["data"]["reasons"][0]["code"] == "WRONG_SCREEN" and "kWh" in r["data"]["retakeHint"]


def test_booleans_are_not_readings(app_with, jpeg_bytes):                                                   # [EP-2]
    c = app_with()
    r = c.post(f"{BASE}/extract-reading", json={"id": "R2-a", "imageBase64": b64(jpeg_bytes), "consumerId": "B",
                                                "previousReading": {"value": True}})
    assert r.status_code == 400 and r.json()["error"]["errorCode"] == "ERROR_06"
    ok = c.post(f"{BASE}/extract-reading", json={"id": "R2-b", "imageBase64": b64(jpeg_bytes), "consumerId": "B"}).json()
    fb = c.post(f"{BASE}/feedback", json={"correlationId": ok["result"]["correlationId"],
                                          "data": {"accurate": False, "actual": True}})
    assert fb.status_code == 400 and c.get(f"{BASE}/readings/R2-b").json()["result"]["actual_reading"] is None


# ---------- money ----------
def test_a_database_outage_after_the_paid_call_keeps_the_reading(app_with, jpeg_bytes, monkeypatch):      # [REL-1]
    c = app_with()
    store = c.app.state.service.metadata_store

    def down(*a, **k):
        raise OperationalError("SELECT", {}, Exception("connection refused"))
    monkeypatch.setattr(store, "last_trusted", down)
    r = c.post(f"{BASE}/extract-reading", json={"id": "R3-a", "imageBase64": b64(jpeg_bytes), "consumerId": "H"})
    body = r.json()["result"]
    assert r.status_code == 200 and body["status"] == "NEEDS_REVIEW"
    assert body["data"]["reasons"][0]["code"] == "HISTORY_UNAVAILABLE" and body["data"]["meterReading"] == "0019712.7"
    assert c.get(f"{BASE}/readings/R3-a").json()["result"]["cost_usd"] > 0


def test_a_malformed_model_answer_is_a_billed_model_error(app_with, jpeg_bytes):                           # [RO-3]
    bad = answer("0019712.7")
    bad["digits"] = "not a list"
    c = app_with(bad)
    r = c.post(f"{BASE}/extract-reading", json={"id": "R4-a", "imageBase64": b64(jpeg_bytes)})
    assert r.status_code == 502 and r.json()["error"]["errorCode"] == "ERROR_04"
    assert c.get(f"{BASE}/readings/R4-a").json()["result"]["cost_usd"] > 0     # paid for: counted


class _OpenAIBody:
    def __init__(self, body: bytes):
        fake = self
        self.calls = 0

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                fake.calls += 1
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1"


def test_an_unreadable_openai_body_is_not_sent_again():                                                    # [REL-2]
    fake = _OpenAIBody(b"<html>gateway</html>")
    try:
        client = OpenAIVisionClient(ReaderSettings(openai_api_key="k", openai_base_url=fake.url))
        with pytest.raises(ModelError) as e:
            client.read(b"img")
        assert e.value.maybe_billed and fake.calls == 1
    finally:
        fake.server.shutdown()


def test_a_trickled_download_stops_at_its_time_limit(image_host):                                         # [REL-3]
    t0 = time.monotonic()
    with pytest.raises(ImageError, match="timed out"):
        fetch_image_url(f"{image_host.base}/trickle", 10**7, timeout=1.0, allow_private=True)
    assert time.monotonic() - t0 < 3


# ---------- ids and records ----------
def test_rejected_requests_keep_the_callers_id_and_are_stored(app_with, jpeg_bytes):                      # [EP-1]
    c = app_with()
    r = c.post(f"{BASE}/extract-reading", json={"id": "R5-a", "imageBase64": b64(jpeg_bytes), "capturedAt": "notadate"})
    assert r.status_code == 400 and r.json()["id"] == "R5-a"
    row = c.get(f"{BASE}/readings/R5-a").json()["result"]
    assert row["meter_reading_status"] == "ERROR" and "imageBase64" not in row["request_json"]
    up = c.post(f"{BASE}/extract-reading/upload", data={"id": "R5-b", "metadata": "notjson"},
                files={"image": ("m.jpg", jpeg_bytes, "image/jpeg")})
    assert up.status_code == 400 and up.json()["id"] == "R5-b"
    assert c.get(f"{BASE}/readings/R5-b").json()["result"]["meter_reading_status"] == "ERROR"
    # a refusal never overwrites an id that already has a result
    ok = c.post(f"{BASE}/extract-reading", json={"id": "R5-c", "imageBase64": b64(jpeg_bytes)})
    assert ok.status_code == 200
    c.post(f"{BASE}/extract-reading", json={"id": "R5-c", "imageBase64": b64(jpeg_bytes), "capturedAt": "bad"})
    assert c.get(f"{BASE}/readings/R5-c").json()["result"]["meter_reading_status"] == "SUCCESS"


def test_busy_reply_keeps_the_callers_id(app_with, jpeg_bytes):                                           # [REL-4]
    c = app_with(env={"METER_MAX_CONCURRENT_READS": "1"})
    c.app.state.admission["in"] = c.app.state.admission["limit"]
    r = c.post(f"{BASE}/extract-reading", json={"id": "R6-a", "imageBase64": b64(jpeg_bytes)})
    assert r.status_code == 503 and r.json()["id"] == "R6-a"


def test_same_id_for_another_photo_by_query_is_refused(app_with, image_host):                             # [REL-6]
    c = app_with(env={"METER_ALLOW_PRIVATE_IMAGE_URLS": "true"})
    first = c.post(f"{BASE}/extract-reading", json={"id": "R7-a", "imageURL": f"{image_host.base}/p?photoId=1"})
    assert first.status_code == 200
    other = c.post(f"{BASE}/extract-reading", json={"id": "R7-a", "imageURL": f"{image_host.base}/p?photoId=2"})
    assert other.status_code == 409 and other.json()["error"]["errorCode"] == "ERROR_15"
    # the same photo with a freshly signed link is a retry, not a different photo
    resigned = c.post(f"{BASE}/extract-reading", json={
        "id": "R7-a", "imageURL": f"{image_host.base}/p?photoId=1&X-Amz-Signature=abc&X-Amz-Date=20261001T000000Z"})
    assert resigned.status_code == 200 and resigned.headers["Idempotent-Replay"] == "true"
    stored = c.get(f"{BASE}/readings/R7-a").json()["result"]["image_url"]
    assert stored == f"{image_host.base}/p"                 # stored and logged without its query string [round 2]


def test_same_photo_by_upload_then_base64_is_a_replay(app_with, jpeg_bytes):                               # [REL-7]
    c = app_with()
    up = c.post(f"{BASE}/extract-reading/upload", data={"id": "R8-a", "consumerId": "C7"},
                files={"image": ("m.jpg", jpeg_bytes, "image/jpeg")})
    assert up.status_code == 200
    again = c.post(f"{BASE}/extract-reading", json={"id": "R8-a", "consumerId": "C7", "imageBase64": b64(jpeg_bytes)})
    assert again.status_code == 200 and again.headers["Idempotent-Replay"] == "true"


def test_readings_use_the_envelope(app_with, jpeg_bytes):                                                  # [EP-5]
    c = app_with()
    c.post(f"{BASE}/extract-reading", json={"id": "R9-a", "imageBase64": b64(jpeg_bytes)})
    body = c.get(f"{BASE}/readings/R9-a").json()
    assert body["responseCode"] == "OK" and body["statusCode"] == 200 and body["result"]["request_id"] == "R9-a"


def test_an_empty_image_is_an_invalid_image(app_with):                                                     # [EP-6]
    r = app_with().post(f"{BASE}/extract-reading", json={"imageBase64": ""})
    assert r.status_code == 400 and r.json()["error"]["errorCode"] == "ERR_001"


def test_consumer_ids_with_a_slash(app_with):                                                              # [EP-7]
    c = app_with()
    assert c.put(f"{BASE}/meters/APDCL/123", json={"meterTypeId": "lt_er300"}).status_code == 200
    assert c.get(f"{BASE}/meters/APDCL/123").json()["result"]["consumerId"] == "APDCL/123"


def test_database_outage_on_other_endpoints_is_503(app_with, monkeypatch):                                 # [EP-3]
    c = app_with()

    def down(*a, **k):
        raise OperationalError("SELECT", {}, Exception("connection refused"))
    monkeypatch.setattr(c.app.state.service.metadata_store, "get_profile", down)
    r = c.get(f"{BASE}/meters/X1")
    assert r.status_code == 503 and r.json()["error"]["errorCode"] == "ERROR_13" and r.json()["error"]["retryable"]


def test_a_link_with_spaces_is_downloaded_and_a_broken_one_is_not_retryable(image_host):                 # [TD-2]
    assert fetch_image_url(f"{image_host.base}/p a th/photo 1.jpg?photoId=1", 10**7, allow_private=True)
    with pytest.raises(ImageError) as e:
        fetch_image_url("http://127.0.0.1:99999/x.jpg", 10**7, allow_private=True)
    assert not e.value.retryable


def test_run_py_serves_photos_with_spaces_in_their_names(tmp_path, jpeg_bytes):                            # [TD-1]
    spec = importlib.util.spec_from_file_location("run_client", Path(__file__).resolve().parents[1] / "scripts" / "run.py")
    run_client = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(run_client)
    photo = tmp_path / "WhatsApp Image 2026-09-30 at 10.15.00.jpeg"
    photo.write_bytes(jpeg_bytes)
    url, server = run_client.serve_once(photo)
    try:
        assert " " not in url and fetch_image_url(url, 10**7, allow_private=True) == jpeg_bytes
    finally:
        server.shutdown()


# ---------- production settings and budget ----------
def test_empty_production_settings_count_as_missing(monkeypatch):                                         # [PROD-PG-3]
    monkeypatch.setenv("METER_IMAGE_DIR", "")
    monkeypatch.setenv("METER_IMAGE_URL_ALLOWED_HOSTS", "  ")
    problems = check_production(ServiceSettings(api_keys=("k3Y-for-the-central-system-0001",), budget_configured=True),
                                "postgresql://x", None)
    assert any("METER_IMAGE_DIR" in p for p in problems) and any("ALLOWED_HOSTS" in p for p in problems)


def test_budget_asks_the_database_every_time():                                                            # [PROD-PG-4]
    asked = []

    def reserve(request_id, owner_ts, amount, since, limit):
        asked.append((request_id, amount, since, limit))
        return len(asked) < 3, 0.0, 0.0
    budget = DailyBudget(1.0, reserve)
    now = datetime.now(timezone.utc)
    assert budget.reserve("a", now, 0.03)[0] and budget.reserve("b", now, 0.03)[0]
    assert budget.reserve("c", now, 0.03) == (False, False)      # refused, but not used up: nothing settled yet
    assert len(asked) == 3                      # no cache: every paid read asks the database (all instances share it)
    assert asked[0][2].utcoffset() == timedelta(hours=5, minutes=30) and asked[0][2].hour == 0   # India-time day


# ---------- PostgreSQL ----------
needs_postgres = pytest.mark.skipif(not __import__("os").getenv("METER_TEST_DB_URL", "").startswith("postgresql"),
                                    reason="needs METER_TEST_DB_URL=postgresql+psycopg2://...")


@needs_postgres
def test_concurrent_first_settings_for_a_consumer(store):                                                  # [PROD-PG-1]
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(8) as ex:
        results = list(ex.map(lambda i: store.set_profile("RACE-1", decimals=1, digits=8), range(8)))
    assert all(r.consumer_id == "RACE-1" for r in results)
