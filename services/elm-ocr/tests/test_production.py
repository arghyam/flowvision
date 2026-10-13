"""Production behaviour: auth, error envelope, idempotency, limits, health, time zones, storage, OpenAI retries."""
import base64
import dataclasses
import http.server
import io
import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app_factory import create_app
from conf.config import ServiceSettings, check_production
from conftest import FakeClient, answer
from service.vision import ModelError, ReaderSettings, previous_from
from service.vision.images import ImageError, _check_url, prepare_image
from service.vision.openai_client import OpenAIVisionClient

BASE = "/flowvision/v1/elm"


def b64(data):
    return base64.b64encode(data).decode()


@pytest.fixture
def app_with(config, make_reader, db_url, monkeypatch):
    """create_app with the given env settings and fake model answers."""
    def _make(*answers, env=None, api_key="", raise_errors=True, **reader_overrides):
        for k, v in (env or {}).items():
            monkeypatch.setenv(k, v)
        reader = make_reader(*(answers or (answer("00197127"),)), **reader_overrides)
        app = create_app(config=config, reader=reader, db_url=db_url, service_api_key=api_key)
        return TestClient(app, raise_server_exceptions=raise_errors)
    return _make


# ---------- authentication and production mode ----------
def test_production_refuses_to_start_without_its_settings(config, make_reader, monkeypatch):
    monkeypatch.setenv("METER_ENV", "production")
    with pytest.raises(RuntimeError, match="METER_SERVICE_API_KEY.*METER_DB_URL.*METER_IMAGE_DIR.*ALLOWED_HOSTS"):
        create_app(config=config, reader=make_reader(answer()), db_url="sqlite://")
    key = "k3Y-for-the-central-system-0001"
    problems = check_production(ServiceSettings(api_keys=(key,)), "postgresql+psycopg2://u:p@db/x", "s3://b/p")
    assert [p for p in problems if "IMAGE_DIR" in p or "ALLOWED_HOSTS" in p]      # both env vars still unset
    monkeypatch.setenv("METER_IMAGE_DIR", "s3://bucket/photos")
    monkeypatch.setenv("METER_IMAGE_URL_ALLOWED_HOSTS", "jalsoochak.example.org")
    ready = ServiceSettings(api_keys=(key,), budget_configured=True, allowed_hosts=("jalsoochak.example.org",))
    assert check_production(ready, "postgresql+psycopg2://u:p@db/x", "s3://b/p") == []
    # a daily cap must be chosen (a number, or "none" on purpose), and the key must not be a placeholder
    no_budget = check_production(dataclasses.replace(ready, budget_configured=False), "postgresql://x", None)
    assert any("METER_DAILY_BUDGET_USD" in p for p in no_budget)
    assert any("too short" in p for p in check_production(dataclasses.replace(ready, api_keys=("change-me",)),
                                                            "postgresql://x", None))
    # credentials or private hosts need an explicit host list
    risky = dataclasses.replace(ready, allowed_hosts=(), allow_private=True, download_headers={"Authorization": "x"})
    assert sum("ALLOWED_HOSTS to list" in p for p in check_production(risky, "postgresql://x", None)) == 2


def test_api_keys_envelope_and_rotation(app_with, jpeg_bytes, monkeypatch):
    monkeypatch.setenv("METER_SERVICE_API_KEY", "old-key, new-key")
    c = app_with(api_key=None)
    denied = c.post(f"{BASE}/extract-reading", json={"imageBase64": b64(jpeg_bytes)})
    body = denied.json()
    assert denied.status_code == 401 and body["responseCode"] == "ERROR" and body["error"]["errorCode"] == "ERROR_09"
    for key in ("old-key", "new-key"):                       # both keys work while the client switches over
        ok = c.post(f"{BASE}/extract-reading", json={"imageBase64": b64(jpeg_bytes)}, headers={"X-API-Key": key})
        assert ok.status_code == 200
    assert c.get("/health").status_code == 200 and c.get("/ready").status_code == 200   # no key needed


