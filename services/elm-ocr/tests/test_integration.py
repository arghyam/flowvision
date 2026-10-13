"""The other system's side: details stored with each reading, photos pulled from its image endpoint."""
import base64
import http.server
import threading
import uuid

import pytest
from fastapi.testclient import TestClient

from app_factory import create_app
from conftest import answer
from service.vision.images import ImageError, fetch_image_url

BASE = "/meter-reader/v1"


@pytest.fixture
def client(config, make_reader, db_url):
    return TestClient(create_app(config=config, reader=make_reader(answer("00197127")), db_url=db_url,
                                 service_api_key=""))


def b64(data):
    return base64.b64encode(data).decode()


def test_details_request_and_response_are_stored(client, jpeg_bytes):
    rid = str(uuid.uuid4())
    body = {"id": rid, "imageBase64": b64(jpeg_bytes), "consumerId": "PSPCL-1", "village": "Akalgarh",
            "agentId": "A7", "metadata": {"source": "field-app"}}
    resp = client.post(f"{BASE}/extract-reading", json=body).json()
    row = client.get(f"{BASE}/readings/{rid}").json()["result"]
    assert row["request_json"]["village"] == "Akalgarh" and row["request_json"]["agentId"] == "A7"
    assert "imageBase64" not in row["request_json"]                     # the photo is kept as a file, not here
    assert row["response_json"]["result"]["data"]["meterReading"] == "0019712.7"
    assert row["response_json"]["result"]["correlationId"] == resp["result"]["correlationId"]
    assert row["metadata"] == {"source": "field-app"} and row["image_path"]


def test_failed_request_is_stored_too(client):
    rid = str(uuid.uuid4())
    resp = client.post(f"{BASE}/extract-reading", json={"id": rid, "imageBase64": b64(b"not an image"),
                                                        "consumerId": "PSPCL-2"}).json()
    assert resp["responseCode"] == "ERROR"
    row = client.get(f"{BASE}/readings/{rid}").json()["result"]
    assert row["meter_reading_status"] == "ERROR" and row["consumer_id"] == "PSPCL-2"
    assert row["response_json"]["error"]["errorCode"] == resp["error"]["errorCode"]


def test_failed_request_can_be_resent_with_the_same_id(client, jpeg_bytes):
    rid = "JS-REQ-000017"                                              # the central system's own id format
    bad = client.post(f"{BASE}/extract-reading", json={"id": rid, "imageBase64": b64(b"broken"), "consumerId": "C1"})
    assert bad.status_code == 415 and bad.json()["error"]["retryable"] is False
    ok = client.post(f"{BASE}/extract-reading", json={"id": rid, "imageBase64": b64(jpeg_bytes), "consumerId": "C1"})
    assert ok.status_code == 200 and ok.json()["result"]["status"] == "SUCCESS"
    row = client.get(f"{BASE}/readings/{rid}").json()["result"]
    assert row["meter_reading_status"] == "SUCCESS" and row["meter_reading"] == "0019712.7"


class _Source:
    """A stand-in for the other system's image endpoint: /img needs a token, /hop redirects to another host."""

    def __init__(self, image):
        self.seen_auth = []
        source = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                source.seen_auth.append((self.path, self.headers.get("Authorization")))
                if self.path == "/img":
                    if self.headers.get("Authorization") != "Bearer t0k":
                        self.send_error(401)
                        return
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(image)))
                    self.end_headers()
                    self.wfile.write(image)
                elif self.path == "/hop":            # same server, but reached as "localhost": another host name
                    self.send_response(302)
                    self.send_header("Location", f"http://localhost:{self.server.server_port}/open")
                    self.end_headers()
                else:
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(image)))
                    self.end_headers()
                    self.wfile.write(image)

            def log_message(self, *a):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"


def test_pull_from_source_endpoint_with_credentials(jpeg_bytes):
    src = _Source(jpeg_bytes)
    try:
        with pytest.raises(ImageError):                                  # no token: the source refuses
            fetch_image_url(f"{src.base}/img", 10**7, allow_private=True)
        allowed = ("127.0.0.1", "localhost")
        data = fetch_image_url(f"{src.base}/img", 10**7, allow_private=True, allowed_hosts=allowed,
                               headers={"Authorization": "Bearer t0k"})
        assert data == jpeg_bytes
        # credentials only go to allowed hosts: without an allow-list they are not sent at all
        with pytest.raises(ImageError, match="HTTP 401"):
            fetch_image_url(f"{src.base}/img", 10**7, allow_private=True, headers={"Authorization": "Bearer t0k"})
        # a redirect to another host is followed, but the token is not sent there
        fetch_image_url(f"{src.base}/hop", 10**7, allow_private=True, allowed_hosts=allowed,
                        headers={"Authorization": "Bearer t0k"})
        assert src.seen_auth[-2:] == [("/hop", "Bearer t0k"), ("/open", None)]
        # and a redirect to a host that is not allowed is refused
        with pytest.raises(ImageError, match="not in METER_IMAGE_URL_ALLOWED_HOSTS"):
            fetch_image_url(f"{src.base}/hop", 10**7, allow_private=True, allowed_hosts=("127.0.0.1",))
    finally:
        src.server.shutdown()


def test_redirect_to_private_address_is_refused(monkeypatch):
    from service.vision import images
    images._check_url("http://93.184.216.34/x.jpg", (), False)          # a public address is fine
    with pytest.raises(ImageError, match="blocked address"):
        images._CheckedRedirects((), False, []).redirect_request(
            type("R", (), {"full_url": "http://93.184.216.34/x"})(), None, 302, "", {}, "http://169.254.169.254/")
