# ELM OCR: electricity meter reader

This service reads the cumulative energy reading (kWh or kVAh) from a photo of a digital electricity meter. The central JalSoochak system sends one photo per request and gets the reading back, with a status that says how far to trust it.

The reading itself comes from a vision model: OpenAI's GPT-6 Sol (the default) or Anthropic's Claude Sonnet 5.5, chosen by one setting. Deterministic checks around the model decide whether a reading can be used as it is, needs a person to confirm it, or needs a new photo.

It is one service in the FlowVision repository: `services/bfm-ocr` reads bulk flow (water) meters, and this one, `services/elm-ocr`, reads electricity meters. Each service has its own code, dependencies and Docker image.

## Where to read what

| You want to... | Read |
|---|---|
| Understand the service, run it locally, or change the code | this README |
| Call the API from another system (request fields, every response, errors, retries) | [docs/INTEGRATION.md](docs/INTEGRATION.md) |
| Deploy and operate it (AWS resources, settings, alarms, limits, runbook) | [docs/DEPLOY_AWS.md](docs/DEPLOY_AWS.md) |
| Deploy on Kubernetes, or understand the repository layout | [ADR-001](../../docs/adr/ADR-001-multi-service-layout.md) |
| See every setting | `src/conf/config.yaml` (each setting names its environment variable) and `.env.example` |

## How it works

For each photo:

1. **Receive.** The caller sends the photo as a link (`imageURL`, downloaded by the service) or as base64 (`imageBase64`), with its own request `id`, the `consumerId` and the time the photo was taken.
2. **Prepare.** The image is validated, rotated upright and shrunk to at most 2048 px.
3. **Read.** The model reads the display one digit at a time with a strict prompt (`src/service/vision/prompt.py`). It also reports the screen label, the unit, the meter's printed make and model, which digits it is unsure of, and its confidence.
4. **Check.** Code in `src/service/vision/checks.py` checks the answer: the right screen, legible digits, the unit, the digit count and decimal point for that meter type, and the consumer's previous reading.
5. **Answer.** The service returns the reading with a status and an action, and stores the request, the photo and the result.
6. **Feedback.** A person can confirm or correct the reading (`POST /feedback`). A confirmed or corrected reading becomes the consumer's trusted previous reading, which the next photo is checked against.

The reading is returned whenever the model saw a display, whatever the status. The status says what to do with it:

| `status` | `action` | Meaning |
|---|---|---|
| `SUCCESS` | `ACCEPT` | The reading passed every check. |
| `NEEDS_REVIEW` | `REVIEW` | The photo was hard to read (glare, an unclear or possibly missing digit, low confidence), or the reading failed a consistency check (digit count, decimal point, previous reading). A person checks the reading against the photo; `uncertainDigits` says which digits to check (empty: check them all). |
| `UNCLEAR` | `RETAKE` | Wrong photo: not the cumulative screen (e.g. a tariff, bill or history screen), no unit visible, or the wrong unit. `retakeHint` says how to take a better photo. |
| `NOMETER` | `RETAKE` | No meter display is visible; `meterReading` then holds a short explanation. |