def test_docs_are_off_in_production_mode(app_with, monkeypatch, tmp_path):
    env = {"METER_ENV": "production", "METER_IMAGE_DIR": str(tmp_path / "img"), "METER_IMAGE_URL_ALLOWED_HOSTS": "*"}
    monkeypatch.setattr("app_factory.check_production", lambda *a: [])      # the test database is SQLite
    c = app_with(env=env, api_key="k")
    assert c.get("/docs").status_code == 404 and c.get("/openapi.json").status_code == 404


# ---------- error envelope ----------
def test_every_error_uses_the_envelope(app_with, jpeg_bytes):
    c = app_with(raise_errors=False)
    bad_date = c.post(f"{BASE}/extract-reading", json={"imageBase64": b64(jpeg_bytes), "capturedAt": "30-09-2026"})
    assert bad_date.status_code == 400 and bad_date.json()["error"]["errorCode"] == "ERROR_06"
    assert "capturedAt" in bad_date.json()["error"]["errorMsg"]
    bad_json = c.post(f"{BASE}/extract-reading", content=b"{not json", headers={"Content-Type": "application/json"})
    assert bad_json.status_code == 400 and bad_json.json()["responseCode"] == "ERROR"
    unknown = c.get(f"{BASE}/no-such-endpoint")
    assert unknown.status_code == 404 and unknown.json()["error"]["errorCode"] == "ERROR_08"
    missing = c.get(f"{BASE}/readings/nope")
    assert missing.status_code == 404 and missing.json()["error"]["errorCode"] == "ERROR_08"


def test_central_system_ids_and_numeric_fields_are_accepted(app_with, jpeg_bytes):
    c = app_with()
    r = c.post(f"{BASE}/extract-reading", json={"id": "JS-2026-09-30.000123", "imageBase64": b64(jpeg_bytes),
                                               "consumerId": 1234567, "previousReading": {"value": 19000}})
    assert r.status_code == 200 and r.json()["id"] == "JS-2026-09-30.000123"
    row = c.get(f"{BASE}/readings/JS-2026-09-30.000123").json()["result"]
    assert row["consumer_id"] == "1234567" and row["request_json"]["previousReading"]["value"] == 19000
    assert str(previous_from(19000).value) == "19000"            # not 19000.0: no invented decimal place
    bad = c.post(f"{BASE}/extract-reading", json={"id": "../../etc", "imageBase64": b64(jpeg_bytes)})
    assert bad.status_code == 400                                 # ids name the stored photo: safe characters only


def test_flowvision_http_200_mode(app_with, jpeg_bytes):
    c = app_with(env={"METER_ERRORS_AS_HTTP_200": "true"}, api_key="k")
    r = c.post(f"{BASE}/extract-reading", json={"imageBase64": b64(jpeg_bytes)})
    assert r.status_code == 200 and r.json()["statusCode"] == 401


# ---------- idempotency ----------
def test_same_id_is_read_once_and_replayed(app_with, jpeg_bytes):
    c = app_with()
    fake = c.app.state.service.reader.client
    body = {"id": "JS-1", "imageBase64": b64(jpeg_bytes), "consumerId": "C1"}
    first = c.post(f"{BASE}/extract-reading", json=body)
    again = c.post(f"{BASE}/extract-reading", json=body)
    assert fake.calls == 1                                         # the model was called (and paid) once
    assert again.json() == first.json() and again.headers["Idempotent-Replay"] == "true"
    fb = c.post(f"{BASE}/feedback", json={"correlationId": first.json()["result"]["correlationId"],
                                          "data": {"accurate": True}})
    assert fb.status_code == 200                                   # the correlationId stays valid after a replay


