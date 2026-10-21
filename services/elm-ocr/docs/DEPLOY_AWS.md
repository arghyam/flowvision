# Deploying the meter reader on AWS

This guide covers the AWS pieces the service needs, two ways to run it (containers on ECS Fargate, or an EC2 instance like FlowVision), and the checks to run before the central JalSoochak system starts sending photos.

```
JalSoochak central system ──HTTPS──> Application Load Balancer ──HTTP:8000──> meter reader (ECS tasks or EC2)
                                                                                    │  ├─> OpenAI or Anthropic API (HTTPS, egress)
                                                                                    │  └─> JalSoochak image endpoint (photo download)
                                                                                    ├─> RDS PostgreSQL (readings)
                                                                                    └─> S3 (photos)
```

## 1. AWS resources

| Resource | Settings |
|---|---|
| **RDS PostgreSQL 16** | `db.t4g.small` is enough for a pilot. Enable automated backups (7+ days), storage encryption and Multi-AZ if the pilot must survive a zone outage. Put it in private subnets, and have its security group accept 5432 only from the service. Create the user and a database it owns (since PostgreSQL 15 an ordinary user cannot create tables in a database it does not own): `CREATE USER meter_reader WITH PASSWORD '...'; CREATE DATABASE meter_reader OWNER meter_reader;`. The service creates its tables on first start and checks them on every start. `meter_reader_db_ddl.sql` is the same schema, if a DBA prefers to create it; run it as that user. |
| **S3 bucket** for photos | Block all public access (default). Use SSE-S3 encryption (the service also asks for AES256 on each upload), versioning optional. Add a lifecycle rule for the retention period agreed for consumer photos (e.g. expire after 365 days). |
| **Secrets Manager** | `OPENAI_API_KEY` (or `ANTHROPIC_API_KEY` with `METER_PROVIDER=claude`), `METER_SERVICE_API_KEY` (a long random string, shared with the JalSoochak team) and `METER_DB_URL` (includes the DB password). |
| **IAM roles** | The **task role** (ECS) or instance profile (EC2) that the service runs as: `s3:PutObject` and `s3:GetObject` on `arn:aws:s3:::<bucket>/<prefix>/*`, nothing else. On ECS, the **task execution role** also needs `AmazonECSTaskExecutionRolePolicy` plus `secretsmanager:GetSecretValue` on the service's secrets (the three above, plus `METER_IMAGE_URL_HEADERS` if the image endpoint needs credentials) (and `kms:Decrypt` if they use a customer-managed key); without it the tasks fail to start. |
| **Application Load Balancer** | HTTPS listener (ACM certificate) forwarding to a target group on port 8000. **Idle timeout: 120 seconds** (Attributes → Connection idle timeout). Health check: `GET /health`, 200, interval 15 s, timeout 5 s. Internal if JalSoochak calls from inside the VPC or over a peering/VPN; internet-facing otherwise, optionally with a security group or WAF allow-list for JalSoochak's IPs. **Do not put API Gateway in front**: its 29–30 s limit is shorter than a slow reading. |
| **Security groups** | ALB: 443 from the callers. Service: 8000 from the ALB only. Outbound: 443 to the internet (OpenAI or Anthropic, the JalSoochak image host, S3) and 5432 to RDS. |
| **CloudWatch** | Log group for the service (retention, e.g. 90 days; the logs contain consumer ids). Alarms: see [section 5](#5-monitoring). |

## 2. Environment variables

Set these as ECS task environment variables and secrets, or in `/etc/meter-reader.env` on EC2 (mode 600). `METER_ENV=production` makes the service refuse to start while any required value is missing, and the error says which.

| Variable | Value |
|---|---|
| `METER_ENV` | `production` |
| `OPENAI_API_KEY` | secret |
| `METER_PROVIDER` | `openai` (default, GPT-6 Sol) or `claude` (Claude Sonnet 5.5; then set `ANTHROPIC_API_KEY`, a secret, instead of `OPENAI_API_KEY`). Only that model reads the photos; no other model is tried. When switching, remove `METER_MODEL` and `METER_REASONING_EFFORT` or set Claude values, or the service refuses to start. Sending consumer photos to Anthropic needs the data-sharing approval first (README, Privacy). |
| `ANTHROPIC_WORKSPACE_ID` | Claude only, and only for an API key that is not tied to one workspace: the workspace to use (`wrkspc_...`). |
| `METER_SERVICE_API_KEY` | secret. Comma-separate two keys while rotating. |
| `METER_DB_URL` | secret: `postgresql+psycopg2://USER:PASSWORD@RDS-ENDPOINT:5432/meter_reader?sslmode=require` |
| `METER_IMAGE_DIR` | `s3://BUCKET/meter-photos` |
| `METER_IMAGE_URL_ALLOWED_HOSTS` | JalSoochak's image host name(s), comma-separated (only these and their subdomains are downloaded from; downloads ignore `HTTP_PROXY`) |
| `METER_IMAGE_URL_HEADERS` | secret, if JalSoochak's image endpoint needs credentials, e.g. `{"Authorization": "Bearer ..."}`. Sent only to the allowed hosts. |
| `METER_ALLOW_PRIVATE_IMAGE_URLS` | `true` only if the image host resolves to private VPC addresses. Cloud metadata addresses stay blocked. Credentials then also go over plain HTTP, but only to a private address. |
| `METER_DAILY_BUDGET_USD` | Default **25** (config.yaml): model calls stop for the rest of the day (midnight to midnight India time) once the day's spend reaches $25, about 1,080 photos at ~$0.023 each. Readings in flight count: each reserves $0.05 per model call (`limits.reserve_per_call_usd`), more than any real call has cost, so spend does not overshoot the cap. Raise it if the pilot sends more; `none` removes the cap (on purpose only). Also set a monthly budget on the OpenAI account itself. |
| `WORKERS` | gunicorn workers per task or instance, e.g. `2` (each runs up to 8 readings at once; `METER_MAX_CONCURRENT_READS`, sized for 2 workers in a 4 GB task: raise it only with more task memory) |

Everything else has production defaults, listed with the other options in `.env.example` and `src/conf/config.yaml`:
- 100 s request deadline; `gunicorn.conf.py` derives its timeouts from it (`METER_REQUEST_DEADLINE`, from the environment or `meter_reader_service/.env`, else config.yaml; `WORKERS` from the environment or `.env`, else 2) and keeps connections alive for 130 s, above the ALB's 120 s idle timeout;
- JSON logs to stdout, gunicorn's own lines (worker killed, out of memory, timeouts) included; access-log lines carry no query strings;
- API docs off.

Database settings (all but `METER_DB_URL` are optional; the defaults suit RDS, and the timeouts apply to PostgreSQL):

| Variable | Default | Purpose |
|---|---|---|
| `METER_DB_URL` | SQLite file | PostgreSQL in production (section 2 above). |
| `METER_DB_POOL_SIZE`, `METER_DB_MAX_OVERFLOW` | 5, 10 | Connections kept open per worker, and extra ones under load. |
| `METER_DB_POOL_TIMEOUT` | 10 s | Wait for a free connection. |
| `METER_DB_POOL_RECYCLE` | 1800 s | Connections are reopened after this. |
| `METER_DB_CONNECT_TIMEOUT` | 5 s | Opening a connection. |
| `METER_DB_STATEMENT_TIMEOUT_MS` | 15000 | The server cancels a slower statement. |
| `METER_DB_TCP_USER_TIMEOUT_MS` | 15000 | With TCP keepalives, a lost database (failover, network loss) fails within about 15–25 s as 503 `ERROR_13` instead of hanging. |
| `METER_DB_NULL_POOL` | `false` | Tests only: no connections kept open. |


## 3a. Option A: ECS Fargate (recommended)

1. Build and push the image:
   ```bash
   cd services/elm-ocr
   docker build -t meter-reader .
   aws ecr get-login-password | docker login --username AWS --password-stdin <account>.dkr.ecr.<region>.amazonaws.com
   docker tag meter-reader <account>.dkr.ecr.<region>.amazonaws.com/meter-reader:<version>
   docker push <account>.dkr.ecr.<region>.amazonaws.com/meter-reader:<version>
   ```
2. Set up the task definition:
   - 1 vCPU and 4 GB (a safety margin: 2 GB also held in the burst tests in [section 6](#6-capacity-and-limits)), port 8000, the task role and task execution role from [section 1](#1-aws-resources);
   - the environment and secrets from [section 2](#2-environment-variables);
   - the `awslogs` log driver;
   - `stopTimeout` 120 on the container (the Fargate maximum). gunicorn waits up to 120 s (deadline + 20) for in-flight readings when a task stops, so they finish on scale-in, deploys and health-check replacements. With the default 30 s they are killed: their ids answer 409 for about 3 minutes and are then read, and billed, again;
   - a container health check (ECS ignores the image's own HEALTHCHECK): `CMD-SHELL`, `python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)"`, interval 30, timeout 5, retries 3, start period 30.
3. Create the service: 2 tasks across two subnets, behind the ALB target group, with health check grace period 60 s and deregistration delay 120 s (so in-flight readings finish during deploys). Scale on CPU or ALB request count if needed.
4. Deploy new versions by updating the task definition's image tag. ECS rolls tasks one at a time, and in-flight readings finish during the deregistration delay.

## 3b. Option B: EC2 with systemd (the FlowVision way)

On an Ubuntu instance with an instance profile (Python 3.12, the default on Ubuntu 24.04), as a dedicated user without sudo:
```bash
sudo useradd --system --create-home --home-dir /opt/meter-reader meterreader
sudo -u meterreader git clone https://github.com/arghyam/flowvision.git /opt/meter-reader/src-repo
sudo ln -s /opt/meter-reader/src-repo/services/elm-ocr /opt/meter-reader/meter_reader_service
sudo -u meterreader python3.12 -m venv /opt/meter-reader/venv
sudo -u meterreader /opt/meter-reader/venv/bin/pip install --require-hashes -r /opt/meter-reader/meter_reader_service/requirements.lock
sudo -u meterreader mkdir -p /opt/meter-reader/meter_reader_service/data /opt/meter-reader/meter_reader_service/logs
sudo install -m 600 /dev/null /etc/meter-reader.env && sudoedit /etc/meter-reader.env    # the variables from section 2
sudo cp /opt/meter-reader/meter_reader_service/meter_reader.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now meter_reader
journalctl -u meter_reader -f
```

Register the instance in the ALB target group (port 8000). Also:
- require IMDSv2 on the instance (hop limit 1);
- get the logs to CloudWatch. The service writes JSON lines to the journal; the CloudWatch agent reads files, so either forward the journal to a file it tails (e.g. rsyslog with `$template json,"%msg%\n"`), use Fluent Bit's systemd input, or set `METER_LOG_TO_FILES=true` with `WORKERS=1` and have the agent tail `logs/api_logs/api.log` (JSON in production; it holds the service's own lines only: gunicorn's worker-timeout and out-of-memory lines, which two alarms in section 5 need, reach only the journal).

Update the code with `sudo -u meterreader git -C /opt/meter-reader/src-repo pull && sudo systemctl reload meter_reader`: new workers start first, then the old ones finish their readings and stop, so no request is refused. After changing `/etc/meter-reader.env`, use `sudo systemctl restart meter_reader` instead (a reload does not re-read it); a restart briefly refuses new requests and waits up to 120 s (gunicorn's graceful_timeout) for in-flight readings.

With Docker on EC2 instead of systemd, `docker compose up -d` (see `docker-compose.yml`) runs the service with a local PostgreSQL, on 127.0.0.1:8000 (put a reverse proxy or the ALB in front). For the pilot, set `METER_DB_URL` (RDS) and `METER_IMAGE_DIR` (S3) in `.env`; they replace the local defaults.

## 4. After each deploy: smoke test

```bash
curl -s https://<host>/health                    # {"status":"ok"}
curl -s https://<host>/ready                     # {"status":"ok","database":"ok",...}
# one real reading (about $0.023); use a new consumer id each time: the same photo of the same consumer within 7 days is answered from the earlier result, with no model call
python scripts/run.py path/to/meter.jpg --server https://<host> --api-key <key> --consumer SMOKE-TEST-$(date +%Y%m%d%H%M)
```

The last command must show:
- a reading;
- a stored record with `photo: YYYY-MM-DD/<id>.jpg`, and the photo in S3;
- `request and response stored: yes`.

## 5. Monitoring

Useful CloudWatch alarms:
- **ALB `HTTPCode_Target_5XX_Count`** above a few per 5 minutes. The service returns real 5xx statuses for model outages (502), busy (503) and internal errors (500).
- **ALB `TargetResponseTime`** p95 above 60 s: the model provider is slow, or photos are queueing.
- **Unhealthy host count** above 0.
- **A log metric filter with the pattern `{ $.level = "CRITICAL" }`,** alarming on 1 or more. It fires for:
  - the daily budget reached;
  - the OpenAI account out of credit (`insufficient_quota`) or an OpenAI key rejected; with `METER_PROVIDER=claude`, the Anthropic account out of credit (HTTP 402), a rejected Anthropic key (401/403) or an unknown model (404);
  - results that could not be stored;
  - a photo that could not be saved (e.g. S3 refused the upload; the reading is still returned);
  - a gunicorn worker timeout (gunicorn's own lines are JSON in production too). (gunicorn's own lines are JSON in production too).
- **A log metric filter for `{ $.message = "*out of memory*" }`** (gunicorn's line when a worker is killed for memory), alarming on 1 or more.
- **RDS** CPU, free storage and connections.

Every reading request is also in the `meter_reader_extraction_data` table, with status, reasons, cost (`cost_usd`) and the full request and response, except those refused before their body is read (401, 413, the photo-body limit's 503) and invalid or busy-refused requests sent without an id. For example, `SELECT meter_reading_status, count(*), sum(cost_usd) FROM meter_reader_extraction_data WHERE request_timestamp > now() - interval '1 day' GROUP BY 1;`.

## 6. Capacity and limits

None of these limits stops the service by itself; each only acts in the situation it guards against.

| Limit | Default | When it acts | What happens | Change with |
|---|---|---|---|---|
| Daily model spend | $25 a day (about 1,080 photos) | The day's model spend, plus the reservations of readings in flight, leaves no room for one more $0.05 reservation | Model calls stop until midnight India time (429 `ERROR_11`), CRITICAL log. If only the reservations of other readings in progress are in the way, the 429 is retryable (`Retry-After: 60`); a retried id whose earlier attempt was billed is told the budget is used up when no other reading is in progress | `METER_DAILY_BUDGET_USD`, `limits.reserve_per_call_usd` (0.05) in config.yaml |
| Same photo again | 7 days | The identical photo file of the same consumer arrives again with a new id | The original read's result is returned; no model call, no cost. While it is still being read, 409 `ERROR_12` (retryable). A photo a person corrected goes to review (`CORRECTED_BEFORE`), even with the setting at 0 | `METER_DUPLICATE_PHOTO_HOURS` (0 = no reuse and no in-flight check) |
| Readings at once | 8 per worker | More readings than that at the same moment | Up to 8 more wait up to 30 s for a slot, then get 503 `ERROR_10` (retryable); beyond that, 503 at once. Sized so a 1 vCPU, 4 GB task with 2 workers is not killed for memory by a burst | `METER_MAX_CONCURRENT_READS` (raise only with more task memory), `METER_BUSY_WAIT_SECONDS`, `WORKERS` |
| Time per reading | 100 s | The model provider is very slow (a normal reading takes ~20 s with OpenAI, ~5 s with Claude) | That reading fails with 502 `ERROR_04`, retryable | `METER_REQUEST_DEADLINE` |
| Model retries | 1 (2 attempts in total) | The provider says busy (429, or 529 for Anthropic), returns a 5xx error, or cannot be reached (Claude gives up connecting after 5 s) | One more try if time allows; never after a timeout or a dropped connection, which may already be billed | OpenAI: `METER_OPENAI_ATTEMPTS` (total attempts; 1 = no retry). Claude: `claude.max_attempts` in config.yaml (no environment override) |
| Request size | 30 MB | A bigger request body | 413 `ERROR_003` (a body that declares its size is read, up to 60 MB, and discarded first, so the client gets the error, not a connection reset; a streamed body without `Content-Length` is cut off at 30 MB) | `METER_MAX_BODY_BYTES` |
| Photo bodies held at once | 150 MB per worker, by `Content-Length` (a body without one counts as 30 MB) | A burst of large photos (normal ~5 MB phone photos never reach it) | One more reading request gets 503 `ERROR_10` (retryable, `Retry-After: 10`); its body is read and thrown away unparsed (so the client gets the 503, not a connection reset), so it is not stored and gets a new response id. A body that stalls for 60 s between two parts gets a retryable 503 (send it again) and frees its place | `METER_MAX_INFLIGHT_BODY_BYTES` |
| Image size | 20 MB; 250 MP (single-scan grey or colour JPEG) / 16 MP (PNG, WebP, and progressive, multi-scan or CMYK JPEG); one full-size decode at a time per worker | A bigger photo | 400 or 413 | `file_size_limit`, `reader.max_jpeg_pixels`, `reader.max_image_pixels` in config.yaml |

Measured in Docker (production image, 2 workers, defaults, at 2 GB and 4 GB, on a fresh container and again right after a first burst): 64 phone photos of 3.6 MB at once peaked at about 0.7 GB for the whole container (32 read, 32 told busy); 32 photos of 11 MB about 1.16 GB (20 read, 12 told busy by the body limit); 16 photos of 19 MB about 1.18 GB (10 read, 6 told busy); 8 progressive 16 MP JPEGs about 1.0 GB. None ran out of memory, lost a paid call or left a row in progress, and each worker held about 0.2–0.5 GB after one burst, levelling off at about 0.7 GB after repeated bursts. 2 GB held in these tests; a 4 GB task is the safety margin. `MALLOC_ARENA_MAX=2` is set in the image and the systemd unit; workers are not recycled (`MAX_REQUESTS` 0, since gunicorn's worker timeout could cut a reading while a recycled worker shuts down, and memory stays flat without it). A database that freezes without dropping its connections (rare; a failover or network loss drops them and gives 503 `ERROR_13` within about 15-25 s) holds requests until the load balancer times out; no model call is made, and `/ready` answers 503 within 5 s, so alarm on it. A photo that cannot be saved is logged at CRITICAL (the reading is still returned).

Every model call is recorded with its cost, including failed calls that OpenAI may still bill (an unusable answer, or a timeout counted at an estimated $0.03), and a retried failed id keeps that cost, so the daily cap sees all spend. Before each model call the request reserves $0.05 per call on its own row (`limits.reserve_per_call_usd`; the costliest of 384 real calls was $0.043), under a lock shared by every worker and task (a PostgreSQL advisory lock), and a call is made only if the day's recorded spend plus reservations, plus its own reservation, stays within the cap (so a cap below $0.05 allows no calls). Afterwards the reservation is replaced by the real cost. So a burst cannot overshoot the cap: spend can end above it only if a call costs more than its reservation, which none has so far. Readings in progress are other ids' rows still processing and less than 10 minutes old; when only their reservations are in the way, the 429 is retryable after 60 s. An older abandoned row stays counted.

Two rare cases cost one extra reading each:
- A worker killed mid-read (out of memory, or a task stopped without enough `stopTimeout`) leaves its $0.05 reservation counted; a retry of that id after about 3 minutes is read, and billed, again.
- If the database fails for more than about 7 s just as a reading is stored, the reading is still returned and its row keeps the reserved cost, but the record stays incomplete, and a retry of that id after about 3 minutes is read again.

A lost database connection fails within about 15–25 s as 503 `ERROR_13` (retryable) instead of hanging (`METER_DB_TCP_USER_TIMEOUT_MS`).

Throughput:

- One reading holds a read slot for about 20 s. One task with 2 workers × 8 slots handles about 0.8 photos per second (about 69,000 a day) in steady state; a short burst waits for a slot, and a sustained overload gets `503 ERROR_10` (retryable) instead of queueing without limit. Add tasks for more; raise `METER_MAX_CONCURRENT_READS` only with more task memory.
- The provider's own rate limits for the account usually bind first. Check the OpenAI account's tier, or the Anthropic organization's rate limits with `METER_PROVIDER=claude`.
- DB connections: each worker opens up to 15 connections (`METER_DB_POOL_SIZE` 5 + `METER_DB_MAX_OVERFLOW` 10). Keep tasks × workers × 15 under the RDS connection limit.

## 7. Operations

- **Rotate the service API key:** set `METER_SERVICE_API_KEY=new,old`, deploy, let JalSoochak switch to `new`, then set `new` alone.
- **Rotate the OpenAI or Anthropic key:** update the secret and redeploy/restart. The key that was pasted in chat during development must be rotated before the pilot.
- **Rotate the database password:** update `METER_DB_URL` in Secrets Manager and redeploy/restart.
- **Data retention:**
  - photos expire through the S3 lifecycle rule;
  - rows in `meter_reader_extraction_data` hold consumer ids and readings; delete old rows on the agreed schedule (e.g. `DELETE ... WHERE request_timestamp < now() - interval '2 years'`);
  - CloudWatch log retention is set on the log group.
- **Backups:** RDS automated backups and snapshots. The S3 bucket can have versioning or replication if photos must survive deletion mistakes.
- **Rollback:** redeploy the previous image tag or git revision. The schema only ever gains columns and indexes, so older versions keep working. After an upgrade, ids answered by an earlier version still replay if their photo was sent as base64, an upload, or a link without a token parameter (`token`, `access_token`, ...); for ids answered by a version from before October 2026, only a link without any query string replays (that version ignored the query, so the match cannot be made safely). Other retries get 409 `ERROR_15`, also for ids answered by the October 2026 build before this one, and also during a rolling deploy while old and new tasks share the database. No money is spent, and the stored result can still be read with `GET /readings/{id}`. The other way round: after a rollback, the older version may refuse retries of ids this version answered (409 `ERROR_15`). Let pending retries finish, or pause them, before rolling back.

## 8. Pilot go-live checklist

- [ ] RDS, the S3 bucket (public access blocked, lifecycle rule), secrets, IAM role and security groups are created.
- [ ] ALB is on HTTPS, with idle timeout 120 s and health check `/health`.
- [ ] `METER_ENV=production` and all section 2 variables are set; the $25 daily cap fits the expected volume (raise `METER_DAILY_BUDGET_USD` if not).
- [ ] A monthly budget and alerts are set on the OpenAI account, or on the Anthropic workspace or organization with `METER_PROVIDER=claude`.
- [ ] The OpenAI key is rotated; the old one pasted in chat is revoked.
- [ ] The smoke test ([section 4](#4-after-each-deploy-smoke-test)) passes against the deployed URL.
- [ ] The CloudWatch alarms from [section 5](#5-monitoring) exist and notify someone.
- [ ] The JalSoochak team has `docs/INTEGRATION.md`, the URL and the API key, and has confirmed its client timeout (120 s) and retry-with-same-id behaviour.
- [ ] JalSoochak's image host is in `METER_IMAGE_URL_ALLOWED_HOSTS`, and a real `imageURL` from it works in the smoke test.
- [ ] Someone owns the `REVIEW` queue and sends `/feedback`.
