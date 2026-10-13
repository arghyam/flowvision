import base64
from datetime import datetime, timezone
import uuid

import pytest
from fastapi.testclient import TestClient

from app_factory import create_app
from conf.config import load_settings
from conftest import answer

BASE = "/flowvision/v1/elm"


@pytest.fixture
def make_client(config, make_reader, db_url):
    def _make(*answers, api_key="", **overrides):
        app = create_app(config=config, reader=make_reader(*answers, **overrides), db_url=db_url,
                         service_api_key=api_key)
        return TestClient(app)
    return _make


def b64(data):
    return base64.b64encode(data).decode()


def test_success_follows_flowvision_envelope(make_client, jpeg_bytes):
    c = make_client(answer("00197127"))
    rid = str(uuid.uuid4())
    body = c.post(f"{BASE}/extract-reading", json={"id": rid, "imageBase64": b64(jpeg_bytes), "consumerId": "C9"}).json()
    assert body["id"] == rid and body["responseCode"] == "OK" and body["statusCode"] == 200
    assert body["result"]["status"] == "SUCCESS"
    data = body["result"]["data"]
    assert data["meterReading"] == "0019712.7" and data["unit"] == "kWh" and data["meterBrand"] == "HPL PPSM 01"
    stored = c.get(f"{BASE}/readings/{rid}").json()["result"]          # written by the background task
    assert stored["meter_reading_status"] == "SUCCESS" and stored["consumer_id"] == "C9"


def test_status_mapping(make_client, jpeg_bytes, monkeypatch):
    monkeypatch.setenv("METER_DUPLICATE_PHOTO_HOURS", "0")      # three fake models, one photo: read it each time
    nomatch = make_client(answer(None, status="NO_DISPLAY", screen="not_visible"))
    r = nomatch.post(f"{BASE}/extract-reading", json={"imageBase64": b64(jpeg_bytes)}).json()["result"]
    assert r["status"] == "NOMETER" and r["data"]["action"] == "RETAKE" and r["data"]["retakeHint"]
    assert r["data"]["meterReading"] == "No meter display detected in the image"   # nothing to read
    # unsure of one digit: still returns what the model read, flagged as a retake
    unclear = make_client(answer("001?712.7", status="UNREADABLE", illegible={3}, best_guess="0019712.7"))
    r = unclear.post(f"{BASE}/extract-reading", json={"imageBase64": b64(jpeg_bytes)}).json()["result"]
    assert r["status"] == "UNCLEAR" and r["data"]["action"] == "RETAKE"
    assert r["data"]["meterReading"] == "0019712.7" and r["data"]["uncertainDigits"] == [4]   # the reading is always shown
    assert r["data"]["decimalSource"] == "registry"
    review = make_client(answer("008365.8"))          # HPL meter with a dropped digit
    r = review.post(f"{BASE}/extract-reading", json={"imageBase64": b64(jpeg_bytes)}).json()["result"]
    assert r["status"] == "NEEDS_REVIEW" and r["data"]["reasons"][0]["code"] == "DIGIT_COUNT_MISMATCH"
    assert r["data"]["meterReading"] == "008365.8" and r["data"]["action"] == "REVIEW"   # what the model read


def test_error_responses(make_client, jpeg_bytes):
    c = make_client(answer("00197127"))
    both = c.post(f"{BASE}/extract-reading", json={"imageURL": "https://x/y.jpg", "imageBase64": "AAAA"}).json()
    assert both["responseCode"] == "ERROR" and both["statusCode"] == 400 and both["error"]["errorCode"] == "ERROR_06"
    notimg = c.post(f"{BASE}/extract-reading", json={"imageBase64": b64(b"hello world")}).json()
    assert notimg["statusCode"] == 415 and notimg["error"]["errorCode"] == "ERR_002"
    local = c.post(f"{BASE}/extract-reading", json={"imageURL": "http://127.0.0.1/meter.jpg"}).json()
    assert local["statusCode"] == 400 and local["error"]["errorCode"] == "ERROR_05"


def test_model_failure_is_llm_error(make_client, jpeg_bytes):
    from service.vision import ModelError

    class Broken:
        def read(self, *a, **k):
            raise ModelError("OpenAI HTTP 500: boom")
    c = make_client(answer("00197127"))
    c.app.state.service.reader.client = Broken()
    body = c.post(f"{BASE}/extract-reading", json={"imageBase64": b64(jpeg_bytes)}).json()
    assert body["responseCode"] == "ERROR" and body["statusCode"] == 502 and body["error"]["errorCode"] == "ERROR_04"