def test_same_id_still_in_progress_gets_409(app_with, jpeg_bytes):
    c = app_with()
    store = c.app.state.service.metadata_store
    store.claim({"request_id": "JS-2", "request_timestamp": datetime.now(timezone.utc)}, stale_after=160)
    r = c.post(f"{BASE}/extract-reading", json={"id": "JS-2", "imageBase64": b64(jpeg_bytes)})
    assert r.status_code == 409 and r.json()["error"]["retryable"] is True and r.headers["Retry-After"]
    # an abandoned claim (e.g. a worker restart) is taken over
    store.claim({"request_id": "JS-3", "request_timestamp": datetime.now(timezone.utc) - timedelta(hours=1)},
                stale_after=160)
    assert c.post(f"{BASE}/extract-reading", json={"id": "JS-3", "imageBase64": b64(jpeg_bytes)}).status_code == 200


# ---------- limits ----------
def test_daily_budget_stops_paid_calls(app_with, jpeg_bytes, another_jpeg):
    c = app_with(env={"METER_DAILY_BUDGET_USD": "0.055"})                 # room for one $0.05 reservation
    assert c.post(f"{BASE}/extract-reading", json={"imageBase64": b64(jpeg_bytes)}).status_code == 200  # costs $0.008
    r = c.post(f"{BASE}/extract-reading", json={"imageBase64": b64(another_jpeg)})
    assert r.status_code == 429 and r.json()["error"]["errorCode"] == "ERROR_11"
    assert c.app.state.service.reader.client.calls == 1


def test_busy_service_answers_503_instead_of_queueing(app_with, jpeg_bytes):
    c = app_with(env={"METER_MAX_CONCURRENT_READS": "1", "METER_BUSY_WAIT_SECONDS": "0.1"})
    slots = c.app.state.service.slots
    assert slots.acquire()                                         # another reading holds the only slot
    try:
        r = c.post(f"{BASE}/extract-reading", json={"id": "JS-4", "imageBase64": b64(jpeg_bytes)})
    finally:
        slots.release()
    assert r.status_code == 503 and r.json()["error"]["retryable"] is True and r.headers["Retry-After"] == "10"
    assert c.post(f"{BASE}/extract-reading", json={"id": "JS-4", "imageBase64": b64(jpeg_bytes)}).status_code == 200


def test_request_body_limit(app_with, jpeg_bytes):
    c = app_with(env={"METER_MAX_BODY_BYTES": "2000"})
    big = c.post(f"{BASE}/extract-reading", json={"imageBase64": "A" * 5000})
    assert big.status_code == 413 and big.json()["error"]["errorCode"] == "ERROR_003"

    def chunks():                                                 # streamed, without Content-Length
        yield b'{"imageBase64": "'
        for _ in range(10):
            yield b"A" * 500
        yield b'"}'
    streamed = c.post(f"{BASE}/extract-reading", content=chunks(), headers={"Content-Type": "application/json"})
    assert streamed.status_code == 413


def test_readiness_reports_the_database(app_with):
    c = app_with()
    assert c.get("/ready").json()["database"] == "ok"
    c.app.state.service.metadata_store.ping = lambda: (_ for _ in ()).throw(RuntimeError("db down"))
    assert c.get("/ready").status_code == 503 and c.get("/health").status_code == 200


# ---------- correctness fixes ----------
def test_time_without_zone_is_india_time(app_with, jpeg_bytes):
    c = app_with()
    c.post(f"{BASE}/extract-reading", json={"id": "JS-5", "imageBase64": b64(jpeg_bytes),
                                          "capturedAt": "2026-09-30T10:00:00", "ts": "2020-01-01T00:00:00Z"})
    row = c.get(f"{BASE}/readings/JS-5").json()["result"]
    captured = datetime.fromisoformat(row["captured_at"])
    captured = captured if captured.tzinfo else captured.replace(tzinfo=timezone.utc)
    assert captured == datetime(2026, 9, 30, 4, 30, tzinfo=timezone.utc)     # 10:00 IST = 04:30 UTC
    assert not row["request_timestamp"].startswith("2020")        # server time, not the caller's `ts`


