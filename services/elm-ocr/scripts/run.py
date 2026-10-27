"""
Send a meter photo to the running service from the terminal, the way the other system will call it, and show the
reading and what was saved. Each photo is one model call (about $0.023 with GPT-6 Sol, $0.011-0.022 with Claude).

This is the client. The service itself is started with src/run.py, in another terminal:

    python src/run.py

Then:

    python scripts/run.py photo.jpg                            # send a photo file
    python scripts/run.py https://other-system/api/images/123  # the service downloads the photo from this URL
    python scripts/run.py photo.jpg --via-url                  # serve photo.jpg on a local URL and send that URL,
                                                               # like the other system's image endpoint (start the
                                                               # service with METER_ALLOW_PRIVATE_IMAGE_URLS=true)
    python scripts/run.py photo.jpg --consumer PSPCL-000123 --detail village=Akalgarh --detail agentId=A7

It prints the reading, then the record the service saved (the photo, the details, the request and the response).
Only the standard library is used, so it runs without installing anything.
"""
import argparse
import base64
import http.server
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote, unquote
from uuid import uuid4

GREEN, YELLOW, RED, BOLD, RESET = "\033[32m", "\033[33m", "\033[31m", "\033[1m", "\033[0m"


def call(method, url, body=None, api_key=None):
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["X-API-Key"] = api_key
    req = urllib.request.Request(url, method=method, headers=headers,
                                 data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:            # errors come back as the same JSON envelope
        raw = e.read().decode(errors="replace")
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {"detail": raw[:300]}
    except urllib.error.URLError as e:
        sys.exit(f"{RED}Cannot reach the service at {url}: {e.reason}.{RESET} Start it first: python src/run.py")


def serve_once(path: Path):
    """Serves one file on a random local port, like the other system's image endpoint. Returns (url, server)."""
    token = uuid4().hex                                   # unique URL per run

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if unquote(self.path) != f"/images/{token}/{path.name}":
                self.send_error(404)
                return
            data = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_port}/images/{token}/{quote(path.name)}", server


def main():
    ap = argparse.ArgumentParser(description="Send a meter photo to the running service and show the result.")
    ap.add_argument("photo", help="a photo file, or an http(s) URL the service should download the photo from")
    ap.add_argument("--via-url", action="store_true", help="serve the photo file on a local URL and send the URL")
    ap.add_argument("--server", default=os.getenv("METER_SERVER", "http://localhost:8000"))
    ap.add_argument("--base-path", default="/flowvision/v1/elm")
    ap.add_argument("--api-key", default=os.getenv("METER_SERVICE_API_KEY"), help="if the service requires X-API-Key")
    ap.add_argument("--consumer", help="consumerId")
    ap.add_argument("--captured-at", help="when the photo was taken, ISO 8601, e.g. 2026-09-30T10:15:00+05:30")
    ap.add_argument("--previous", help="the previous reading, e.g. 0019650.2")
    ap.add_argument("--detail", action="append", default=[], metavar="KEY=VALUE",
                    help="any other detail the other system would send; stored with the reading (repeatable)")
    ap.add_argument("--json", action="store_true", help="print the full response and stored record as JSON")
    args = ap.parse_args()

    body = {"id": str(uuid4())}
    server = None
    if args.photo.startswith(("http://", "https://")):
        body["imageURL"] = args.photo
    else:
        path = Path(args.photo)
        if not path.is_file():
            sys.exit(f"No such file: {path}")
        if args.via_url:
            body["imageURL"], server = serve_once(path)
        else:
            body["imageBase64"] = base64.b64encode(path.read_bytes()).decode()
    if args.consumer:
        body["consumerId"] = args.consumer
    if args.captured_at:
        body["capturedAt"] = args.captured_at
    if args.previous:
        body["previousReading"] = {"value": args.previous}
    for item in args.detail:
        key, _, value = item.partition("=")
        body[key] = value

    base = args.server.rstrip("/") + args.base_path
    print(f"Sending {'URL ' + body['imageURL'] if 'imageURL' in body else path.name} to {base}/extract-reading "
          f"(one model call, about $0.023 with GPT-6 Sol, $0.011-0.022 with Claude) ...")
    t0 = time.time()
    status, resp = call("POST", f"{base}/extract-reading", body, args.api_key)
    if server:
        server.shutdown()
    if "responseCode" not in resp:
        sys.exit(f"{RED}HTTP {status}{RESET}: {resp}")

    if resp.get("responseCode") != "OK":
        err = resp.get("error") or {}
        print(f"{RED}ERROR {err.get('errorCode')}{RESET} (HTTP {status}): {err.get('errorMsg')}"
              + ("  [retryable: send it again with the same id]" if err.get("retryable") else ""))
        if err.get("errorCode") == "ERROR_09":
            print("  The service needs an API key: add --api-key (or set METER_SERVICE_API_KEY)")
        if "private or local" in str(err.get("errorMsg")):
            print("  To test with a local URL, restart the service with METER_ALLOW_PRIVATE_IMAGE_URLS=true")
    else:
        res = resp["result"]
        d = res.get("data") or {}
        action = d.get("action")
        color = GREEN if action == "ACCEPT" else (YELLOW if action == "RETAKE" else RED)
        print(f"\n  {BOLD}{d.get('meterReading')} {d.get('unit') or ''}{RESET}   {color}{action}{RESET} ({res['status']})")
        if d.get("uncertainDigits"):
            print(f"  unsure digits (positions): {d['uncertainDigits']}")
        print(f"  decimal point from: {d.get('decimalSource') or '-'}   meter: "
              f"{(d.get('meter') or {}).get('typeId') or 'unknown type'} ({d.get('meterBrand') or '?'})")
        for r in d.get("reasons") or []:
            print(f"  - {r['message']}")
        if d.get("retakeHint"):
            print(f"  Tip for the consumer: {d['retakeHint']}")
        print(f"  {time.time() - t0:.1f}s, about ${d.get('costEstimateUsd') or 0:.4f}")

    # The record is written just after the response is sent: give it a moment.
    stored = None
    for _ in range(10):
        code, row = call("GET", f"{base}/readings/{body['id']}", api_key=args.api_key)
        if code == 200:
            stored = row.get("result", row)
            break
        time.sleep(0.3)
    if stored is None:
        print(f"\n{YELLOW}No stored record found for request {body['id']}.{RESET}")
    else:
        print(f"\nSaved in the database (request id {body['id']}):")
        print(f"  status {stored.get('meter_reading_status')}, reading {stored.get('meter_reading')}, "
              f"consumer {stored.get('consumer_id') or '-'}")
        print(f"  photo: {stored.get('image_path') or 'not saved'}"
              + (f"  (view: {base}/readings/{body['id']}/image)" if stored.get("image_path") else ""))
        extras = {k: v for k, v in (stored.get("request_json") or {}).items()
                  if k not in ("id", "ts", "imageURL", "consumerId", "capturedAt", "previousReading", "metadata")}
        if extras:
            print(f"  details sent: {extras}")
        print(f"  request and response stored: {'yes' if stored.get('request_json') and stored.get('response_json') else 'no'}")
    if args.json:
        print(json.dumps({"response": resp, "stored": stored}, indent=2, ensure_ascii=False))
    return 1 if resp.get("responseCode") != "OK" else 0


if __name__ == "__main__":
    sys.exit(main())
