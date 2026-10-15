"""Regressions for the bugs found by the second live system test (2026-10-05), one test per confirmed root cause
(report ids in brackets)."""
import base64
import hashlib
import http.server
import importlib.util
import json
import logging
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app_factory import caller_id, create_app
from conf.config import ServiceSettings, check_production
from conf.logging import NoQueryFilter
from conftest import FakeClient, answer, make_jpeg
from models.models import ReadingExtractionRequest
from service.api.reading_service import _link_identity, fingerprint, legacy_fingerprints, redact_url
from service.vision import ModelError, ReaderSettings, checks
from service.vision import images
from service.vision.images import ImageError, fetch_image_url, prepare_image
from service.vision.openai_client import OpenAIVisionClient

BASE = "/flowvision/v1/elm"


def b64(data):
    return base64.b64encode(data).decode()


@pytest.fixture
def app_with(config, make_reader, db_url, monkeypatch, tmp_path):
    def _make(*answers, env=None, api_key="", file_db=False, **reader_overrides):
        for k, v in (env or {}).items():
            monkeypatch.setenv(k, v)
        reader = make_reader(*(answers or (answer("00197127"),)), **reader_overrides)
        # concurrency tests: in-memory SQLite shares one connection between threads; a file gives each its own
        url = f"sqlite:///{tmp_path / 'concurrent.db'}" if file_db and db_url == "sqlite://" else db_url
        return TestClient(create_app(config=config, reader=reader, db_url=url, service_api_key=api_key),
                          raise_server_exceptions=False)
    return _make


def post(c, **body):
    return c.post(f"{BASE}/extract-reading", json=body)


# ---------- a corrected reading must never come back as ACCEPT ----------
def test_corrected_photo_resent_with_the_same_capture_time_goes_to_review(app_with, jpeg_bytes):     # [BURN-01]
    c = app_with()
    t = "2026-09-25T10:00:00+05:30"
    first = post(c, id="C1-a", imageBase64=b64(jpeg_bytes), consumerId="K", capturedAt=t).json()
    c.post(f"{BASE}/feedback", json={"correlationId": first["result"]["correlationId"],
                                     "data": {"accurate": False, "actual": "0019721.7"}})
    calls = c.app.state.service.reader.client.calls
    for rid in ("C1-b", "C1-c"):                               # same capturedAt, then none
        r = post(c, id=rid, imageBase64=b64(jpeg_bytes), consumerId="K", **({"capturedAt": t} if rid == "C1-b" else {}))
        assert r.json()["result"]["status"] == "NEEDS_REVIEW" and r.headers["Duplicate-Of"] == "C1-a"
    assert c.app.state.service.reader.client.calls == calls     # never read (or paid) again
    # the correction stays the consumer's trusted history: a new photo reading the old wrong value is held
    later = post(c, id="C1-d", imageBase64=b64(make_jpeg((1, 2, 3))), consumerId="K",
                 capturedAt="2026-09-25T10:01:00+05:30").json()["result"]
    assert later["status"] == "NEEDS_REVIEW" and later["data"]["reasons"][0]["code"] == "BELOW_PREVIOUS"
    assert later["data"]["previousReading"]["value"] == "19721.7"


def test_photo_is_read_again_after_the_meter_settings_change(app_with, jpeg_bytes):                  # [FIX2-04]
    c = app_with(answer("00018103", make="L&T", model_code="ER300P"))
    post(c, id="M1-a", imageBase64=b64(jpeg_bytes), consumerId="M")
    c.put(f"{BASE}/meters/M", json={"meterTypeId": "lt_er300"})
    r = post(c, id="M1-b", imageBase64=b64(jpeg_bytes), consumerId="M")
    assert "Duplicate-Of" not in r.headers and c.app.state.service.reader.client.calls == 2


def test_copies_written_by_older_versions_are_not_history_or_reused(app_with, jpeg_bytes):           # [UPG-2, UPG-3]
    c = app_with()
    store = c.app.state.service.metadata_store
    sha = hashlib.sha1(jpeg_bytes).hexdigest()
    first = post(c, id="U-a", imageBase64=b64(jpeg_bytes), consumerId="U",
                 capturedAt="2026-09-20T10:00:00+05:30").json()
    # what an older version wrote for a resend of the same photo: a copy with no duplicate_of mark, read days later
    row = store.get_record("U-a")
    copy = {k: row[k] for k in ("correlation_id", "meter_reading_status", "meter_reading", "unit", "response_json")}
    now = datetime.now(timezone.utc)
    store.store_rejected({"request_id": "U-b", "consumer_id": "U", "image_sha1": sha, "request_timestamp": now,
                          "captured_at": datetime(2026, 9, 24, 4, 30, tzinfo=timezone.utc), **copy})
    c.post(f"{BASE}/feedback", json={"correlationId": first["result"]["correlationId"],
                                     "data": {"actual": "0019700.0"}})     # sets both rows' correction (same id)
    prev = store.last_trusted("U", before=datetime(2026, 9, 30, tzinfo=timezone.utc))
    assert prev.captured_at.date().isoformat() == "2026-09-20"        # the original, not the later copy
    r = post(c, id="U-c", imageBase64=b64(jpeg_bytes), consumerId="U")
    assert r.json()["result"]["status"] == "NEEDS_REVIEW" and c.app.state.service.reader.client.calls == 1


# ---------- the same photo sent twice at once is paid once ----------
def test_same_photo_in_flight_under_another_id_is_not_read_twice(app_with, jpeg_bytes):        # [LOAD-1, BURN-02]
    c = app_with()
    store = c.app.state.service.metadata_store
    started = datetime.now(timezone.utc)
    store.claim({"request_id": "F-a", "consumer_id": "F", "request_timestamp": started}, stale_after=160)
    assert store.claim_photo("F-a", started, "F", hashlib.sha1(jpeg_bytes).hexdigest(),
                             started - timedelta(hours=1), 160)[0] == "new"      # F-a is being read now
    r = post(c, id="F-b", imageBase64=b64(jpeg_bytes), consumerId="F")
    assert r.status_code == 409 and r.json()["error"]["errorCode"] == "ERROR_12" and r.json()["error"]["retryable"]
    assert "F-a" in r.json()["error"]["errorMsg"] and c.app.state.service.reader.client.calls == 0