def test_partial_consumer_settings_keep_the_digit_check(app_with, jpeg_bytes):
    hpl_dropped = answer("0083658", model_code="PPSM 01", conf=0.97)          # HPL shows 8 digits; one dropped
    c = app_with(hpl_dropped)
    c.put(f"{BASE}/meters/P1", json={"decimals": 1})
    r = c.post(f"{BASE}/extract-reading", json={"imageBase64": b64(jpeg_bytes), "consumerId": "P1"}).json()["result"]
    assert r["status"] == "NEEDS_REVIEW" and r["data"]["reasons"][0]["code"] == "DIGIT_COUNT_MISMATCH"
    unknown_dropped = answer("008365.8", make=None, model_code=None, conf=0.97)
    c = app_with(unknown_dropped)
    c.put(f"{BASE}/meters/U1", json={"digits": 8})
    r = c.post(f"{BASE}/extract-reading", json={"imageBase64": b64(jpeg_bytes), "consumerId": "U1"}).json()["result"]
    assert r["status"] == "NEEDS_REVIEW" and r["data"]["reasons"][0]["code"] == "DIGIT_COUNT_MISMATCH"


def test_accurate_on_a_retake_does_not_become_the_trusted_reading(app_with, jpeg_bytes):
    c = app_with(answer("0012345", status="NOT_CUMULATIVE_SCREEN", screen="max_demand", make=None, model_code=None))
    r = c.post(f"{BASE}/extract-reading", json={"imageBase64": b64(jpeg_bytes), "consumerId": "K1"}).json()
    c.post(f"{BASE}/feedback", json={"correlationId": r["result"]["correlationId"], "data": {"accurate": True}})
    store = c.app.state.service.metadata_store
    assert store.last_trusted("K1", before=datetime.now(timezone.utc) + timedelta(days=1)) is None


def test_nometer_returns_the_explanation_not_a_guess(app_with, jpeg_bytes):
    c = app_with(answer("0019712.7", status="NO_DISPLAY", screen="not_visible"))
    r = c.post(f"{BASE}/extract-reading", json={"id": "JS-6", "imageBase64": b64(jpeg_bytes)}).json()
    assert r["result"]["status"] == "NOMETER" and r["result"]["data"]["meterReading"].startswith("No meter display")
    assert c.get(f"{BASE}/readings/JS-6").json()["result"]["meter_reading"] is None


def test_feedback_for_unknown_reading_is_404(app_with):
    c = app_with()
    r = c.post(f"{BASE}/feedback", json={"correlationId": "11111111-2222-3333-4444-555555555555",
                                         "data": {"accurate": True}})
    assert r.status_code == 404 and r.json()["error"]["errorCode"] == "ERROR_08"


def test_billing_unit_must_be_an_energy_unit(app_with):
    c = app_with()
    assert c.put(f"{BASE}/meters/B1", json={"billingUnit": "kW"}).status_code == 400
    assert c.put(f"{BASE}/meters/B1", json={"billingUnit": "kvah"}).json()["result"]["billingUnit"] == "kVAh"


def test_upload_accepts_octet_stream_and_records_failures(app_with, jpeg_bytes):
    c = app_with()
    ok = c.post(f"{BASE}/extract-reading/upload", data={"id": "UP-1", "metadata": '{"agentId": "A7"}'},
                files={"image": ("m.jpg", jpeg_bytes, "application/octet-stream")})
    assert ok.status_code == 200 and ok.json()["id"] == "UP-1"
    assert c.get(f"{BASE}/readings/UP-1").json()["result"]["metadata"] == {"agentId": "A7"}
    bad = c.post(f"{BASE}/extract-reading/upload", data={"id": "UP-2"}, files={"image": ("m.jpg", b"nope", "image/jpeg")})
    assert bad.status_code == 415 and c.get(f"{BASE}/readings/UP-2").json()["result"]["meter_reading_status"] == "ERROR"