def test_upload_with_previous_reading(make_client, jpeg_bytes):
    c = make_client(answer("00197127"))
    r = c.post(f"{BASE}/extract-reading/upload", files={"image": ("m.jpg", jpeg_bytes, "image/jpeg")},
               data={"previousValue": "20000", "previousUnit": "kWh"}).json()
    assert r["result"]["status"] == "NEEDS_REVIEW" and r["result"]["data"]["reasons"][0]["code"] == "BELOW_PREVIOUS"
    bad = c.post(f"{BASE}/extract-reading/upload", files={"image": ("m.txt", b"not an image", "text/plain")}).json()
    assert bad["responseCode"] == "ERROR" and bad["statusCode"] == 415


def test_meter_settings_feedback_and_history(make_client, jpeg_bytes, another_jpeg):
    c = make_client(answer("00018103", make=None, model_code=None))
    assert c.put(f"{BASE}/meters/C5", json={"meterTypeId": "nope"}).json()["error"]["errorCode"] == "ERROR_07"
    assert c.get(f"{BASE}/meters/C5").json()["statusCode"] == 404
    put = c.put(f"{BASE}/meters/C5", json={"meterTypeId": "lt_er300", "billingUnit": "kwh"}).json()
    assert put["result"]["meterTypeId"] == "lt_er300" and put["result"]["billingUnit"] == "kWh"
    res = c.post(f"{BASE}/extract-reading", json={"imageBase64": b64(jpeg_bytes), "consumerId": "C5",
                                                  "capturedAt": "2026-09-01T10:00:00Z"}).json()["result"]
    assert res["status"] == "SUCCESS" and res["data"]["meterReading"] == "00018103"
    fb = c.post(f"{BASE}/feedback", json={"correlationId": res["correlationId"],
                                          "data": {"accurate": False, "actual": "00018130"}}).json()
    assert fb["result"]["status"] == "SUBMITTED"
    assert c.get(f"{BASE}/readings/{res['correlationId']}").json()["result"]["actual_reading"] == "00018130"
    # the corrected value is now the previous reading: 00018103 is below it
    again = c.post(f"{BASE}/extract-reading", json={"imageBase64": b64(another_jpeg), "consumerId": "C5",
                                                    "capturedAt": "2026-09-15T10:00:00Z"}).json()["result"]
    assert again["status"] == "NEEDS_REVIEW" and again["data"]["reasons"][0]["code"] == "BELOW_PREVIOUS"
    types = c.get(f"{BASE}/meter-types").json()["result"]
    assert {"hpl_ppsm01", "lt_er300", "lt_em101"} <= {t["id"] for t in types}


def test_bad_input_is_a_request_error(make_client, jpeg_bytes):
    c = make_client(answer("00197127"))
    up = c.post(f"{BASE}/extract-reading/upload", files={"image": ("m.jpg", jpeg_bytes, "image/jpeg")},
                data={"capturedAt": "yesterday"}).json()
    assert up["responseCode"] == "ERROR" and up["statusCode"] == 400 and up["error"]["errorCode"] == "ERROR_06"
    prev = c.post(f"{BASE}/extract-reading", json={"imageBase64": b64(jpeg_bytes), "previousReading": {"value": "abc"}}).json()
    assert prev["statusCode"] == 400 and prev["error"]["errorCode"] == "ERROR_06"
    fb = c.post(f"{BASE}/feedback", json={"correlationId": str(uuid.uuid4()),
                                          "data": {"accurate": False, "actual": "19,800"}}).json()
    assert fb["statusCode"] == 400 and fb["error"]["errorCode"] == "ERROR_06"