def test_concurrent_copies_of_one_photo_make_one_model_call(app_with, jpeg_bytes):           # [LOAD-1, FIX2-12]
    c = app_with(file_db=True)
    service = c.app.state.service
    slow = FakeClient(answer("00197127"))
    real_read = slow.read

    def read(*a, **k):
        time.sleep(3)
        return real_read(*a, **k)
    slow.read = read
    service.reader.client = slow
    reqs = [ReadingExtractionRequest(id=f"P-{i}", imageBase64=b64(jpeg_bytes), consumerId="P") for i in range(6)]
    with ThreadPoolExecutor(6) as ex:
        replies = list(ex.map(service.extract_reading, reqs))
    assert slow.calls == 1                                   # one paid read for six copies
    read_ids = [r.response.id for r in replies if r.response.statusCode == 200 and "Duplicate-Of" not in r.headers]
    refused = [r.response.id for r in replies if r.response.statusCode == 409]
    assert len(read_ids) == 1 and len(refused) + 1 == 6 - sum("Duplicate-Of" in r.headers for r in replies)
    # each refused id, sent again, gets the finished result without a model call
    for rid in refused:
        again = post(c, id=rid, imageBase64=b64(jpeg_bytes), consumerId="P")
        assert again.status_code == 200 and again.headers["Duplicate-Of"] == read_ids[0]
    assert slow.calls == 1


# ---------- the daily cap counts readings in flight ----------
def test_readings_in_flight_count_against_the_cap(app_with, jpeg_bytes):              # [LOAD-2, BURN-03, FIX2-09]
    c = app_with(env={"METER_DAILY_BUDGET_USD": "0.07"}, file_db=True)    # room for one $0.05 reservation
    service = c.app.state.service
    slow = FakeClient(answer("00197127"))
    real_read = slow.read

    def read(*a, **k):
        time.sleep(3)                                        # every other request arrives while this one is paid for
        return real_read(*a, **k)
    slow.read = read
    service.reader.client = slow
    reqs = [ReadingExtractionRequest(id=f"B-{i}", imageBase64=b64(make_jpeg((20 * i, 40, 80)))) for i in range(12)]
    with ThreadPoolExecutor(12) as ex:
        replies = list(ex.map(service.extract_reading, reqs))
    assert slow.calls == 1                                   # $0.07 of room: one reading, not twelve
    assert sum(r.response.statusCode == 429 for r in replies) == 11
    spent = sum(service.metadata_store.get_record(f"B-{i}")["cost_usd"] or 0 for i in range(12))
    assert spent <= 0.07                                     # never above the cap


def test_a_dead_workers_call_is_counted_and_carried_to_the_retry(app_with, jpeg_bytes):            # [DOCK-3, BURN-09]
    c = app_with()
    store = c.app.state.service.metadata_store
    long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
    store.claim({"request_id": "D-a", "request_timestamp": long_ago}, stale_after=160)
    assert store.reserve("D-a", long_ago, 0.03, long_ago - timedelta(days=1), None)[0]   # then the worker died
    assert store.get_record("D-a")["cost_usd"] == pytest.approx(0.03)         # counted while it lies abandoned
    assert post(c, id="D-a", imageBase64=b64(jpeg_bytes)).status_code == 200
    assert store.get_record("D-a")["cost_usd"] == pytest.approx(0.038)        # the retry adds its own $0.008


def test_budget_day_is_india_time_whatever_the_naive_time_offset(app_with):                          # [BURN-11]
    c = app_with(env={"METER_NAIVE_TIME_OFFSET": "+00:00"})
    start = c.app.state.service.budget.day_start(datetime(2026, 10, 5, 20, 0, tzinfo=timezone.utc))
    assert start.isoformat() == "2026-10-06T00:00:00+05:30"


# ---------- tariff, bill and demand labels ----------
@pytest.mark.parametrize("label", ["01 B kWh", "01B", "kWh 01 1 2 3", "TOU1", "ToU 1 kWh", "TARIFF 1", "RATE 1 kWh",
                                   "kWhT1", "T01", "T.1", "T_1", "T – 1", "MAX DEMAND", "MDkW", "Z01"])
def test_more_tariff_bill_and_demand_labels_are_refused(app_with, jpeg_bytes, label):              # [FIX2-03, ACC-1]
    c = app_with({**answer("00197127"), "screen_label": label})
    r = post(c, imageBase64=b64(jpeg_bytes)).json()["result"]
    assert r["status"] == "UNCLEAR" and r["data"]["reasons"][0]["code"] == "WRONG_SCREEN"


@pytest.mark.parametrize("label", ["CUM kVAh 028 →", "R Y B; Prepaid; TMPR; k W h", "RYB← kW h",
                                   "PRE PAID; ON; kVAh; CUM", "CUM kVA h 1 2 3 →"])
def test_real_total_screen_labels_still_pass(app_with, jpeg_bytes, label):
    c = app_with({**answer("00197127"), "screen_label": label})
    assert post(c, imageBase64=b64(jpeg_bytes)).json()["result"]["status"] == "SUCCESS"


def test_reading_on_a_voltage_screen_keeps_the_point_shown():                                          # [ACC-3]
    a = {**answer("238.47", screen="voltage_current_pf", unit="V", status="NOT_CUMULATIVE_SCREEN")}
    rule = checks.MeterRule(1, 8, "registry", "hpl_ppsm01")
    assert checks.candidate(a, rule).reading == "238.47"
    assert checks.candidate(answer("00197127"), rule).reading == "0019712.7"   # energy screens still use the type


# ---------- links: what is stored, and what counts as the same photo ----------
def test_links_are_stored_and_logged_without_any_query_string():                     # [FIX2-01, DOC-04, SEC-4]
    url = "https://img.example/p/a.jpg?photoId=7&jwt=SECRET1&hmac=SECRET2&passwd=SECRET3&s=x#frag"
    assert redact_url(url) == "https://img.example/p/a.jpg"
    assert redact_url("http://user:pw@[10.0.0.5]/x.jpg?t=1") == "http://[10.0.0.5]/x.jpg"   # not a valid link


def test_a_reissued_token_link_is_the_same_photo(app_with, monkeypatch):                         # [FIX2-02, BURN-07]
    base = "https://img.example/p"
    same = lambda a, b: _link_identity(a) == _link_identity(b)
    assert same(f"{base}?photoId=3&token=AAA", f"{base}?photoId=3&token=BBB")
    assert same(f"{base}?photoId=3&access_token=A", f"{base}?access_token=B&photoId=3")
    assert same("https://img.example/a b.jpg", "https://img.example/a%20b.jpg")
    assert not same(f"{base}?photoId=3&token=A", f"{base}?photoId=4&token=A")