# ---------- images ----------
def test_link_local_and_metadata_addresses_are_always_blocked():
    for url in ("http://169.254.169.254/latest/meta-data/", "http://169.254.170.2/v2/credentials",
                "http://[fd00:ec2::254]/", "http://0.0.0.0/x.jpg"):
        with pytest.raises(ImageError, match="blocked address"):
            _check_url(url, (), allow_private=True)
    _check_url("http://127.0.0.1/x.jpg", (), allow_private=True)           # same host or VPC: allowed on request
    with pytest.raises(ImageError, match="private or local"):
        _check_url("http://10.0.0.5/x.jpg", (), allow_private=False)


def test_oversized_images_are_refused_before_decoding(jpeg_bytes):
    from PIL import Image
    png = io.BytesIO()
    Image.new("RGB", (64, 48)).save(png, format="PNG")
    with pytest.raises(ImageError, match="too large to process"):
        prepare_image(png.getvalue(), 10**7, 2048, max_pixels=1000)        # 64x48 = 3072 pixels
    prepare_image(jpeg_bytes, 10**7, 2048, max_pixels=1000)                 # JPEGs have their own, higher limit
    with pytest.raises(ImageError, match="too large to process"):
        prepare_image(jpeg_bytes, 10**7, 2048, max_pixels=1000, max_jpeg_pixels=1000)


def test_s3_photo_store():
    boto3 = pytest.importorskip("boto3")
    from botocore.stub import Stubber
    from service.api.image_store import S3ImageStore
    client = boto3.client("s3", region_name="ap-south-1", aws_access_key_id="x", aws_secret_access_key="y")
    store = S3ImageStore("s3://meter-photos/pilot", client=client)
    with Stubber(client) as stub:
        stub.add_response("put_object", {}, {"Bucket": "meter-photos", "Key": "pilot/2026-09-30/JS-7.jpg",
                                              "Body": b"\xff\xd8\xff\xe0data", "ContentType": "image/jpeg",
                                              "ServerSideEncryption": "AES256"})
        name = store.save("JS-7", b"\xff\xd8\xff\xe0data", datetime(2026, 9, 30, tzinfo=timezone.utc))
        stub.add_response("get_object", {"Body": io.BytesIO(b"\xff\xd8\xff\xe0data")},
                          {"Bucket": "meter-photos", "Key": "pilot/2026-09-30/JS-7.jpg"})
        assert name == "2026-09-30/JS-7.jpg" and store.load(name) == b"\xff\xd8\xff\xe0data"
    assert store.load("../../secret") is None                             # only names the store writes


# ---------- OpenAI retry policy ----------
class _FakeOpenAI:
    """Answers each POST with the next scripted (status, body, headers, delay)."""

    def __init__(self, script):
        self.script, self.calls = list(script), 0
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                status, body, headers, delay = fake.script[min(fake.calls, len(fake.script) - 1)]
                fake.calls += 1
                time.sleep(delay)
                payload = json.dumps(body).encode()
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *a):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1"


OK_BODY = {"choices": [{"message": {"content": json.dumps(answer("00197127"))}, "finish_reason": "stop"}],
           "usage": {"prompt_tokens": 10, "completion_tokens": 5}, "model": "gpt-6-sol"}


def _client(url, timeout=30.0):
    return OpenAIVisionClient(ReaderSettings(openai_api_key="k", openai_base_url=url, request_timeout=timeout))


def test_openai_retries_once_after_503_honouring_retry_after(monkeypatch):
    monkeypatch.setattr("service.vision.openai_client.MIN_ATTEMPT_SECONDS", 0.5)
    fake = _FakeOpenAI([(503, {"error": {"message": "busy"}}, {"Retry-After": "0"}, 0), (200, OK_BODY, {}, 0)])
    try:
        assert _client(fake.url).read(b"img")["answer"]["display_text"] == "00197127" and fake.calls == 2
    finally:
        fake.server.shutdown()