Every reason code, with the exact texts returned for it, is in [docs/INTEGRATION.md](docs/INTEGRATION.md#every-possible-outcome).

## Key terms

| Term | Meaning |
|---|---|
| **Reading** | The cumulative energy total on the display, kept as text exactly as shown (`"0019712.7"`), with leading zeros. |
| **Consumer** | The meter's account, identified by the caller's `consumerId`. Settings and history are kept per consumer. |
| **Request id** | The caller's `id` for one photo. It is also the retry key: the same id returns the stored result instead of reading the photo again. |
| **Correlation id** | The id of one result (`correlationId`), used to send feedback on it. |
| **Retake** | The photo cannot give the right reading: ask for a new photo. |
| **Review** | The photo may give the right reading, but a person must confirm or correct it. |
| **Screen label** | The small text on the display next to the number (e.g. `CUM kWh`, `T1`, `H1`). Some labels mark a screen other than the total. |
| **Meter type** | A known meter model (e.g. `hpl_ppsm01`) with a fixed digit count and decimal position. |
| **Trusted previous reading** | The latest reading of the consumer that was accepted, confirmed or corrected; new readings are compared with it. |
| **Provider** | Which company's model reads the photo: `openai` or `claude` (`METER_PROVIDER`). |

## Quick start

Requirements: Python 3.12, and an OpenAI API key with GPT-6 Sol access (or an Anthropic API key, with `METER_PROVIDER=claude`). Run every command from this folder (`services/elm-ocr`):

```bash
cp .env.example .env                                  # then put your key in it
pip install --require-hashes -r requirements.lock     # pinned with hashes, as in the Docker image
python src/run.py                                     # development server on http://localhost:8000, API docs at /docs
```

In a second terminal, send it a photo (one model call: about $0.023 with GPT-6 Sol, about $0.015 with Claude):

```bash
python scripts/run.py path/to/photo.jpg
python scripts/run.py path/to/photo.jpg --consumer PSPCL-000123 --detail village=Akalgarh
python scripts/run.py https://other-system.example/api/images/123     # the service downloads the photo
```

The script prints the reading, how far to trust it, and what was saved; `--json` shows the whole response. Add `--api-key` when the service requires one. To test the "download from the other system" path with a local photo, start the service with `METER_ALLOW_PRIVATE_IMAGE_URLS=true` and add `--via-url`: the script then serves the photo on a local URL (in PowerShell, set `$env:METER_ALLOW_PRIVATE_IMAGE_URLS="true"` before starting the service).

You can also call the API from Postman or curl, e.g. `POST http://localhost:8000/flowvision/v1/elm/extract-reading/upload` with a form-data file field `image`.

Other ways to run it:
- **Production:** gunicorn with `gunicorn.conf.py`, in the Docker image (ECS, EC2 or Kubernetes) or with the systemd unit `meter_reader.service` (EC2). See [docs/DEPLOY_AWS.md](docs/DEPLOY_AWS.md).
- **A production-like stack on one machine** (production mode plus PostgreSQL): `docker compose up --build`.

## Common tasks

**Switch the model (OpenAI or Claude).** Set `METER_PROVIDER=openai` or `claude` in `.env` (or the deployment's environment), with that provider's key (`OPENAI_API_KEY` or `ANTHROPIC_API_KEY`), and restart. Both use the same prompt, answer format, checks, budget and storage, and each reading records the model that produced it (`model`). Only the chosen model reads a photo; there is no fallback to another model. The service refuses to start if the key is missing or `METER_MODEL` names the other provider's model.

**Add a meter type.** Built-in types are in `src/service/vision/meter_types.py` (see [Known meter types](#known-meter-types)). Add a type there, or without a code change in a JSON file named by `METER_TYPES_FILE`:
```json
[{"id": "genus_xyz", "description": "Genus XYZ-100, 8 digits, 1 decimal", "decimals": 1, "digits": 8, "patterns": ["\\bXYZ\\s*-?100\\b"]}]
```
Patterns are matched against the model code the model copies from the meter body, never the brand alone. Use only formats confirmed on real photos. Add `"tariff_indicator": true` only for a meter whose total screen also shows the tariff zone in force next to `CUM` (as `T1 CUM kWh`), as the manufacturer's manual or real photos show. A single consumer's meter can also be set with `PUT /meters/{consumerId}`.

**Reject another screen label.** Labels that mark a screen other than the total are in `NOT_TOTAL_LABEL` in `src/service/vision/checks.py`. Add the pattern there and add examples to `test_label_of_a_non_total_screen_overrides_the_model` in `tests/test_checks.py`, including labels that must still pass (the unit's own `kW h`, the `R Y B` phase indicator).

**Change a threshold.** The minimum confidence (`METER_MIN_CONFIDENCE`, 0.90), the confidence for accepting without a cross-check (`METER_CLEAR_READ_CONFIDENCE`, 0.95) and the largest daily increase (`METER_MAX_DAILY_INCREASE`, 300) are settings. To see the effect on photos already evaluated, without paying again: `python scripts/replay.py data/evaluation.jsonl --images <photos> --min-confidence 0.85`.

**Change the prompt.** Edit `src/service/vision/prompt.py` and update `PROMPT_VERSION`, which is stored with every reading. The prompt was tuned on hand-checked photos, so re-run `scripts/evaluate.py` on a hand-checked set before deploying a change: `replay.py` cannot measure prompt changes, because it reuses the saved answers.

**Run the tests.** See [Tests and evaluation](#tests-and-evaluation).

## API

The base path is `/flowvision/v1/elm`, the prefix the shared FlowVision ingress sends to this service. Every endpoint is also served, unchanged, under `/meter-reader/v1` for clients that already use it (`basepaths` in `config.yaml`). When `METER_SERVICE_API_KEY` is set, every endpoint under either base path needs the header `X-API-Key`; `/`, `/health`, `/ready` and the API docs (when enabled; off in production) do not.

| Endpoint | |
|---|---|
| `POST /extract-reading` | JSON. Required: exactly one of `imageURL` and `imageBase64`. Optional, but always send them: `id`, `consumerId`, `capturedAt`. Optional: `previousReading {value, unit, capturedAt}`, `metadata`, and any other details, which are stored with the reading. |
| `POST /extract-reading/upload` | The same as multipart. Required: an `image` file. Optional, but always send them: `id`, `consumerId`, `capturedAt`. Optional: `previousValue`, `previousUnit`, `previousCapturedAt`, `metadata` (JSON text); other form fields are ignored. |
| `POST /feedback` | `{"correlationId": "...", "data": {"accurate": false, "actual": "0019712.7"}}`; `accurate` may be left out when `actual` is sent. A corrected reading (`actual`), or a confirmed (`accurate: true`) SUCCESS or REVIEW reading, becomes the consumer's trusted previous reading. A REVIEW reading with a `?`, or with no number, needs an `actual`. |
| `GET` / `PUT /meters/{consumerId}` | Per-consumer settings: `meterTypeId`, `decimals`, `digits`, `billingUnit` (kWh or kVAh). |
| `GET /meter-types` | The known meter types. |
| `GET /readings/{requestOrCorrelationId}` | The stored record, in `result`. |
| `GET /readings/{requestOrCorrelationId}/image` | The saved photo. |
| `GET /health` | Liveness, with no dependencies: for load balancer and container health checks. |
| `GET /ready` | Readiness: 503 if the database is unreachable. |

Every response under the base paths uses FlowVision's envelope (`id`, `ts`, `responseCode`, `statusCode`, and `result` or `error`); `/`, `/health`, `/ready` and the saved photo do not. Errors carry `errorCode`, `errorMsg` and `retryable`, with the HTTP status equal to `statusCode`; `METER_ERRORS_AS_HTTP_200=true` switches to FlowVision's style of HTTP 200 for everything. The full contract is in [docs/INTEGRATION.md](docs/INTEGRATION.md).

## How a photo is checked

1. **Prepare.** The image is validated: JPEG, PNG or WebP; only baseline or progressive JPEGs (as cameras and phones write them); at most 20 MB; at most 250 megapixels for a single-scan grey or colour JPEG, or 16 for PNG, WebP and progressive, multi-scan or CMYK JPEGs. It is rotated according to its EXIF data and shrunk to at most 2048 px. Large single-scan JPEGs are decoded at reduced size (the longer side still at least 3/4 of 2048).
2. **Read.** The model in use (GPT-6 Sol with high reasoning effort and high image detail, or Claude Sonnet 5.5 with high effort; only that model) reads it with the strict prompt in `src/service/vision/prompt.py`. The model:
   - reads digit by digit, flagging any digit it can't read;
   - copies the LCD label and the meter's printed make and model code;
   - gives a best guess for unclear digits and the decimal places.
3. **Per-read checks** (`src/service/vision/checks.py`):
   - It must be the cumulative screen. These labels mean the wrong screen:
     - a tariff or time-of-day zone total (T1, T01, T.1, T_1, T–1, kWhT1, TII, T-III, TR1, TOD, TOU, TARIFF, RATE 1, PEAK, OFF PEAK, Z01, E1...E8, L&T's C0...C7, OBIS 1.8.1 and 9.8.1). Exceptions: a sum of zones (`C (T1+T2) kWh`) is the total; and on a meter type with `tariff_indicator` (Genus SAKSHAM-345, L&T ER300), a T1...T8 next to `CUM` (or L&T's lone `C`) is the time zone in force, lit on every screen (`L1 L2 L3 T1 CUM kWh`), not a tariff label;
     - an export or reverse register (2.8.x, 10.8.x, EXPORT, EXP, REV); a NET or IMPORT total (IMP, IP-Cu) is accepted;
     - a past bill, billing period or month (01 BILL, 01 B, a bare 01, HIST, HIS, BP, BP 1, LAST, PREV, MON, MONTH, MTD, M1...M12, OBIS 1.8.0*01);
     - daily or period consumption (DAY, TODAY, DAILY, CONS, CONSUMPTION, USAGE) and diesel-generator energy (DG);
     - the high-resolution screen (HR, HI-RES, HIGH RES), which drops the leading digits;
     - a history or billing register: H alone or with a number, and B with a number (H, H1, H-1, H01, B1, B 1, B01, up to 12). The h of the unit (kWh, kW h) and the R Y B phase indicator, including a lone B, are not these labels;
     - maximum demand (MD, MDkW, MAX DEMAND);
     - a prepaid balance (INR, ₹).
   - On a non-energy screen (voltage, Hz, maximum demand, date), `meterReading` keeps the decimal point shown, not the meter type's kWh format.
   - Every digit must be legible, the digit count certain, and the digit list and the display text must agree.
   - The confidence must be at least 0.90.
   - The unit must be visible and be kWh or kVAh.
   - The unit must agree with the LCD label the model copied: a kWh reading beside a label naming only kVAh (or kVArh), or the other way round, goes to review (`BAD_OUTPUT`). A label listing several units (a printed legend such as `kWh kVAh kVArh`) decides nothing.
   - Failing only the legibility or confidence checks sends the photo to review, with the model's reading. The wrong screen, or a missing or wrong unit on a clear read, means a retake.
4. **Decimal point and digit count from the meter type** (`src/service/vision/meter_types.py`), never from the model's view of a tiny dot.
   - Each consumer setting wins.
   - A setting left empty comes from the consumer's meter type, else from the type identified by its printed **model code** (never the brand alone).
   - For an unknown meter, only a clearly visible point counts.
5. **Unit check.** If the consumer's billing unit is set, a clear reading of the other unit is `UNCLEAR`. A hard-to-read photo goes to review whatever its unit, so the reviewer checks `unit` too.
6. **Previous-reading check.** A reading lower than the last trusted reading (the newest taken at or before this photo's capture time), or more than 300 per day above it, goes to review. A reused answer (one that shares an earlier request's `correlationId`) never counts as a previous reading; a real re-read of the same photo, e.g. after an earlier `UNCLEAR` result, does. Trusted readings are:
   - `SUCCESS` readings not marked wrong;
   - a corrected `actual`;
   - `accurate: true` on a SUCCESS or REVIEW reading (a reading with a `?`, or no number at all, needs an `actual`).
7. **Cross-check or clear read.** A dropped digit or shifted decimal point is caught by the meter type's digit count or by a previous reading. Without either, a **clear read** (every check passed, confidence ≥ 0.95, `METER_CLEAR_READ_CONFIDENCE`) is still accepted; anything less goes to review as `NO_CROSS_CHECK`.

## Configuration

All settings are in `src/conf/config.yaml` (or the file named by `CONFIG_PATH`), most with the environment variable that overrides them named next to them; a few (the image size limits, the per-call reservation, the download timeout and the prices) have none. Secrets and per-deployment values are environment variables, all listed in `.env.example`. The main ones:

| Variable | Default | Purpose |
|---|---|---|
| `METER_PROVIDER` | `openai` | The model that reads the photos: `openai` (GPT-6 Sol) or `claude` (Claude Sonnet 5.5). |
| `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` | required | The key of the provider in use. With an Anthropic key that is not scoped to a workspace (`sk-ant-usr...`), also set `ANTHROPIC_WORKSPACE_ID`. |
| `METER_ENV` | `development` | `production` enforces the settings in [Production mode](#production-mode), JSON logs, docs off. |
| `METER_SERVICE_API_KEY` | unset | Clients send it as `X-API-Key`; comma-separate two keys to rotate. |
| `METER_DB_URL`, `METER_IMAGE_DIR` | SQLite, `data/images` | Storage. The other `METER_DB_*` settings (pool size, timeouts) are listed in `.env.example` and DEPLOY_AWS.md. |
| `METER_IMAGE_URL_ALLOWED_HOSTS`, `METER_IMAGE_URL_HEADERS`, `METER_ALLOW_PRIVATE_IMAGE_URLS` | any public host, none, `false` | Downloading photos from the central system. |
| `METER_DAILY_BUDGET_USD` | `25` | Daily model spend limit in USD (`none` = no cap). |
| `METER_DUPLICATE_PHOTO_HOURS` | `168` | The same photo of the same consumer within this is answered from the earlier result; `0` = no reuse (a corrected photo still goes to review). |
| `METER_REQUEST_DEADLINE`, `METER_MAX_CONCURRENT_READS`, `METER_BUSY_WAIT_SECONDS` | `100`, `8`, `30` | Time limit per reading; readings at once per worker (raise it only with more memory); wait for a free slot. |
| `METER_MAX_BODY_BYTES`, `METER_MAX_INFLIGHT_BODY_BYTES` | 30 MB, 150 MB | Largest request body; photo request bodies held at once per worker (by `Content-Length`). |
| `METER_MIN_CONFIDENCE`, `METER_CLEAR_READ_CONFIDENCE`, `METER_MAX_DAILY_INCREASE` | `0.90`, `0.95`, `300` | Check thresholds. |
| `METER_MODEL`, `METER_REASONING_EFFORT` | `gpt-6-sol`, `high` (Claude: `claude-sonnet-5-5`, `high`) | The model and effort of the provider in use. |

**Cost and speed.** GPT-6 Sol: about $0.023 and 10–30 s per photo, almost all of it the model thinking. Claude Sonnet 5.5 (same price per token, $2 / $10 per million): a median of $0.014 and 4 s per photo (highest $0.022, slowest 15 s) over three runs of the 268 hand-checked photos. Both models have been evaluated on those photos; see [Accuracy so far](#accuracy-so-far). Each response includes `costEstimateUsd`.

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

Per-consumer settings are in `meter_reader_profiles`. Failed requests are stored with status `ERROR`. Tables, columns and indexes are created on startup, safely even when several workers start at once; `meter_reader_db_ddl.sql` holds the same schema for DBAs.

## Production mode

With `METER_ENV=production` the service refuses to start until these are set:
- `METER_SERVICE_API_KEY` (clients send it as `X-API-Key`; at least 16 random characters);
- `METER_DB_URL` (PostgreSQL);
- `METER_IMAGE_DIR` (e.g. `s3://bucket/prefix`);
- `METER_IMAGE_URL_ALLOWED_HOSTS`.

The daily spend cap is **$25** by default (`config.yaml`), per India-time day; change it with `METER_DAILY_BUDGET_USD` (`none` = no cap, only on purpose). If `config.yaml` has no cap, production refuses to start until the variable is set. Production mode also switches the logs to JSON lines and turns the API docs off.

## Built-in protections

These are always on:

| What | How |
|---|---|
| **Safe retries** | The request `id` is an idempotency key. The same id returns the stored result without a second model call, and an id still being processed gets 409 (retry shortly). The same id for a different consumer or photo is refused (409 `ERROR_15`), and so is a photo first sent as base64 and then as a link under the same id: resend an id in the form first used. After a failed attempt, the same consumer may resend the id with a corrected photo; another consumer may not. |
| **No double spend on the same photo** | The identical photo file of the same consumer, sent again with a new id within 7 days of its original read, gets that result without a model call (`METER_DUPLICATE_PHOTO_HOURS`; `0` turns off this reuse and the in-flight check below). While that photo is still being read under another id, the new request gets 409 `ERROR_12` (retryable), and its retry gets the result: a double submit pays once. The photo is read again if the consumer's meter settings really changed since (a `PUT /meters` with the same settings changes nothing). A photo whose reading a person marked wrong or corrected is never read or accepted again, whatever the setting: it comes back `NEEDS_REVIEW` (`CORRECTED_BEFORE`), at no cost. A confirmation (`accurate: true`, or an `actual` equal to the returned reading) is not a correction. |
| **Time limit** | A reading takes 10–30 s with GPT-6 Sol (about 4 s with Claude, at most about 15 s seen) and at most `METER_REQUEST_DEADLINE` (100 s). The model call is retried at most once, and only for errors the provider did not bill. |
| **Load** | At most `METER_MAX_CONCURRENT_READS` (8) readings at once per worker (sized for a 4 GB task with 2 workers); as many more wait up to 30 s for a slot, and beyond that a request gets 503 (retryable) instead of an unbounded queue. Raise it only with more memory. |
| **Cost** | A daily cap of **$25** (about 1,080 photos at GPT-6 Sol's cost; `METER_DAILY_BUDGET_USD`) stops model calls for the rest of the India-time day once reached (429). Before each model call the request reserves $0.05 on its own row (`limits.reserve_per_call_usd`; the costliest of 384 real calls was $0.043), under a lock that every worker and instance shares, so readings in flight count; a call is made only if its reservation still fits within the cap (a cap below $0.05 allows no calls). The reservation is then replaced by the real cost, so spend can end above the cap only if a call costs more than its reservation (none has so far). When only the reservations of other readings in progress are in the way, the 429 is retryable (`Retry-After: 60`). A retried id's own billed earlier attempt counts as spent, not as in progress, so with nothing else in progress it is told the budget is used up (not retryable). Failed calls that the provider may still bill are counted too, and a retried failed id keeps that cost. |
| **Input** | Request bodies up to 30 MB, checked (with the API key) before the body is read, and at most 150 MB of photo request bodies held at once per worker (`METER_MAX_INFLIGHT_BODY_BYTES`, by `Content-Length`; one more gets 503, retryable). A body refused for its size (413) or for the photo body limit (503) is read and discarded first, so the client gets the error, not a connection reset; a body that stalls for 60 s gets a retryable 503 (send it again). Images up to 20 MB, and 250 megapixels (single-scan grey or colour JPEG) or 16 (PNG, WebP, and progressive, multi-scan or CMYK JPEG), with one full-size image decode at a time per process. Dates outside 2000–2100, the NUL character in any text, NaN or Infinity (in details or as the id), and details nested more than 20 levels deep are refused (400). |
| **Image downloads** | Only the allowed hosts (a listed host and its subdomains). Cloud metadata and link-local addresses are always blocked. Each download connects only to the address that was checked, so DNS rebinding cannot reach a blocked address; IPv6 links work under the same rules (an IPv4 address carried inside an IPv6 one, as in NAT64, 6to4 or Teredo, is checked too; site-local `fec0::/10` counts as private, and the local-use NAT64 prefix `64:ff9b:1::/48` is always refused), and the download timeout covers connecting (TLS handshake included) and slow headers too. Links with a user name or password are refused (400). Credentials go only to the allowed hosts, only over HTTPS, or over plain HTTP to a private address when `METER_ALLOW_PRIVATE_IMAGE_URLS=true` (never plain HTTP to a public address), and are dropped on any cross-origin redirect. Downloads ignore `HTTP_PROXY`/`HTTPS_PROXY` (the address check must apply to the image host itself). Links are logged and stored without their query string and fragment, and access-log lines carry no query strings. |
| **Records** | Written before the response is sent, for refused requests that carry an id (bad request, busy) too. A request refused before its body is read (wrong API key, a body over 30 MB, the per-worker photo body limit full) is not recorded. |
| **Logs** | The photo never appears in the logs, at any log level; API keys never appear in logs, responses or the database. |

## Tests and evaluation

```bash
pip install -r requirements-dev.txt
python -m pytest -q                               # 264 tests, fake model, no cost (3 run only on PostgreSQL)
METER_TEST_DB_URL=postgresql+psycopg2://user@localhost:5432/meter_test METER_DB_NULL_POOL=true python -m pytest -q
```

The tests never call a real model: a fake client answers with scripted model answers (`tests/conftest.py`), and the Claude tests replace Anthropic's API with a fake at the HTTP level (`tests/test_claude.py`).

GitHub Actions (`.github/workflows/image-build-elm-ocr.yaml`) runs the tests on PostgreSQL for every pull request and every push to `main` that changes this folder. On `main`, once they pass, it builds this folder's Docker image and pushes it to ECR as `meter-reader`. To run the tests in the image locally: `docker build --target test -t meter-reader-test . && docker run --rm meter-reader-test`.

To measure accuracy with real model calls, you need a set of hand-checked photos: a CSV with `image_id`, `truth_kind` (`reading`, `not_cumulative` or `unreadable`) and `truth_display`, and a folder of the photos named `<image_id>.jpg`. These sets are kept outside this repository, because the photos are private.

```bash
python scripts/evaluate.py --review review.csv --images photos/ --yes --limit 10   # real API on 10 photos (~$0.23)
python scripts/evaluate.py --review review.csv --images photos/ --yes              # all; results in data/evaluation.jsonl
python scripts/replay.py data/evaluation.jsonl --images photos/                    # re-score saved answers (free)
```

`evaluate.py` costs money (`--yes` confirms it), saves the model's full answers, and resumes where it stopped. `replay.py` feeds those saved answers back through the current checks, so a change to the checks, meter types or thresholds can be measured for free. To confirm the 95% accuracy target before going live, use a fresh, hand-checked set large enough to give about 60 ACCEPT readings with zero errors (several hundred photos at the current accept rate).

## Code layout

```
src/
  run.py            development server; production runs routes:app with gunicorn (gunicorn.conf.py)
  routes.py         the app object that servers load
  app_factory.py    builds the FastAPI app: endpoints, auth, error envelope, body-size limit, health checks
  conf/             config.yaml, settings and production checks (config.py), logging (logging.py)
  error/, models/, validation/    error codes, request/response models, image validation
  service/api/      request handling (reading_service), limits (guards), storage (metadata_service, database,
                    image_store: local folder or S3)
  service/vision/   the reader, with no web or database code:
                      reader.py         runs a read: prepares the image, calls the model, applies the checks
                      prompt.py         the prompt and answer format sent to the model
                      openai_client.py  OpenAI (GPT-6 Sol);  claude_client.py  Anthropic (Claude Sonnet 5.5)
                      checks.py         the rules that turn an answer into ACCEPT / REVIEW / RETAKE
                      meter_types.py    known meter models: digit count and decimal point
                      images.py, net.py image preparation, safe image downloads
scripts/            run.py (send a photo to the service), evaluate.py (real-API evaluation),
                    replay.py (free re-scoring of saved answers)
tests/              pytest suite; a fake model client, so no API calls
docs/               INTEGRATION.md (API contract), DEPLOY_AWS.md (deployment and operations)
```

A request goes `app_factory.py` (endpoint) → `reading_service.py` (id, duplicate photo and budget checks, download, storage) → `reader.py` (model call and checks) → back to `reading_service.py` (response and record).

## Known meter types

| id | Meter | Display |
|---|---|---|
| `hpl_ppsm01` | HPL PPSM 01 smart meter (PSPCL) | 8 digits, 1 decimal (`0019712.7`) |
| `hpl_ppem26` | HPL PPEM 26 (PSPCL) | 7 digits, 1 decimal |
| `lt_er300` | L&T / Schneider ER300 (APDCL) | 8 digits, no decimal point (`00018103`); every screen also lights the time zone in force (T0...T7), and the total is marked `C` |
| `lt_em101` | L&T / Schneider EM101+ (APDCL) | 1 decimal; digit count varies (`2344.7`) |
| `genus_saksham345` | Genus SAKSHAM-345 (APDCL) | 8 digits, 1 decimal (`0032740.9`); every screen also lights the time zone in force (`T1 CUM kWh`) |
| `visiontek_37tm` | Visiontek 37TM (Punjab) | 8 digits, 1 decimal |
| `avon_asem306` | Avon ASEM-306 (Punjab) | 8 digits, 1 decimal |
| `aew_aplsm346` | AEW APL-sm346 (Punjab) | 1 decimal; digit count not fixed |

To add one, see [Common tasks](#common-tasks).

## Accuracy so far

**Claude Sonnet 5.5** (the provider chosen in October 2026), on the 268 hand-checked fresh photos, one read per photo and no reading history (so the previous-reading checks did not apply):
- **Against GPT-6 Sol, same 100 photos (2026-10-21, prompt `deploy-4`, the rules then on `main`).** Claude returned the right reading on 46 of 54 readable photos (GPT-6 Sol: 32), asked for 41 retakes (GPT-6 Sol: 50), accepted 3 (GPT-6 Sol: 6) and accepted no wrong reading (neither did GPT-6 Sol). It cost $0.014 per photo against $0.022, with a median of 4 s against 24 s.
- **Current prompt `deploy-5` against `deploy-4`, all 268 photos, same day (2026-10-23), current rules.** The right reading came back on 114 of 146 readable photos with both prompts. With `deploy-5`: 8 readable photos accepted (`deploy-4`: 5), all right; 60 of 68 unreadable photos retaken (53) and 49 of 54 other-screen photos (45), so fewer meaningless readings reach review; Genus `T1 CUM` totals go to review instead of retake; a billing-period (`BP 1`) screen is now rejected. No wrong reading was accepted in either run. (One accept differs from the hand label, but the photo clearly shows the accepted value; the hand label is wrong.) Readings vary a little from run to run: the same `deploy-4` prompt returned 119 right readings on 2026-10-21.
- Why so few ACCEPTs: Claude answers UNREADABLE whenever it is not certain of every character, even when its best reading is right (it was on most of these), so most readable photos go to review, where the app shows the reading for confirmation. ACCEPT rises in production as consumers get confirmed previous readings and meter types are added.

**GPT-6 Sol**, on hand-reviewed photos:
- **100 trial photos.** Every ACCEPT was correct except one bill-history screen (labelled "1. bILL kW·h"), which the current prompt now declines as another screen. The returned reading was right on 36 of 51 readable photos, decimal point included; most misses were glare photos, with the wrong digit flagged in `uncertainDigits`. The rules were tuned on these same photos.
- **268 fresh photos** (2026-10-02, $6.17). 20 of 21 ACCEPT readings were right (95.2%). The miss, PB_harrion-tapprian, was a clear-photo misread: 0016183.1 for 0016189.1, at confidence 0.98. The returned reading was right on 87 of 146 readable photos, and 17 Assam tariff or bill screens that the hand review counted as readings were rightly declined. About half of these photos had no readable total at all: 25% were unreadable even for a person, and 20% showed the wrong screen.

21 ACCEPT readings are too few to support the 95% target; about 60 with zero errors are needed. Since these GPT-6 Sol runs, hard-to-read photos go to review instead of retake and more screen labels are rejected (H, B1, export, zone, billing-period, month, daily and high-resolution screens); none of these changes adds an ACCEPT.

## Privacy

Photos are sent to OpenAI; approval for this has been given. With `METER_PROVIDER=claude` they are sent to Anthropic instead, which needs its own approval before real photos are used. Only the photo and the fixed prompt are sent to the model, never consumer ids or other details. Every photo is also kept in `data/images/` or S3, for research and for checking readings. File names hold no consumer details; the database links photo, consumer and reading. These are consumer photos:
- never publish or share them;
- restrict access to the folder or bucket;
- set a retention period (S3 lifecycle rule);
- the logs contain consumer ids.