def test_an_id_answered_by_the_older_version_replays_after_the_upgrade(app_with, jpeg_bytes):            # [UPG-1]
    c = app_with()
    store = c.app.state.service.metadata_store
    req = ReadingExtractionRequest(id="O-1", imageBase64="data:image/jpeg;base64," + b64(jpeg_bytes), consumerId="O")
    old = legacy_fingerprints(req, None).pop()
    assert old != fingerprint(req, None)
    first = post(c, id="O-1", imageBase64="data:image/jpeg;base64," + b64(jpeg_bytes), consumerId="O")
    store.database_service.execute(store_update(old))                    # as the older version stored it
    again = post(c, id="O-1", imageBase64="data:image/jpeg;base64," + b64(jpeg_bytes), consumerId="O")
    assert again.status_code == 200 and again.headers["Idempotent-Replay"] == "true"
    assert again.json()["result"]["correlationId"] == first.json()["result"]["correlationId"]


def store_update(fp):
    from service.api.database import extraction_data as t
    import sqlalchemy as sa
    return sa.update(t).where(t.c.request_id == "O-1").values(request_fingerprint=fp)


def test_failed_id_of_one_consumer_is_not_taken_over_by_another(app_with, jpeg_bytes):                  # [BURN-06]
    c = app_with()
    assert post(c, id="X-1", imageBase64=b64(b"not a photo"), consumerId="first").status_code == 415
    r = post(c, id="X-1", imageBase64=b64(jpeg_bytes), consumerId="second")
    assert r.status_code == 409 and r.json()["error"]["errorCode"] == "ERROR_15"
    assert c.get(f"{BASE}/readings/X-1").json()["result"]["consumer_id"] == "first"
    assert post(c, id="X-1", imageBase64=b64(jpeg_bytes), consumerId="first").status_code == 200   # its own retry


# ---------- image links: SSRF and odd links ----------
class _Local:
    """An image host on 127.0.0.1; /slowheaders trickles its headers, one byte every 0.1 s."""

    def __init__(self, image):
        self.seen = []
        local = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                local.seen.append((self.path, self.headers.get("Authorization")))
                if self.path == "/slowheaders":
                    self.wfile.write(b"HTTP/1.1 200 OK\r\n")
                    for ch in b"X-Slow: " + b"a" * 100:
                        self.wfile.write(bytes([ch]))
                        self.wfile.flush()
                        time.sleep(0.1)
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(image)))
                self.end_headers()
                self.wfile.write(image)

            def log_message(self, *a):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.port = self.server.server_port


@pytest.fixture
def local(jpeg_bytes):
    h = _Local(jpeg_bytes)
    yield h
    h.server.shutdown()


def test_dns_rebinding_cannot_reach_a_blocked_address(local, monkeypatch):                         # [SEC-1, DOC-03]
    real = socket.getaddrinfo
    answers = iter([[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", local.port))]])

    def rebinding(host, port, *a, **k):
        if host == "rebind.example":          # first lookup: a public address; every later one: this machine
            return next(answers, [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))])
        return real(host, port, *a, **k)
    monkeypatch.setattr(socket, "getaddrinfo", rebinding)
    with pytest.raises(ImageError, match="private or local"):
        fetch_image_url(f"http://rebind.example:{local.port}/x.jpg", 10**7)
    assert local.seen == []                    # nothing ever reached the local host


@pytest.mark.parametrize("url,message", [
    ("http://[fd00:ec2::254]/x.jpg", "blocked"), ("http://[0:0:0:0:0:ffff:a9fe:a9fe]/x.jpg", "blocked"),
    ("http://[::1]:1/x.jpg", "private or local"), ("http://user:pw@example.org/x.jpg", "user name or password")])
def test_odd_links_are_refused_cleanly(url, message):                                          # [SEC-5, SEC-7]
    with pytest.raises(ImageError, match=message) as e:
        fetch_image_url(url, 10**7)
    assert e.value.retryable is False


def test_odd_links_are_a_clean_400_through_the_api(app_with):                                        # [SEC-5]
    c = app_with()
    for i, url in enumerate(("http://[10.0.0.5]/x.jpg", "http://[fd00:ec2::254]/x.jpg", "http://u:p@h.example/x")):
        r = post(c, id=f"L-{i}", imageURL=url)
        assert r.status_code == 400 and r.json()["id"] == f"L-{i}" and r.json()["error"]["retryable"] is False


def test_trickled_headers_cannot_outlast_the_download_timeout(local):                                   # [FIX2-07]
    t0 = time.monotonic()
    with pytest.raises(ImageError, match="timed out"):
        fetch_image_url(f"http://127.0.0.1:{local.port}/slowheaders", 10**7, timeout=1.5, allow_private=True)
    assert time.monotonic() - t0 < 4


def test_credentials_go_over_plain_http_only_to_a_private_address(local, monkeypatch):            # [DOC-02, SEC-3]
    creds = {"Authorization": "Bearer t"}
    fetch_image_url(f"http://127.0.0.1:{local.port}/x.jpg", 10**7, allowed_hosts=("127.0.0.1",),
                    allow_private=True, headers=creds)
    assert local.seen[-1] == ("/x.jpg", "Bearer t")          # same network: sent
    real = socket.getaddrinfo

    def public_then_local(host, port, *a, **k):              # checked as a public address, then served from here
        if host == "pub.example":
            ip = "93.184.216.34" if not public_then_local.checked else "127.0.0.1"
            public_then_local.checked = True
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]
        return real(host, port, *a, **k)
    public_then_local.checked = False
    monkeypatch.setattr(socket, "getaddrinfo", public_then_local)
    fetch_image_url(f"http://pub.example:{local.port}/y.jpg", 10**7, allowed_hosts=("pub.example",),
                    allow_private=True, headers=creds)
    assert local.seen[-1] == ("/y.jpg", None)                # a public address over plain http: never sent