def test_openai_out_of_credit_is_not_retried():
    fake = _FakeOpenAI([(429, {"error": {"code": "insufficient_quota"}}, {}, 0)])
    try:
        with pytest.raises(ModelError) as e:
            _client(fake.url).read(b"img")
        assert e.value.alert and not e.value.retryable and fake.calls == 1
    finally:
        fake.server.shutdown()


def test_openai_timeout_is_not_retried_and_deadline_caps_the_wait(monkeypatch):
    monkeypatch.setattr("service.vision.openai_client.MIN_ATTEMPT_SECONDS", 0.2)
    fake = _FakeOpenAI([(200, OK_BODY, {}, 3)])
    try:
        t0 = time.monotonic()
        with pytest.raises(ModelError) as e:
            _client(fake.url, timeout=30).read(b"img", deadline=time.monotonic() + 1.0)
        assert e.value.retryable and fake.calls == 1 and time.monotonic() - t0 < 2.5
    finally:
        fake.server.shutdown()


# ---------- logging ----------
def test_json_log_lines_are_single_lines():
    from conf.logging import JsonFormatter
    try:
        raise ValueError("boom\nsecond line")
    except ValueError:
        record = logging.LogRecord("x", logging.ERROR, __file__, 1, "failed: %s", ("a",), exc_info=__import__("sys").exc_info())
    line = JsonFormatter().format(record)
    assert "\n" not in line and json.loads(line)["message"] == "failed: a" and "boom" in json.loads(line)["exception"]


def test_fake_client_accepts_deadline():
    assert FakeClient(answer()).read(b"x", deadline=time.monotonic() + 5)["answer"]
    assert Decimal("1") == Decimal("1")


# ---------- review fixes: no double spend, no wrong reading ----------
def test_digits_only_setting_keeps_the_clear_read_rule_for_the_decimal_point(app_with, jpeg_bytes):
    # an ER300 (8 digits, no decimal point) read with an invented point at 0.92 confidence
    c = app_with(answer("0001810.3", make="L&T", model_code=None, conf=0.92))
    c.put(f"{BASE}/meters/D1", json={"digits": 8})
    r = c.post(f"{BASE}/extract-reading", json={"imageBase64": b64(jpeg_bytes), "consumerId": "D1"}).json()["result"]
    assert r["status"] == "NEEDS_REVIEW" and r["data"]["reasons"][0]["code"] == "NO_CROSS_CHECK"


def test_reused_id_for_another_consumer_or_photo_is_refused(app_with, jpeg_bytes, another_jpeg):
    c = app_with()
    first = c.post(f"{BASE}/extract-reading", json={"id": "JS-10", "imageBase64": b64(jpeg_bytes), "consumerId": "A"})
    assert first.status_code == 200
    for body in ({"consumerId": "B", "imageBase64": b64(jpeg_bytes)}, {"consumerId": "A", "imageBase64": b64(another_jpeg)}):
        r = c.post(f"{BASE}/extract-reading", json={"id": "JS-10", **body})
        assert r.status_code == 409 and r.json()["error"]["errorCode"] == "ERROR_15"
        assert r.json()["error"]["retryable"] is False
    assert c.app.state.service.reader.client.calls == 1


