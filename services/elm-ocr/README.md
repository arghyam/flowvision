# Electricity meter reader (GPT-6 Sol)

Reads the cumulative energy reading (kWh or kVAh) from a photo of a digital electricity meter, using OpenAI's GPT-6 Sol with checks around it. The central JalSoochak system sends a photo and gets the reading back.

- **Calling the API** (for the JalSoochak developers): [docs/INTEGRATION.md](docs/INTEGRATION.md)
- **Deploying on AWS** (ECS Fargate or EC2): [docs/DEPLOY_AWS.md](docs/DEPLOY_AWS.md)

`meterReading` holds the reading as displayed (e.g. `0019712.7`) whenever the model can see a display, with the decimal point from the meter type where it is known. The checks never withhold the reading; they set `status` and `action`, which say how far to trust it:

| Status | `action` | Meaning |
|---|---|---|
| `SUCCESS` | `ACCEPT` | The reading passed every check. |
| `UNCLEAR` | `RETAKE` | Wrong photo: not the cumulative screen (e.g. a tariff or bill screen), no unit visible, or the wrong unit. `retakeHint` says how to take a better photo. |
| `NOMETER` | `RETAKE` | No meter display is visible; `meterReading` then holds a short explanation. |
| `NEEDS_REVIEW` | `REVIEW` | The photo was hard to read (glare, an unclear or missing digit, low confidence), or the reading failed a consistency check (digit count, decimal point, previous reading). A person checks the reading against the photo; `uncertainDigits` says which digits to check (empty: check them all). |