# ---------- OpenAI answers that are cut off, garbled or odd ----------
class _FakeOpenAI:
    def __init__(self, mode):
        self.calls = 0
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                fake.calls += 1
                if mode == "chunked-cut":
                    self.send_response(200)
                    self.send_header("Transfer-Encoding", "chunked")
                    self.end_headers()
                    self.wfile.write(b"10\r\n{\"usage\": {\"pro")
                    self.wfile.flush()
                    self.connection.shutdown(socket.SHUT_RDWR)
                    return
                if mode == "garbled":
                    self.wfile.write(b"NOT HTTP AT ALL\r\n\r\n")
                    self.connection.shutdown(socket.SHUT_RDWR)
                    return
                if mode == "negative-retry-after" and fake.calls == 1:
                    body = b'{"error": {"message": "slow down"}}'
                    self.send_response(429)
                    self.send_header("Retry-After", "-5")
                else:
                    payload = {"list": [1, 2]} if mode == "not-an-object" else answer("00197127")
                    body = json.dumps({"choices": [{"message": {"content": json.dumps(
                        [1, 2] if mode == "not-an-object" else payload)}, "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 2500, "completion_tokens": 300}}).encode()
                    self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.client = OpenAIVisionClient(ReaderSettings(openai_api_key="sk-fake-not-real",
                                                        openai_base_url=f"http://127.0.0.1:{self.server.server_port}"))


@pytest.mark.parametrize("mode", ["chunked-cut", "garbled"])
def test_a_cut_off_or_garbled_answer_counts_as_possibly_billed(mode):                          # [BURN-04, FIX2-06]
    fake = _FakeOpenAI(mode)
    try:
        with pytest.raises(ModelError) as e:
            fake.client.read(make_jpeg())
        assert e.value.maybe_billed and fake.calls == 1          # counted, and never sent a second time
    finally:
        fake.server.shutdown()


def test_an_answer_that_is_not_an_object_is_a_billed_model_error():                                   # [FIX2-06]
    fake = _FakeOpenAI("not-an-object")
    try:
        with pytest.raises(ModelError) as e:
            fake.client.read(make_jpeg())
        assert e.value.usage["completion_tokens"] == 300
    finally:
        fake.server.shutdown()


def test_negative_retry_after_is_retried_not_a_crash():                                                 # [BURN-12]
    fake = _FakeOpenAI("negative-retry-after")
    try:
        assert fake.client.read(make_jpeg())["answer"]["display_text"] == "00197127" and fake.calls == 2
    finally:
        fake.server.shutdown()


def test_with_two_reads_a_failed_read_keeps_the_others_cost(app_with, jpeg_bytes):                       # [BURN-08]
    c = app_with(reads=2)

    class OneFails(FakeClient):
        def read(self, *a, **k):
            self.calls += 1
            if self.calls == 1:
                raise ModelError("cut off", usage={"prompt_tokens": 2500, "completion_tokens": 300})
            return super().read(*a, **k)
    c.app.state.service.reader.client = OneFails(answer("00197127"))
    assert post(c, id="R2-a", imageBase64=b64(jpeg_bytes)).status_code == 502
    assert c.get(f"{BASE}/readings/R2-a").json()["result"]["cost_usd"] == pytest.approx(0.016)


# ---------- requests the database cannot hold, and other validation ----------
@pytest.mark.parametrize("body", [{"capturedAt": "0001-01-01T00:00:00"}, {"capturedAt": "9999-12-31T23:59:59"},
                                  {"consumerId": "a\u0000b"}, {"metadata": {"k\u0000": 1}},
                                  {"previousReading": {"value": "1", "capturedAt": "0001-01-01T00:00:00"}}])
def test_details_a_database_cannot_hold_are_a_400(app_with, jpeg_bytes, body):            # [DOC-01, UPG-4, FIX2-10]
    c = app_with()
    r = post(c, id="V-1", imageBase64=b64(jpeg_bytes), **body)
    assert r.status_code == 400 and r.json()["error"]["errorCode"] == "ERROR_06" and not r.json()["error"]["retryable"]
    assert c.app.state.service.reader.client.calls == 0


def test_booleans_are_refused_for_meter_settings(app_with):                                              # [FIX2-13]
    c = app_with()
    for body in ({"decimals": True}, {"digits": True}, {"decimals": False}):
        assert c.put(f"{BASE}/meters/B1", json=body).status_code == 400


def test_numeric_and_newline_ids_on_refused_requests():                                       # [FIX2-14, BURN-14]
    assert caller_id(7777777) == "7777777" and caller_id(True) is None
    assert caller_id("T10-nl\n") is None and caller_id("ok-1") == "ok-1"


def test_feedback_with_only_the_true_reading(app_with, jpeg_bytes):                                        # [DOC-09]
    c = app_with()
    res = post(c, imageBase64=b64(jpeg_bytes)).json()["result"]
    r = c.post(f"{BASE}/feedback", json={"correlationId": res["correlationId"], "data": {"actual": "0019713.0"}})
    assert r.status_code == 200
    assert c.post(f"{BASE}/feedback", json={"correlationId": res["correlationId"], "data": {}}).status_code == 400


def test_upload_without_a_file_keeps_and_stores_the_callers_id(app_with):                                 # [DOC-10]
    c = app_with()
    r = c.post(f"{BASE}/extract-reading/upload", data={"id": "up-4", "consumerId": "C"})
    assert r.status_code == 400 and r.json()["id"] == "up-4"
    assert c.get(f"{BASE}/readings/up-4").json()["result"]["meter_reading_status"] == "ERROR"


def test_immediate_busy_refusals_are_stored(app_with, jpeg_bytes):                                  # [FIX2-08, LOAD-3]
    c = app_with()
    c.app.state.admission["in"] = c.app.state.admission["limit"]
    r = post(c, id="Q-1", imageBase64=b64(jpeg_bytes), consumerId="C")
    assert r.status_code == 503 and r.json()["id"] == "Q-1"
    c.app.state.admission["in"] = 0
    assert c.get(f"{BASE}/readings/Q-1").json()["result"]["meter_reading_status"] == "ERROR"


def test_stored_times_are_sent_with_their_zone_on_sqlite(app_with, jpeg_bytes):                            # [UPG-5]
    c = app_with()
    post(c, id="Z-1", imageBase64=b64(jpeg_bytes), capturedAt="2026-09-30T10:00:00+05:30")
    row = c.get(f"{BASE}/readings/Z-1").json()["result"]
    assert datetime.fromisoformat(row["captured_at"]) == datetime(2026, 9, 30, 4, 30, tzinfo=timezone.utc)
    put = c.put(f"{BASE}/meters/Z", json={"decimals": 1}).json()["result"]
    assert datetime.fromisoformat(put["updatedAt"]).tzinfo is not None


def test_production_check_refuses_an_empty_host_list(monkeypatch):                                          # [FIX2-15]
    monkeypatch.setenv("METER_IMAGE_DIR", "s3://b/p")
    monkeypatch.setenv("METER_IMAGE_URL_ALLOWED_HOSTS", ",")
    s = ServiceSettings(api_keys=("k3Y-for-the-central-system-0001",), budget_configured=True)
    assert any("ALLOWED_HOSTS" in p for p in check_production(s, "postgresql://x", "s3://b/p"))
    monkeypatch.setenv("METER_IMAGE_URL_ALLOWED_HOSTS", "*")
    assert check_production(s, "postgresql://x", "s3://b/p") == []


# ---------- memory: images ----------
def test_large_png_is_refused_before_decoding():                                                           # [SEC-2]
    from PIL import Image
    import io
    buf = io.BytesIO()
    Image.new("L", (5000, 4000)).save(buf, format="PNG")          # 20 MP, tiny file
    with pytest.raises(ImageError, match="too large"):
        prepare_image(buf.getvalue(), 20 * 2**20, 2048, max_pixels=ReaderSettings(openai_api_key="x").max_image_pixels)


def test_phone_jpeg_decodes_at_reduced_size():                                                             # [DOCK-1]
    from PIL import Image
    import io
    buf = io.BytesIO()
    Image.new("RGB", (4000, 3000), (120, 160, 90)).save(buf, format="JPEG")
    out, mime = prepare_image(buf.getvalue(), 20 * 2**20, 2048)
    size = Image.open(io.BytesIO(out)).size
    assert mime == "image/jpeg" and 1536 <= max(size) <= 2048
    assert Image.MAX_IMAGE_PIXELS is None                         # Pillow's own 179 MP refusal is off   [SEC-6]


# ---------- logs and server settings ----------
def test_access_log_drops_query_strings():                                                                  # [SEC-2]
    record = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
                               ("1.2.3.4:5", "GET", "/meter-reader/v1/meter-types?X-API-Key=secret", "1.1", 401), None)
    NoQueryFilter().filter(record)
    assert "secret" not in record.getMessage()


def test_gunicorn_uses_the_configured_deadline(tmp_path, monkeypatch):                                       # [DOC-05]
    cfg = tmp_path / "config.yaml"
    cfg.write_text("limits:\n  request_deadline_seconds: 200\n", encoding="utf-8")
    monkeypatch.setenv("CONFIG_PATH", str(cfg))
    monkeypatch.setenv("METER_ENV", "production")
    import dotenv
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: None)   # never the machine's real .env in tests
    spec = importlib.util.spec_from_file_location("gconf", Path(__file__).resolve().parents[1] / "gunicorn.conf.py")
    gconf = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gconf)
    assert gconf.timeout == 260 and gconf.graceful_timeout == 220
    assert gconf.logconfig_dict["loggers"]["gunicorn.error"]["handlers"] == ["console"]   # JSON lines  [DOCK-6]


# ---------- re-verification findings (2026-10-05, round 3) ----------
def test_a_confirmation_is_not_a_correction(app_with, jpeg_bytes):                                     # [NEW-DOC-03]
    c = app_with()
    first = post(c, id="K3-a", imageBase64=b64(jpeg_bytes), consumerId="K3").json()
    c.post(f"{BASE}/feedback", json={"correlationId": first["result"]["correlationId"],
                                     "data": {"actual": "19712.70"}})          # the same value, written differently
    again = post(c, id="K3-b", imageBase64=b64(jpeg_bytes), consumerId="K3")
    assert again.json()["result"]["status"] == "SUCCESS" and again.headers["Duplicate-Of"] == "K3-a"


def test_an_invalid_consumer_id_does_not_lock_the_request_id(app_with, jpeg_bytes):                    # [NEW-DOC-04]
    c = app_with()
    assert post(c, id="m3", consumerId="Z" * 129, imageBase64=b64(jpeg_bytes)).status_code == 400
    assert post(c, id="m3", consumerId="Z" * 20, imageBase64=b64(jpeg_bytes)).status_code == 200


def test_refused_requests_with_unstorable_values_are_still_stored(app_with, jpeg_bytes):        # [NEW-DOC-05, -06]
    c = app_with()
    for rid, extra in (("nul-1", {"consumerId": "a\u0000b"}), ("nul-2", {"metadata": {"a\u0000": 1}}),
                       ("sur-1", {"metadata": {"x": "\udfff"}})):
        body = json.dumps({"id": rid, "imageBase64": b64(jpeg_bytes), **extra})
        r = c.post(f"{BASE}/extract-reading", content=body, headers={"Content-Type": "application/json"})
        assert r.status_code == 400 and r.json()["id"] == rid
        stored = c.get(f"{BASE}/readings/{rid}")
        assert stored.status_code == 200 and stored.json()["result"]["meter_reading_status"] == "ERROR"
    nan = '{"id": "nan-1", "metadata": {"x": NaN}, "imageBase64": "%s"}' % b64(jpeg_bytes)
    assert c.post(f"{BASE}/extract-reading", content=nan, headers={"Content-Type": "application/json"}).status_code == 400
    assert c.get(f"{BASE}/readings/nan-1").status_code == 200
    assert c.app.state.service.reader.client.calls == 0


def test_a_nul_in_a_looked_up_id_is_not_found(app_with):                                                # [NEW-DOC-07]
    c = app_with()
    assert c.get(f"{BASE}/readings/a%00b").status_code == 404
    assert c.get(f"{BASE}/readings/a%00b/image").status_code == 404


def test_photo_downloads_ignore_proxy_settings(local, monkeypatch):                                     # [NEW-DOC-08]
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")          # nothing listens there
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    assert fetch_image_url(f"http://127.0.0.1:{local.port}/x.jpg", 10**7, allow_private=True)


def test_non_energy_screens_never_take_the_kwh_decimal(app_with):                                       # [NEW-ACC-1]
    rule, prev = checks.MeterRule(1, 8, "registry", "hpl_ppsm01"), checks.Previous(value=__import__("decimal").Decimal("19712.7"))
    volts = answer("23847", screen="voltage_current_pf", unit="V", status="NOT_CUMULATIVE_SCREEN")
    assert checks.candidate(volts, rule, prev).reading == "23847"
    assert checks.candidate(answer("00197127"), None, prev).reading == "0019712.7"   # energy: from the previous


@pytest.mark.parametrize("unit", ["kW·h", "kW.h", "CUM kWh", "CUM"])
def test_odd_energy_unit_spellings_keep_the_meter_format(unit):                                         # [NEW-ACC-2]
    rule = checks.MeterRule(1, 8, "registry", "hpl_ppsm01")
    assert checks.candidate(answer("00327409", unit=unit), rule).reading == "0032740.9"
    assert checks.normalize_unit("kW·h") == "kWh"


# ---------- re-verification findings, second batch (2026-10-05) ----------
def test_deadline_timer_cuts_a_connection_whose_socket_object_was_replaced(local):                         # [REV-1]
    """HTTPS replaces the connection's socket object with a TLS one; the timer must cut the final socket."""
    import http.client
    from service.vision import net

    class LikeTLS(http.client.HTTPConnection):              # replaces self.sock after connecting, as TLS does
        def connect(self):
            super().connect()
            self.sock = socket.socket(fileno=self.sock.detach())
    watch = net.Watch(time.monotonic() + 1.0)
    conn = net._guarded(LikeTLS, watch)("127.0.0.1", local.port, timeout=30)
    conn.connect()
    conn.sock.settimeout(10)                                # so that only the overall timer can end the read
    t0 = time.monotonic()
    try:
        assert conn.sock.recv(10) == b""                    # the server never sends: only the timer ends this
    except OSError:
        pass
    finally:
        conn.close()
        watch.cancel()
    assert watch.expired and time.monotonic() - t0 < 3


def test_progressive_jpegs_get_the_png_pixel_limit():                                                     # [REV-2]
    from PIL import Image
    import io
    buf = io.BytesIO()
    Image.new("RGB", (5000, 4000), (90, 90, 90)).save(buf, format="JPEG", progressive=True)   # 20 MP
    with pytest.raises(ImageError, match="cannot be decoded at reduced size"):
        prepare_image(buf.getvalue(), 20 * 2**20, 2048, max_pixels=16_000_000)


def test_reused_id_with_another_query_link_is_refused_even_against_old_rows(app_with, local):      # [REV-3, REV-4]
    c = app_with(env={"METER_ALLOW_PRIVATE_IMAGE_URLS": "true"})
    base = f"http://127.0.0.1:{local.port}/x.jpg"
    assert post(c, id="Q-1", imageURL=f"{base}?key=photos/1.jpg").status_code == 200
    assert post(c, id="Q-1", imageURL=f"{base}?key=photos/2.jpg").status_code == 409        # key identifies a photo
    req = ReadingExtractionRequest(id="Q-1", imageURL=f"{base}?photoId=2")
    assert legacy_fingerprints(req, None) == set()          # the old format cannot tell query links apart


def test_corrected_photo_is_never_reread_even_with_reuse_off(app_with, jpeg_bytes):                          # [REV-5]
    c = app_with(env={"METER_DUPLICATE_PHOTO_HOURS": "0"})
    first = post(c, id="H0-a", imageBase64=b64(jpeg_bytes), consumerId="H0").json()
    c.post(f"{BASE}/feedback", json={"correlationId": first["result"]["correlationId"],
                                     "data": {"accurate": False, "actual": "0019800.0"}})
    again = post(c, id="H0-b", imageBase64=b64(jpeg_bytes), consumerId="H0").json()["result"]
    assert again["status"] == "NEEDS_REVIEW" and c.app.state.service.reader.client.calls == 1


def test_a_real_reread_after_an_unclear_result_is_history(app_with, jpeg_bytes):                         # [NEW-UPG-B]
    c = app_with(answer("00197127", status="UNREADABLE", illegible={3}), answer("00197127"),
                 env={"METER_DUPLICATE_PHOTO_HOURS": "0"})
    assert post(c, id="UB-a", imageBase64=b64(jpeg_bytes), consumerId="UB",
                capturedAt="2026-09-20T10:00:00+05:30").json()["result"]["status"] == "NEEDS_REVIEW"
    assert post(c, id="UB-b", imageBase64=b64(jpeg_bytes), consumerId="UB",
                capturedAt="2026-09-21T10:00:00+05:30").json()["result"]["status"] == "SUCCESS"
    prev = c.app.state.service.metadata_store.last_trusted("UB", before=datetime(2026, 9, 30, tzinfo=timezone.utc))
    assert prev is not None and str(prev.value) == "19712.7"


@pytest.mark.parametrize("ip", ["64:ff9b::a9fe:a9fe", "::a9fe:a9fe", "::ffff:0:a00:5", "2002:a9fe:a9fe::1",
                                "fec0::1"])
def test_ipv6_forms_hiding_a_private_ipv4_are_refused(ip):                                                  # [NEW-3]
    with pytest.raises(ImageError):
        images._check_ip(ip, allow_private=False)


def test_put_meters_without_a_change_keeps_photos_reusable(app_with, jpeg_bytes):                 # [BURN-R3-01]
    c = app_with()
    c.put(f"{BASE}/meters/PU", json={"meterTypeId": "hpl_ppsm01"})
    post(c, id="PU-a", imageBase64=b64(jpeg_bytes), consumerId="PU")
    c.put(f"{BASE}/meters/PU", json={"meterTypeId": "hpl_ppsm01"})          # the same settings again
    r = post(c, id="PU-b", imageBase64=b64(jpeg_bytes), consumerId="PU")
    assert r.headers.get("Duplicate-Of") == "PU-a" and c.app.state.service.reader.client.calls == 1


def test_cap_reached_only_by_readings_in_flight_is_retryable(app_with, jpeg_bytes):             # [LOAD-R3-1, -2]
    c = app_with(env={"METER_DAILY_BUDGET_USD": "0.09"})
    service = c.app.state.service
    assert service.reserve_usd == 0.05                       # more than any measured call ($0.043)
    store, now = service.metadata_store, datetime.now(timezone.utc)
    store.claim({"request_id": "IF-a", "request_timestamp": now}, stale_after=160)
    store.reserve("IF-a", now, 0.05, now - timedelta(days=1), None)          # a reading in flight
    r = post(c, id="IF-b", imageBase64=b64(jpeg_bytes))
    assert r.status_code == 429 and r.json()["error"]["retryable"] is True and r.headers["Retry-After"] == "60"


@pytest.mark.parametrize("label", ["TII kWh", "T II", "T-III", "PEAK kWh", "OFF PEAK", "OFF-PEAK", "TR1", "2.8.1"])
def test_roman_and_named_tariff_zones_are_refused(app_with, jpeg_bytes, label):                         # [NEW-05]
    c = app_with({**answer("00197127"), "screen_label": label})
    assert post(c, imageBase64=b64(jpeg_bytes)).json()["result"]["data"]["reasons"][0]["code"] == "WRONG_SCREEN"


def test_large_photo_bodies_in_one_worker_are_bounded(app_with, jpeg_bytes, another_jpeg):         # [DOCK-1, NEW-4]
    body = json.dumps({"id": "BB-1", "imageBase64": b64(jpeg_bytes)})
    c = app_with(env={"METER_MAX_INFLIGHT_BODY_BYTES": str(len(body) + 100)})   # room for one body at a time
    slow = FakeClient(answer("00197127"))
    real_read = slow.read

    def read(*a, **k):
        time.sleep(2)
        return real_read(*a, **k)
    slow.read = read
    c.app.state.service.reader.client = slow
    with ThreadPoolExecutor(2) as ex:
        first = ex.submit(post, c, id="BB-1", imageBase64=b64(jpeg_bytes))
        time.sleep(0.5)                                   # BB-1 is being read now
        second = ex.submit(post, c, id="BB-2", imageBase64=b64(another_jpeg))
        r1, r2 = first.result(), second.result()
    assert r1.status_code == 200
    assert r2.status_code == 503 and r2.json()["error"]["retryable"] is True and r2.headers["Retry-After"] == "10"
    assert post(c, id="BB-2", imageBase64=b64(another_jpeg)).status_code == 200      # admitted once room frees


def test_a_call_is_made_only_if_its_reservation_fits_under_the_cap(app_with, jpeg_bytes, another_jpeg):  # [R4B-1]
    c = app_with(env={"METER_DAILY_BUDGET_USD": "0.055"})
    assert post(c, id="CAP-1", imageBase64=b64(jpeg_bytes)).status_code == 200            # $0 + 0.05 fits; costs 0.008
    r = post(c, id="CAP-2", imageBase64=b64(another_jpeg))                                  # 0.008 + 0.05 > 0.055
    assert r.status_code == 429 and r.json()["error"]["retryable"] is False


def test_a_retried_billed_id_at_the_cap_is_told_used_up_not_retry(app_with, jpeg_bytes):             # [ST4-BUDGET-2]
    c = app_with(env={"METER_DAILY_BUDGET_USD": "0.06"})

    class CutOff:
        def read(self, *a, **k):
            raise ModelError("cut off", usage={"prompt_tokens": 2500, "completion_tokens": 300})
    c.app.state.service.reader.client = CutOff()
    assert post(c, id="RB-1", imageBase64=b64(jpeg_bytes)).status_code == 502                # billed $0.008
    c.app.state.service.metadata_store.database_service.execute(_set_cost("RB-1", 0.03))     # as if it cost more
    r = post(c, id="RB-1", imageBase64=b64(jpeg_bytes))
    assert r.status_code == 429 and r.json()["error"]["retryable"] is False                  # nothing in flight


def _set_cost(rid, usd):
    from service.api.database import extraction_data as t
    import sqlalchemy as sa
    return sa.update(t).where(t.c.request_id == rid).values(cost_usd=usd)


def test_a_consumer_id_a_database_cannot_hold_does_not_lock_the_request_id(app_with, jpeg_bytes):     # [R4B-2]
    c = app_with()
    body = json.dumps({"id": "NC-1", "consumerId": "A\u0000B", "imageBase64": b64(jpeg_bytes)})
    assert c.post(f"{BASE}/extract-reading", content=body, headers={"Content-Type": "application/json"}).status_code == 400
    assert post(c, id="NC-1", consumerId="AB", imageBase64=b64(jpeg_bytes)).status_code == 200


def test_nan_and_infinity_are_not_request_ids(app_with, jpeg_bytes):                                    # [NEW-A-3]
    c = app_with()
    for raw in ("NaN", "Infinity"):
        body = '{"id": %s, "imageBase64": "%s"}' % (raw, b64(jpeg_bytes))
        r = c.post(f"{BASE}/extract-reading", content=body, headers={"Content-Type": "application/json"})
        assert r.status_code == 400 and r.json()["id"] not in ("nan", "inf")
    assert c.app.state.service.reader.client.calls == 0 and caller_id(float("nan")) is None


def test_deeply_nested_details_are_a_400(app_with, jpeg_bytes):                                          # [NEW-UPG-D]
    c = app_with()
    nested = {}
    for _ in range(100):
        nested = {"a": nested}
    r = post(c, id="DEEP-1", imageBase64=b64(jpeg_bytes), metadata=nested)
    assert r.status_code == 400 and r.json()["id"] == "DEEP-1" and c.app.state.service.reader.client.calls == 0


def test_float_ids_are_echoed_on_refusals():                                                                 # [NEW-06]
    assert caller_id(7.5) == "7.5"


def test_upload_names_the_previous_reading_fields_it_refuses(app_with, jpeg_bytes):                     # [DOC NEW-06]
    c = app_with()
    r = c.post(f"{BASE}/extract-reading/upload", files={"image": ("m.jpg", jpeg_bytes, "image/jpeg")},
               data={"id": "UPP-1", "previousValue": "100", "previousUnit": "kWh", "previousCapturedAt": "0001-01-01"})
    assert r.status_code == 400 and "previousCapturedAt" in r.json()["error"]["errorMsg"]
    stored = c.get(f"{BASE}/readings/UPP-1").json()["result"]["request_json"]
    assert stored["previousCapturedAt"] == "0001-01-01" and stored["previousUnit"] == "kWh"


# ---------- final check round (2026-10-05) ----------
def test_jpegs_that_cannot_be_decoded_small_get_the_16_mp_limit():                                     # [ST4D-1]
    from PIL import Image
    import io
    from service.vision.images import _jpeg_scans

    def jpeg(mode="RGB", **kw):
        buf = io.BytesIO()
        Image.new(mode, (5000, 4000), (10, 20, 30, 40)[:len(mode)]).save(buf, format="JPEG", **kw)
        return buf.getvalue()
    baseline = Image.new("RGB", (5000, 4000))
    exif = baseline.getexif()
    exif[0x010F] = "camera"                               # an EXIF block with its own thumbnail segment
    buf = io.BytesIO()
    baseline.save(buf, format="JPEG", exif=exif.tobytes())
    assert _jpeg_scans(buf.getvalue())[0] == 1
    assert prepare_image(buf.getvalue(), 30 * 2**20, 2048)                 # 20 MP baseline: decoded small, accepted
    for data in (jpeg("CMYK"), jpeg(progressive=True)):
        with pytest.raises(ImageError, match="cannot be decoded at reduced size"):
            prepare_image(data, 30 * 2**20, 2048)


def test_local_use_nat64_prefix_is_never_fetched():                                                     # [REV4-6]
    with pytest.raises(ImageError, match="blocked"):
        images._check_ip("64:ff9b:1:a9fe:a9:fe00::", allow_private=True)


def test_workers_are_not_recycled_by_default(tmp_path, monkeypatch):                                     # [REV4-1]
    monkeypatch.delenv("MAX_REQUESTS", raising=False)
    import dotenv
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: None)
    spec = importlib.util.spec_from_file_location("gconf2", Path(__file__).resolve().parents[1] / "gunicorn.conf.py")
    gconf = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gconf)
    assert gconf.max_requests == 0