def test_same_photo_sent_again_with_a_new_id_is_not_read_again(app_with, jpeg_bytes):
    c = app_with()
    fake = c.app.state.service.reader.client
    first = c.post(f"{BASE}/extract-reading", json={"id": "JS-11", "imageBase64": b64(jpeg_bytes), "consumerId": "P"}).json()
    again = c.post(f"{BASE}/extract-reading", json={"id": "JS-12", "imageBase64": b64(jpeg_bytes), "consumerId": "P"})
    assert fake.calls == 1 and again.headers["Duplicate-Of"] == "JS-11"
    body = again.json()
    assert body["id"] == "JS-12" and body["result"]["data"]["meterReading"] == first["result"]["data"]["meterReading"]
    assert body["result"]["data"]["costEstimateUsd"] == 0.0
    row = c.get(f"{BASE}/readings/JS-12").json()["result"]
    assert row["cost_usd"] == 0.0 and row["meter_reading_status"] == "SUCCESS" and row["image_path"]
    # another consumer's photo is always read: results never cross between consumers
    c.post(f"{BASE}/extract-reading", json={"id": "JS-13", "imageBase64": b64(jpeg_bytes), "consumerId": "Q"})
    assert fake.calls == 2


def test_duplicate_check_can_be_switched_off(app_with, jpeg_bytes):
    c = app_with(env={"METER_DUPLICATE_PHOTO_HOURS": "0"})
    for rid in ("JS-14", "JS-15"):
        c.post(f"{BASE}/extract-reading", json={"id": rid, "imageBase64": b64(jpeg_bytes), "consumerId": "P"})
    assert c.app.state.service.reader.client.calls == 2


def test_billed_model_failures_count_against_the_budget(app_with, jpeg_bytes, another_jpeg):
    c = app_with(env={"METER_DAILY_BUDGET_USD": "0.10"})

    class CutOff:                         # answered (and billed: ~16k output tokens) but unusable
        calls = 0

        def read(self, *a, **k):
            CutOff.calls += 1
            raise ModelError("model output was cut off", usage={"prompt_tokens": 2500, "completion_tokens": 16000})
    c.app.state.service.reader.client = CutOff()
    r = c.post(f"{BASE}/extract-reading", json={"id": "JS-16", "imageBase64": b64(jpeg_bytes)})
    assert r.status_code == 502
    assert c.get(f"{BASE}/readings/JS-16").json()["result"]["cost_usd"] == pytest.approx(0.165)
    r = c.post(f"{BASE}/extract-reading", json={"id": "JS-17", "imageBase64": b64(another_jpeg)})
    assert r.status_code == 429 and CutOff.calls == 1              # the budget saw the $0.165



def test_a_retried_id_keeps_the_cost_of_its_billed_failure(app_with, jpeg_bytes):
    c = app_with()
    working = c.app.state.service.reader.client

    class CutOff:
        def read(self, *a, **k):
            raise ModelError("model output was cut off", usage={"prompt_tokens": 2500, "completion_tokens": 16000})
    c.app.state.service.reader.client = CutOff()
    assert c.post(f"{BASE}/extract-reading", json={"id": "JS-21", "imageBase64": b64(jpeg_bytes)}).status_code == 502
    c.app.state.service.reader.client = working
    assert c.post(f"{BASE}/extract-reading", json={"id": "JS-21", "imageBase64": b64(jpeg_bytes)}).status_code == 200
    assert c.get(f"{BASE}/readings/JS-21").json()["result"]["cost_usd"] == pytest.approx(0.165 + 0.008)

def test_admission_limit_answers_busy_before_taking_a_thread(app_with, jpeg_bytes):
    c = app_with(env={"METER_MAX_CONCURRENT_READS": "1"})
    c.app.state.admission["in"] = c.app.state.admission["limit"]     # two readings already admitted
    r = c.post(f"{BASE}/extract-reading", json={"imageBase64": b64(jpeg_bytes)})
    assert r.status_code == 503 and r.json()["error"]["errorCode"] == "ERROR_10"
    c.app.state.admission["in"] = 0
    assert c.post(f"{BASE}/extract-reading", json={"imageBase64": b64(jpeg_bytes)}).status_code == 200