Every reason code, with the exact `reasons[].message`, `retakeHint` and `meterReading` texts returned for it, is listed in [docs/INTEGRATION.md](docs/INTEGRATION.md#every-possible-outcome). What each request field does, and what you lose by leaving it out, is in [section 2](docs/INTEGRATION.md#2-read-a-meter-photo) there.

The API under `/flowvision/v1/elm` (and, for existing clients, `/meter-reader/v1`) uses FlowVision's envelope (`id` / `ts` / `responseCode` / `statusCode` / `result` or `error`) for every response, errors included (except the saved photo itself; `/`, `/health` and `/ready` answer with plain JSON), along with FlowVision's statuses, feedback format, config, logging, error codes, validation and database components.

## Quick start

Requirements: Python 3.12 and an OpenAI API key with GPT-6 Sol access. From this directory (`services/elm-ocr`):

```bash
cp .env.example .env                  # put OPENAI_API_KEY in it
pip install --require-hashes -r requirements.lock   # pinned with hashes, as in the image
python src/run.py                     # development server: http://localhost:8000, API docs at /docs
```

In a second terminal, send it a photo (one OpenAI call, about $0.023):

```bash
python scripts/run.py path/to/photo.jpg
python scripts/run.py path/to/photo.jpg --consumer PSPCL-000123 --detail village=Akalgarh
python scripts/run.py https://other-system.example/api/images/123     # the service downloads the photo
```

It prints the reading, how far to trust it, and what was saved (photo, details, request and response); `--json` shows everything. To test the "download from the other system" path with a local photo, start the service with `METER_ALLOW_PRIVATE_IMAGE_URLS=true` and add `--via-url`: the script serves the photo on a local URL, as the other system's image endpoint would. In PowerShell, set the variable with `$env:METER_ALLOW_PRIVATE_IMAGE_URLS="true"` before `python src/run.py`. Add `--api-key` when the service requires one.

The ways to run it:
- **Production:** gunicorn with `gunicorn.conf.py`, through the Docker image or the systemd unit `meter_reader.service`. See [docs/DEPLOY_AWS.md](docs/DEPLOY_AWS.md).
- **A production-like stack on one machine** (the service in production mode plus PostgreSQL): `docker compose up --build`.

## Production mode

With `METER_ENV=production` the service refuses to start until these are set:
- `METER_SERVICE_API_KEY` (clients send it as `X-API-Key`; at least 16 random characters);
- `METER_DB_URL` (PostgreSQL);
- `METER_IMAGE_DIR` (e.g. `s3://bucket/prefix`);
- `METER_IMAGE_URL_ALLOWED_HOSTS`.

The daily spend cap is **$25** by default (config.yaml), per India-time day; change it with `METER_DAILY_BUDGET_USD` (`none` = no cap, only on purpose). If config.yaml has no cap, production refuses to start until the variable is set.

Production mode also switches the logs to JSON lines and turns the API docs off.

These protections are always on:

| What | How |
|---|---|
| **Safe retries** | The request `id` is an idempotency key. The same id returns the stored result without a second model call, and an id still being processed gets 409 (retry shortly). The same id for a different consumer or photo is refused (409 `ERROR_15`), and so is a photo first sent as base64 and then as a link under the same id: resend an id in the form first used. After a failed attempt, the same consumer may resend the id with a corrected photo; another consumer may not. |
| **No double spend on the same photo** | The identical photo file of the same consumer, sent again with a new id within 7 days of its original read, gets that result without a model call (`METER_DUPLICATE_PHOTO_HOURS`; `0` turns off this reuse and the in-flight check below). While that photo is still being read under another id, the new request gets 409 `ERROR_12` (retryable), and its retry gets the result: a double submit pays once. The photo is read again if the consumer's meter settings really changed since (a `PUT /meters` with the same settings changes nothing). A photo whose reading a person marked wrong or corrected is never read or accepted again, whatever the setting: it comes back `NEEDS_REVIEW` (`CORRECTED_BEFORE`), at no cost. A confirmation (`accurate: true`, or an `actual` equal to the returned reading) is not a correction. |
| **Time limit** | A reading takes about 20 s and at most `METER_REQUEST_DEADLINE` (100 s). The model call is retried at most once, and only for errors OpenAI did not bill. |
| **Load** | At most `METER_MAX_CONCURRENT_READS` (8) readings at once per worker (sized for a 4 GB task with 2 workers); as many more wait up to 30 s for a slot, and beyond that a request gets 503 (retryable) instead of an unbounded queue. Raise it only with more memory. |
| **Cost** | A daily cap of **$25** (about 1,080 photos; `METER_DAILY_BUDGET_USD`) stops model calls for the rest of the India-time day once reached (429). Before each model call the request reserves $0.05 on its own row (`limits.reserve_per_call_usd`; the costliest of 384 real calls was $0.043), under a lock that every worker and instance shares, so readings in flight count; a call is made only if its reservation still fits within the cap (a cap below $0.05 allows no calls). The reservation is then replaced by the real cost, so spend can end above the cap only if a call costs more than its reservation (none has so far). When only the reservations of other readings in progress are in the way, the 429 is retryable (`Retry-After: 60`). A retried id's own billed earlier attempt counts as spent, not as in progress, so with nothing else in progress it is told the budget is used up (not retryable). Failed calls that OpenAI may still bill are counted too, and a retried failed id keeps that cost. |
| **Input** | Request bodies up to 30 MB, checked (with the API key) before the body is read, and at most 150 MB of photo request bodies held at once per worker (`METER_MAX_INFLIGHT_BODY_BYTES`, by `Content-Length`; one more gets 503, retryable). A body refused for its size (413) or for the photo body limit (503) is read and discarded first, so the client gets the error, not a connection reset; a body that stalls for 60 s gets a retryable 503 (send it again). Images up to 20 MB, and 250 megapixels (single-scan grey or colour JPEG) or 16 (PNG, WebP, and progressive, multi-scan or CMYK JPEG), with one full-size image decode at a time per process. Dates outside 2000–2100, the NUL character in any text, NaN or Infinity (in details or as the id), and details nested more than 20 levels deep are refused (400). |
| **Image downloads** | Only the allowed hosts (a listed host and its subdomains). Cloud metadata and link-local addresses are always blocked. Each download connects only to the address that was checked, so DNS rebinding cannot reach a blocked address; IPv6 links work under the same rules (an IPv4 address carried inside an IPv6 one, as in NAT64, 6to4 or Teredo, is checked too; site-local `fec0::/10` counts as private, and the local-use NAT64 prefix `64:ff9b:1::/48` is always refused), and the download timeout covers connecting (TLS handshake included) and slow headers too. Links with a user name or password are refused (400). Credentials go only to the allowed hosts, only over HTTPS, or over plain HTTP to a private address when `METER_ALLOW_PRIVATE_IMAGE_URLS=true` (never plain HTTP to a public address), and are dropped on any cross-origin redirect. Downloads ignore `HTTP_PROXY`/`HTTPS_PROXY` (the address check must apply to the image host itself). Links are logged and stored without their query string and fragment, and access-log lines carry no query strings. |
| **Records** | Written before the response is sent, for refused requests that carry an id (bad request, busy) too. A request refused before its body is read (wrong API key, a body over 30 MB, the per-worker photo body limit full) is not recorded. |

## Connecting the other system

The central system calls `POST /flowvision/v1/elm/extract-reading` for each photo with:
- `imageURL` (downloaded by the service) or `imageBase64`;
- its own `id`;
- `consumerId`, `capturedAt`, and any other details, which are stored with the reading.

The full contract, with retries, timeouts and error codes, is in [docs/INTEGRATION.md](docs/INTEGRATION.md).

## Storage

Relative paths are under `services/elm-ocr/` (Docker: `/app`), wherever the service is started from; the startup log prints them.

| What | Where | Setting |
|---|---|---|
| Readings database | `data/meter_reader.db` (SQLite) locally; PostgreSQL in production | `METER_DB_URL` |
| Photos, unchanged | `data/images/<YYYY-MM-DD>/<request id>.<ext>`, or `s3://bucket/prefix/...` (`none` = don't keep them) | `METER_IMAGE_DIR` |
| Logs | stdout (one line per record, JSON in production), plus daily files in `logs/` in development (off in production and in the Docker image; `METER_LOG_TO_FILES` turns them on or off) | `config.yaml` |

Each row of `meter_reader_extraction_data` holds:
- the details sent and `image_path`;
- the reading, unit, status, reasons and meter type;
- `request_json` (the full request, without image data) and `response_json`;
- model, time and cost;
- any feedback.

Failed requests are stored with status `ERROR`. Tables, columns and indexes are created on startup, safely even when several workers start at once. `meter_reader_db_ddl.sql` is generated from the same table definitions, for DBAs.

## API

The base path is `/flowvision/v1/elm`, the per-meter prefix the shared FlowVision ingress routes to this service. Every endpoint is also served, unchanged, under `/meter-reader/v1` for existing clients (`basepaths` in config.yaml; the same pattern as BFM's two prefixes). When `METER_SERVICE_API_KEY` is set, every endpoint under either base path needs the header `X-API-Key`; `/`, `/health`, `/ready` and the API docs (when enabled; off in production) do not.

| Endpoint | |
|---|---|
| `POST /extract-reading` | JSON. Required: exactly one of `imageURL` and `imageBase64`. Optional, but always send them: `id`, `consumerId`, `capturedAt`. Optional: `previousReading {value, unit, capturedAt}`, `metadata`, and any other details. |
| `POST /extract-reading/upload` | The same as multipart. Required: an `image` file. Optional, but always send them: `id`, `consumerId`, `capturedAt`. Optional: `previousValue`, `previousUnit`, `previousCapturedAt`, `metadata` (JSON text); other form fields are ignored. An invalid field is reported under its form field name. |
| `POST /feedback` | `{"correlationId": "...", "data": {"accurate": false, "actual": "0019712.7"}}`; `accurate` may be left out when `actual` is sent. A corrected reading (`actual`), or a confirmed (`accurate: true`) SUCCESS or REVIEW reading, becomes the consumer's trusted previous reading. A REVIEW reading with a `?`, or with no number, needs an `actual`. |
| `GET` / `PUT /meters/{consumerId}` | Per-consumer settings: `meterTypeId`, `decimals`, `digits`, `billingUnit` (kWh or kVAh). |
| `GET /meter-types` | The known meter types. |
| `GET /readings/{requestOrCorrelationId}` | The stored record, in `result`. |
| `GET /readings/{requestOrCorrelationId}/image` | The saved photo. |
| `GET /health` | Liveness, with no dependencies: for the load balancer and container health checks. |
| `GET /ready` | Readiness: 503 if the database is unreachable. |

The response fields and all error codes (with which ones are retryable) are in [docs/INTEGRATION.md](docs/INTEGRATION.md).

Errors carry `errorCode`, `errorMsg` and `retryable`, with the HTTP status equal to `statusCode`. `METER_ERRORS_AS_HTTP_200=true` restores FlowVision's HTTP 200 for everything.

## How a photo is checked

1. **Prepare.** The image is validated (JPEG/PNG/WebP; only baseline or progressive JPEGs, as cameras and phones write, so lossless and other unusual JPEGs are refused; at most 20 MB, and at most 250 megapixels for a single-scan grey or colour JPEG, or 16 for PNG, WebP and progressive, multi-scan or CMYK JPEGs), rotated according to its EXIF data, and shrunk to at most 2048 px. Large single-scan JPEGs are decoded at reduced size (the longer side still at least 3/4 of 2048).
2. **Read.** GPT-6 Sol reads it with the strict prompt in `src/service/vision/prompt.py` (high reasoning effort, high image detail). The model:
   - reads digit by digit, flagging any digit it can't read;
   - copies the LCD label and the meter's printed make and model code;
   - gives a best guess for unclear digits and the decimal places.
3. **Per-read checks** (`src/service/vision/checks.py`):
   - It must be the cumulative screen. A label marking a tariff total (T1, T01, T.1, T_1, T–1, kWhT1, TII, T-III, TR1, TOD, TOU, TARIFF, RATE 1, PEAK, OFF PEAK, Z01, 1.8.1), an export register (2.8.x), a past bill (01 BILL, 01 B, a bare 01, HIST), maximum demand (MD, MDkW, MAX DEMAND) or a prepaid balance (INR, ₹) means the wrong screen.
   - On a non-energy screen (voltage, Hz, maximum demand, date), `meterReading` keeps the decimal point shown, not the meter type's kWh format.
   - Every digit must be legible, the digit count certain, and the digit list and the display text must agree.
   - The confidence must be at least 0.90.
   - The unit must be visible and be kWh or kVAh.
   - Failing only the legibility or confidence checks sends the photo to review, with the model's reading. The wrong screen, or a missing or wrong unit on a clear read, means a retake.
4. **Decimal point and digit count from the meter type** (`src/service/vision/meter_types.py`), never from the model's view of a tiny dot.
   - Each consumer setting wins.
   - A setting left empty comes from the consumer's meter type, else from the type identified by its printed **model code** (never the brand alone).
   - For an unknown meter, only a clearly visible point counts.
5. **Unit check.** If the consumer's billing unit is set, a clear reading of the other unit is `UNCLEAR`. A hard-to-read photo goes to review whatever its unit, so the reviewer checks `unit` too.
6. **Previous-reading check.** A reading lower than the last trusted reading (the newest taken at or before this photo's capture time), or more than 300 per day above it, is flagged for review. A reused answer (one that shares an earlier request's `correlationId`) never counts as a previous reading; a real re-read of the same photo, e.g. after an earlier `UNCLEAR` result, does. Trusted readings are:
   - `SUCCESS` readings not marked wrong;
   - a corrected `actual`;
   - `accurate: true` on a SUCCESS or REVIEW reading (a reading with a `?`, or no number at all, needs an `actual`).
7. **Cross-check or clear read.** A dropped digit or shifted decimal point is caught by the meter type's digit count or by a previous reading. Without either, a **clear read** (every check passed, confidence ≥ 0.95, `METER_CLEAR_READ_CONFIDENCE`) is still accepted; anything less is flagged `NO_CROSS_CHECK`.

**Evidence so far** (hand-reviewed photos):
- On the 100 trial photos (`../trial/`), every ACCEPT was correct except one bill-history screen (labelled "1. bILL kW·h"), which the current prompt now declines as another screen (step 3). The returned reading was right on 36 of 51 readable photos, decimal point included. Most misses were glare photos, with the wrong digit flagged in `uncertainDigits`. The rules were tuned on these same photos.
- On 268 fresh photos (2026-10-02, $6.17), 20 of 21 ACCEPT readings were right (95.2%). The miss, PB_harrion-tapprian, was a clear-photo misread: 0016183.1 for 0016189.1, at confidence 0.98. The returned reading was right on 87 of 146 readable photos, and 17 Assam tariff or bill screens that the hand review counted as readings were rightly declined. A replay of all saved answers under the rules of that date changed no decision. Since then, hard-to-read photos go to review instead of retake; that changes no ACCEPT.

21 ACCEPT readings are too few to support the 95% target; about 60 with zero errors are needed.

## Known meter types

| id | Meter | Display |
|---|---|---|
| `hpl_ppsm01` | HPL PPSM 01 smart meter (PSPCL) | 8 digits, 1 decimal (`0019712.7`) |
| `hpl_ppem26` | HPL PPEM 26 (PSPCL) | 7 digits, 1 decimal |
| `lt_er300` | L&T / Schneider ER300 (APDCL) | 8 digits, no decimal point (`00018103`) |
| `lt_em101` | L&T / Schneider EM101+ (APDCL) | 1 decimal; digit count varies (`2344.7`) |
| `genus_saksham345` | Genus SAKSHAM-345 (APDCL) | 8 digits, 1 decimal (`0032740.9`) |
| `visiontek_37tm` | Visiontek 37TM (Punjab) | 8 digits, 1 decimal |
| `avon_asem306` | Avon ASEM-306 (Punjab) | 8 digits, 1 decimal |
| `aew_aplsm346` | AEW APL-sm346 (Punjab) | 1 decimal; digit count not fixed |

Add types in a JSON file named by `METER_TYPES_FILE` (format in `meter_types.py`), or set one consumer's meter with `PUT /meters/{consumerId}`.

## Configuration

All settings are in `src/conf/config.yaml` (or the file named by `CONFIG_PATH`), most with the environment variable that overrides it named next to it; a few (the image size limits, the per-call reservation, the download timeout and the prices) have none. Secrets and per-deployment values are environment variables, all listed in `.env.example`. The main ones:

| Variable | Default | Purpose |
|---|---|---|
| `OPENAI_API_KEY` | required | |
| `METER_ENV` | `development` | `production` enforces the settings above, JSON logs, docs off. |
| `METER_SERVICE_API_KEY` | unset | Clients send it as `X-API-Key`; comma-separate two keys to rotate. |
| `METER_DB_URL`, `METER_IMAGE_DIR` | SQLite, `data/images` | Storage. The other `METER_DB_*` settings (pool size, timeouts) are listed in `.env.example` and DEPLOY_AWS.md. |
| `METER_IMAGE_URL_ALLOWED_HOSTS`, `METER_IMAGE_URL_HEADERS`, `METER_ALLOW_PRIVATE_IMAGE_URLS` | any public host, none, `false` | Downloading photos from the central system. |
| `METER_DAILY_BUDGET_USD` | `25` | Daily model spend limit in USD (`none` = no cap). |
| `METER_DUPLICATE_PHOTO_HOURS` | `168` | The same photo of the same consumer within this is answered from the earlier result; `0` = no reuse (a corrected photo still goes to review). |
| `METER_REQUEST_DEADLINE`, `METER_MAX_CONCURRENT_READS`, `METER_BUSY_WAIT_SECONDS` | `100`, `8`, `30` | Time limit per reading; readings at once per worker (raise it only with more memory); wait for a free slot. |
| `METER_MAX_BODY_BYTES`, `METER_MAX_INFLIGHT_BODY_BYTES` | 30 MB, 150 MB | Largest request body; photo request bodies held at once per worker (by `Content-Length`). |
| `METER_MIN_CONFIDENCE`, `METER_CLEAR_READ_CONFIDENCE`, `METER_MAX_DAILY_INCREASE` | `0.90`, `0.95`, `300` | Check thresholds. |
| `METER_MODEL`, `METER_REASONING_EFFORT` | `gpt-6-sol`, `high` | Only this combination has been tested. |

**Cost and speed:** about $0.023 and 20 s per photo, almost all of it waiting on OpenAI. Each response includes `costEstimateUsd`.

## Tests and evaluation

```bash
pip install -r requirements-dev.txt
python -m pytest -q                               # 228 tests, fake model, no cost (3 run only on PostgreSQL)
METER_TEST_DB_URL=postgresql+psycopg2://user@localhost:5432/meter_test METER_DB_NULL_POOL=true python -m pytest -q
python scripts/evaluate.py --yes --limit 10       # real API on 10 hand-checked trial photos (~$0.23)
python scripts/evaluate.py --yes                  # all 100 (~$2.30); results go to data/evaluation.jsonl
python scripts/replay.py data/evaluation.jsonl    # re-score saved answers under the current rules (free)
```

GitHub Actions (`.github/workflows/image-build-elm-ocr.yaml`) runs the tests (on PostgreSQL) for every pull request
and push to `main` that changes this folder. On `main`, once they pass, it builds this folder's Docker image and pushes
it to ECR as `meter-reader`. To run the tests in the image locally:
`docker build --target test -t meter-reader-test . && docker run --rm meter-reader-test`.

`evaluate.py` saves the model's full answers and resumes where it stopped. Point `--review` and `--images` at a fresh, hand-checked set large enough to give about 60 ACCEPT readings with zero errors (several hundred photos at the current accept rate) to confirm the 95% target before going live.

## Code layout

```
src/
  run.py            development server; production runs routes:app with gunicorn (gunicorn.conf.py)
  app_factory.py    builds the FastAPI app: endpoints, auth, error envelope, body-size limit, health checks
  conf/             config.yaml, settings and production checks (config.py), logging (logging.py)
  error/, models/, validation/    error codes, request/response models, image validation
  service/api/      request handling (reading_service), limits (guards), storage (metadata_service, database,
                    image_store: local folder or S3)
  service/vision/   the reader, with no web or database code: prompt, openai_client, net, images, meter_types,
                    checks, reader, settings
scripts/            run.py (send a photo to the service), evaluate.py (real-API evaluation),
                    replay.py (free re-scoring of saved answers)
tests/              pytest suite with a fake model client
docs/               INTEGRATION.md (API contract), DEPLOY_AWS.md (deployment)
```

## Privacy

Photos are sent to OpenAI; approval for this has been given. Every photo is also kept in `data/images/` or S3, for research and for checking readings. File names hold no consumer details; the database links photo, consumer and reading. These are consumer photos:
- never publish or share them;
- restrict access to the folder or bucket;
- set a retention period (S3 lifecycle rule);
- the logs contain consumer ids.