def test_connecting_is_bounded_by_the_overall_timer(local):                                            # [REV4-4]
    """The timer is armed as soon as the socket connects (before a proxy tunnel or TLS handshake): a connection that
    never gets past that point is still cut at the deadline. (The fake host closes its side once ours shuts down,
    which is what wakes a blocked read on Windows; on Linux the shutdown alone does.)"""
    from service.vision import net
    watch = net.Watch(time.monotonic() + 1.0)
    try:
        sock = watch.create_connection(("127.0.0.1", local.port), 30)
        sock.settimeout(30)
        t0 = time.monotonic()
        try:
            assert sock.recv(10) == b""                   # e.g. waiting for the proxy's CONNECT answer
        except OSError:
            pass
        sock.close()
        assert watch.expired and time.monotonic() - t0 < 3
    finally:
        watch.cancel()


# ---------- pre-deployment check (2026-10-06) ----------
def _crafted_jpeg(side, scans, junk=False):
    """A minimal baseline JPEG (3 components) with the given scans; the image data is all zeros."""
    import struct

    def seg(m, payload):
        return b"\xff" + bytes([m]) + struct.pack(">H", len(payload) + 2) + payload
    o = bytearray(b"\xff\xd8")
    o += seg(0xDB, b"\x00" + bytes([1] * 64))
    o += seg(0xC0, struct.pack(">BHHB", 8, side, side, 3) + bytes([1, 0x11, 0, 2, 0x11, 0, 3, 0x11, 0]))
    counts = bytes([0, 2] + [0] * 14)
    o += seg(0xC4, b"\x00" + counts + b"\x00\x01")
    o += seg(0xC4, b"\x10" + counts + b"\x00\x01")
    for n, comps in enumerate(scans):
        if junk and n > 0:
            o += seg(0xFE, b"comment") + b"STRAYBYTES"
        o += seg(0xDA, bytes([len(comps)]) + b"".join(bytes([c, 0]) for c in comps) + bytes([0, 63, 0]))
        o += bytes(64)
    o += b"\xff\xd9"
    return bytes(o)