def test_photo_is_saved_and_can_be_fetched(make_client, jpeg_bytes, tmp_path):
    c = make_client(answer("00197127"))
    rid = str(uuid.uuid4())
    res = c.post(f"{BASE}/extract-reading", json={"id": rid, "imageBase64": b64(jpeg_bytes), "consumerId": "C11",
                                                  "ts": "2026-09-30T10:00:00Z"}).json()["result"]
    row = c.get(f"{BASE}/readings/{res['correlationId']}").json()["result"]
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")                   # filed by the day the server received it
    assert row["image_path"] == f"{day}/{rid}.jpg"                          # named by request, not by consumer
    assert (tmp_path / "images" / day / f"{rid}.jpg").read_bytes() == jpeg_bytes   # unchanged original
    img = c.get(f"{BASE}/readings/{rid}/image")
    assert img.status_code == 200 and img.content == jpeg_bytes and img.headers["content-type"] == "image/jpeg"
    up = c.post(f"{BASE}/extract-reading/upload", files={"image": ("m.jpg", jpeg_bytes, "image/jpeg")}).json()
    assert c.get(f"{BASE}/readings/{up['id']}/image").content == jpeg_bytes
    assert c.get(f"{BASE}/readings/nope/image").status_code == 404


def test_photos_not_saved_when_switched_off(make_client, jpeg_bytes, monkeypatch):
    monkeypatch.setenv("METER_IMAGE_DIR", "none")
    c = make_client(answer("00197127"))
    r = c.post(f"{BASE}/extract-reading", json={"imageBase64": b64(jpeg_bytes)}).json()
    assert c.get(f"{BASE}/readings/{r['id']}").json()["result"]["image_path"] is None
    assert c.get(f"{BASE}/readings/{r['id']}/image").status_code == 404


def test_storage_paths_are_under_the_service_folder(config, tmp_path):
    from conf.config import SERVICE_ROOT, database_url
    from service.api.database import DatabaseService
    assert database_url(config) == f"sqlite:///{SERVICE_ROOT / 'data' / 'meter_reader.db'}"
    # a table created before image_path existed gets the column on startup
    url = f"sqlite:///{tmp_path / 'old.db'}"
    import sqlalchemy as sa
    engine = sa.create_engine(url)
    with engine.begin() as conn:
        conn.execute(sa.text("CREATE TABLE meter_reader_extraction_data (request_id VARCHAR PRIMARY KEY)"))
    engine.dispose()
    cols = {c["name"] for c in sa.inspect(DatabaseService(url).engine).get_columns("meter_reader_extraction_data")}
    assert {"image_path", "meter_reading", "consumer_id"} <= cols


def test_api_key(make_client, jpeg_bytes):
    c = make_client(answer("00197127"), api_key="s3cret")
    payload = {"imageBase64": b64(jpeg_bytes)}
    assert c.post(f"{BASE}/extract-reading", json=payload).status_code == 401
    ok = c.post(f"{BASE}/extract-reading", json=payload, headers={"X-API-Key": "s3cret"}).json()
    assert ok["result"]["status"] == "SUCCESS"
    assert c.get("/").status_code == 200


def test_legacy_base_path_serves_the_same_api(make_client, jpeg_bytes):
    """Existing clients keep /meter-reader/v1: the same routes, key check and records as /flowvision/v1/elm."""
    legacy_base = "/meter-reader/v1"
    c = make_client(answer("00197127"), api_key="s3cret")
    key = {"X-API-Key": "s3cret"}
    payload = {"id": "legacy-1", "imageBase64": b64(jpeg_bytes)}
    assert c.post(f"{legacy_base}/extract-reading", json=payload).status_code == 401
    legacy = c.post(f"{legacy_base}/extract-reading", json=payload, headers=key)
    assert legacy.status_code == 200 and legacy.json()["result"]["status"] == "SUCCESS"
    # one store behind both paths: the same id sent to the new path replays the stored result
    replay = c.post(f"{BASE}/extract-reading", json=payload, headers=key)
    assert replay.headers.get("Idempotent-Replay") == "true" and replay.json()["result"] == legacy.json()["result"]
    assert c.get(f"{legacy_base}/readings/legacy-1", headers=key).json()["result"]["request_id"] == "legacy-1"
    assert c.get(f"{legacy_base}/meter-types", headers=key).status_code == 200


def test_load_settings(config, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(RuntimeError):
        load_settings(config)
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    s = load_settings(config)
    assert s.model == "gpt-6-sol" and s.reads == 1 and s.reasoning_effort == "high" and s.max_image_bytes == 20971520
    monkeypatch.setenv("METER_READS", "2")
    monkeypatch.setenv("METER_REASONING_EFFORT", "none")
    s = load_settings(config)
    assert s.reads == 2 and s.reasoning_effort is None