def test_api_key_is_checked_before_the_body_is_read(app_with):
    c = app_with(env={"METER_MAX_BODY_BYTES": "1000"}, api_key="the-right-key-000000")
    r = c.post(f"{BASE}/extract-reading", json={"imageBase64": "A" * 5000})
    assert r.status_code == 401 and r.json()["error"]["errorCode"] == "ERROR_09"


def test_signed_image_urls_are_stored_without_their_query(app_with):
    c = app_with()
    c.post(f"{BASE}/extract-reading", json={"id": "JS-18", "imageURL": "https://user:pw@img.invalid/p/1.jpg?X-Amz-Signature=s3cr3t"})
    row = c.get(f"{BASE}/readings/JS-18").json()["result"]
    assert row["image_url"] == "https://img.invalid/p/1.jpg" and "s3cr3t" not in json.dumps(row)


def test_a_superseded_request_does_not_overwrite_the_newer_one(store):
    t0 = datetime.now(timezone.utc) - timedelta(hours=1)
    store.claim({"request_id": "JS-19", "request_timestamp": t0}, stale_after=160)
    t1 = datetime.now(timezone.utc)
    assert store.claim({"request_id": "JS-19", "request_timestamp": t1}, stale_after=160)[0] == "retry"
    assert store.store_extraction({"request_id": "JS-19", "request_timestamp": t0,
                                   "meter_reading_status": "SUCCESS", "meter_reading": "1"}) is False
    assert store.store_extraction({"request_id": "JS-19", "request_timestamp": t1,
                                   "meter_reading_status": "SUCCESS", "meter_reading": "2"}) is True
    assert store.get_record("JS-19")["meter_reading"] == "2"


def test_s3_access_denied_is_a_missing_photo():
    boto3 = pytest.importorskip("boto3")
    from botocore.stub import Stubber
    from service.api.image_store import S3ImageStore
    client = boto3.client("s3", region_name="ap-south-1", aws_access_key_id="x", aws_secret_access_key="y")
    store = S3ImageStore("s3://meter-photos/pilot", client=client)
    with Stubber(client) as stub:
        stub.add_client_error("get_object", service_error_code="AccessDenied", http_status_code=403)
        assert store.load("2026-09-30/JS-20.jpg") is None


# ---------- PostgreSQL concurrency (run when METER_TEST_DB_URL points at PostgreSQL, as in CI) ----------
needs_postgres = pytest.mark.skipif(not __import__("os").getenv("METER_TEST_DB_URL", "").startswith("postgresql"),
                                    reason="needs METER_TEST_DB_URL=postgresql+psycopg2://...")


@needs_postgres
def test_concurrent_startups_on_an_empty_database(db_url):
    from concurrent.futures import ThreadPoolExecutor
    from service.api.database import DatabaseService, metadata
    engine = DatabaseService(db_url).engine
    metadata.drop_all(engine)                                  # a brand-new database, as on first deploy
    engine.dispose()
    with ThreadPoolExecutor(8) as ex:                          # 8 workers/instances booting at once
        services = list(ex.map(lambda _: DatabaseService(db_url), range(8)))
    assert all(s.ping() for s in services)


@needs_postgres
def test_concurrent_requests_with_the_same_id_claim_it_once(store):
    from concurrent.futures import ThreadPoolExecutor
    record = {"request_id": "JS-RACE", "request_timestamp": datetime.now(timezone.utc)}
    with ThreadPoolExecutor(8) as ex:
        states = [s for s, _ in ex.map(lambda _: store.claim(dict(record), stale_after=160), range(8))]
    assert states.count("new") == 1 and states.count("in_progress") == 7


def test_shipped_daily_budget_is_25_dollars(config, monkeypatch):
    from conf.config import load_service_settings
    s = load_service_settings(config)
    assert s.daily_budget_usd == 25.0 and s.budget_configured          # production accepts it without an env var
    monkeypatch.setenv("METER_DAILY_BUDGET_USD", "none")
    assert load_service_settings(config).daily_budget_usd is None       # switched off only on purpose