def test_only_a_single_scan_over_every_component_decodes_small():                                     # [ST6R-01]
    from service.vision.images import _jpeg_reducible
    assert _jpeg_reducible(_crafted_jpeg(64, [[1, 2, 3]]))                 # one interleaved scan: reducible
    assert not _jpeg_reducible(_crafted_jpeg(64, [[1]]))                   # one scan over 1 of 3 components
    assert not _jpeg_reducible(_crafted_jpeg(64, [[1], [2], [3]]))         # several scans
    assert not _jpeg_reducible(_crafted_jpeg(64, [[1, 2, 3], [1]], junk=True))   # stray bytes: unknown
    with pytest.raises(ImageError, match="cannot be decoded at reduced size"):
        prepare_image(_crafted_jpeg(5000, [[1]]), 30 * 2**20, 2048)        # 25 MP, partial scan: 16 MP limit


def test_lossless_and_other_unusual_jpegs_are_refused_before_decoding():                                # [ST7-01]
    """A lossless JPEG decoded with Pillow's draft mode crashes the process; it must never get that far."""
    import struct
    from service.vision.images import _jpeg_kind

    def seg(m, payload):
        return b"\xff" + bytes([m]) + struct.pack(">H", len(payload) + 2) + payload

    def lossless(side):
        o = bytearray(b"\xff\xd8")
        o += seg(0xC3, struct.pack(">BHHB", 8, side, side, 1) + bytes([1, 0x11, 0]))
        o += seg(0xC4, b"\x00" + bytes([1] + [0] * 15) + b"\x00")
        o += seg(0xDA, bytes([1, 1, 0, 1, 0, 0]))
        o += bytes(side * side // 8) + b"\xff\xd9"
        return bytes(o)
    for side in (100, 4000):
        with pytest.raises(ImageError, match="unsupported kind of JPEG"):
            prepare_image(lossless(side), 30 * 2**20, 2048)
    assert _jpeg_kind(make_jpeg()) == 0xC0                                  # ordinary photos are still read
    assert prepare_image(make_jpeg(), 30 * 2**20, 2048)


def test_a_refusal_that_leaves_the_body_unsent_closes_the_connection():                                  # [ST7-02]
    import asyncio
    from app_factory import RequestGate
    sent = []

    async def send(message):
        sent.append(message)

    async def reply(scope, receive_, send_):
        await send_({"type": "http.response.start", "status": 413, "headers": [(b"content-type", b"x")]})
    gate = RequestGate(None, limit=10, reply=reply, deny=None, protected_prefix="/none/", key_ok=lambda k: True)
    scope = {"type": "http", "method": "POST", "path": "/x",
             "headers": [(b"content-length", b"100"), (b"expect", b"100-continue")]}
    asyncio.run(gate(scope, None, send))
    assert (b"connection", b"close") in sent[0]["headers"]
    # an admitted request that also carries the header keeps its connection           [ST9-01]
    sent.clear()

    async def app(scope_, receive_, send_):
        await send_({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"x")]})

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}
    gate = RequestGate(app, limit=1000, reply=reply, deny=None, protected_prefix="/none/", key_ok=lambda k: True)
    asyncio.run(gate(scope, receive, send))
    assert sent[0]["status"] == 200 and all(h[0] != b"connection" for h in sent[0]["headers"])


def test_refusing_deeply_nested_details_never_turns_into_a_500(app_with, jpeg_bytes, caplog):   # [ST6-1, ST6R-02]
    c = app_with()
    body = '{"id": "DN-1", "metadata": {"x": %s}, "imageBase64": "%s"}' % ("[" * 1500 + "]" * 1500, b64(jpeg_bytes))
    r = c.post(f"{BASE}/extract-reading", content=body, headers={"Content-Type": "application/json"})
    assert r.status_code == 400 and r.json()["id"] == "DN-1" and r.json()["error"]["retryable"] is False
    assert c.get(f"{BASE}/readings/DN-1").json()["result"]["meter_reading_status"] == "ERROR"
    assert b64(jpeg_bytes)[:40] not in caplog.text                         # the photo never reaches the logs


def test_a_client_waiting_for_100_continue_is_refused_without_sending_its_body(app_with):              # [ST6R-03]
    import asyncio
    from app_factory import RequestGate
    calls = []

    async def receive():
        calls.append("receive")
        return {"type": "http.request", "body": b"x", "more_body": False}

    async def reply(scope, receive_, send):
        calls.append("reply")
    gate = RequestGate(None, limit=10, reply=reply, deny=None, protected_prefix="/none/", key_ok=lambda k: True)
    scope = {"type": "http", "method": "POST", "path": "/x",
             "headers": [(b"content-length", b"100"), (b"expect", b"100-continue")]}
    asyncio.run(gate(scope, receive, None))
    assert calls == ["reply"]                                    # refused at once, the body never requested


def test_a_stalled_body_gets_a_retryable_503(monkeypatch):                                             # [ST6R-04]
    import asyncio
    import app_factory
    monkeypatch.setattr(app_factory, "BODY_PART_TIMEOUT", 0.2)
    sent = []

    async def receive():
        await asyncio.sleep(5)                                   # the client stops sending

    async def app(scope, receive_, send):
        await receive_()
        await send({"type": "http.response.start", "status": 400, "headers": []})

    async def stalled(scope, receive_, send):
        sent.append("503")
    gate = app_factory.RequestGate(app, limit=10_000, reply=None, deny=None, protected_prefix="/none/",
                                   key_ok=lambda k: True, stalled=stalled)
    asyncio.run(gate({"type": "http", "method": "POST", "path": "/x", "headers": []}, receive, lambda m: None))
    assert sent == ["503"]
