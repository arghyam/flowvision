# ADR-001: Multi-service repository layout

- **Status:** Accepted
- **Date:** 2026-10-08

## Context

FlowVision began as a single service: BFM OCR, which reads bulk flow meters and lived at
the repository root. A second meter type, ELM, needs its own models and reading providers,
and more meter types may follow. The repository has to hold several services without
coupling their dependencies, images or releases.

The following constraints shaped the decision:

- Kubernetes manifests live in a separate repository, and deploys are manual.
- There is no test suite and no CI.
- The BFM image is about 7.5 GB, and its build downloads about 3 GB of CUDA wheels.
- Restructuring must not change how BFM behaves for existing clients: their paths, the
  database table and the responses stay the same.

## Decision

1. **Layout.**

   ```text
   services/<meter>-ocr/   one self-contained service per meter type
   libs/ocr-common/        code shared by two or more services
   templates/service/      conventions for new services
   docs/adr/               architecture decision records
   CODEOWNERS
   ```

2. **Self-contained services.** Each service has its own `pyproject.toml` or
   `requirements.txt`, its own hashed `requirements.lock` and its own `Dockerfile`, and
   its directory is its build context. Services never import from each other.
3. **Independent locks.** Each service compiles its own lock with
   `uv pip compile --generate-hashes`, and its image installs with
   `pip install --require-hashes`. There is no uv workspace and no shared lock, so BFM's
   ML stack never constrains another service's versions.
4. **Shared code only when two services need it.** `libs/ocr-common` starts empty.
5. **BFM moved as-is.** It was moved with `git mv` (every file a 100% rename). The only
   other changes fix the build: a hashed lock, and a Dockerfile fix for directory
   ownership under the legacy builder. Its behaviour, its existing paths and its database
   table are unchanged.
6. **BFM is also served under `/flowvision/v1/bfm`.** The same routes are mounted under
   both `/flowvision/v1` (existing clients) and `/flowvision/v1/bfm`, the per-meter
   prefix that new services use. Both paths return identical responses.
7. **The GitBook docs stay at the root** (`README.md`, `SUMMARY.md`, `.gitbook/` and the
   rest). They are stale and were not updated by this change.
8. **No `deploy/` folder.** Deployment config stays in the Kubernetes repository.
9. **Per-service image builds.** Each service has its own GitHub Actions workflow
   (`.github/workflows/image-build-<service>.yaml`) that runs only when that service's folder
   or its workflow changes, and pushes the image to ECR on `main`. ELM's workflow first runs
   its test suite (on PostgreSQL) and also runs it, without pushing, on pull requests. BFM
   has no tests yet. Deploys stay manual.

New services follow [templates/service/README.md](../../templates/service/README.md).

## Changes required outside this repository

These are tracked here because the Kubernetes repository and the build procedure live
elsewhere.

- **BFM image build.** Build BFM with `docker build services/bfm-ocr`, not from the repo
  root. Update any build script or runbook that points at the old root `Dockerfile`.
  The image's behaviour, port (8000) and environment variables are unchanged.
- **BFM ingress.** If the existing rule is `pathType: Prefix` on `/flowvision/v1` with no
  rewrite, it already sends `/flowvision/v1/bfm/...` to BFM, and nothing is strictly
  required. Add an explicit `/flowvision/v1/bfm` rule anyway, so the new path does not
  depend on the catch-all.
- **Deploy order for BFM.** The ingress change and the new image can ship in either
  order. Until the image ships, `/bfm` paths return 404 from the old image, which is
  harmless because no client calls them yet. Move clients to `/bfm` only once both are
  live.
- **ELM deployment.** ELM serves real endpoints; its settings are in
  [services/elm-ocr/.env.example](../../services/elm-ocr/.env.example).
  - Add a Deployment and a Service for the ELM image (ECR repository `meter-reader`), with
    container port 8000.
  - Point the liveness probe at `GET /health` and the readiness probe at `GET /ready`, which
    also checks the database. Probes reach the pod directly, so neither needs an ingress rule.
  - Set `securityContext.runAsNonRoot: true`. The image runs as `10001:10001`. It also runs
    with `readOnlyRootFilesystem: true` when `/tmp` is an `emptyDir`.
  - Set `WORKERS=1` (one process per pod) and scale with replicas.
  - Set `terminationGracePeriodSeconds` to at least 130, so in-flight readings (up to 100 s)
    finish on rollout.
  - Set `METER_ENV=production`, which makes the pod refuse to start until the required
    settings are present. Take `OPENAI_API_KEY`, `METER_SERVICE_API_KEY`, `METER_DB_URL` and
    `METER_IMAGE_URL_HEADERS` (if used) from a Secret. Set `METER_IMAGE_DIR` and
    `METER_IMAGE_URL_ALLOWED_HOSTS` as plain environment variables.
  - ELM creates and extends its own tables (`meter_reader_*`) on startup, so its role needs
    to own them. To place them in the `elm` schema (Deferred, item 2), run
    `ALTER ROLE <elm role> SET search_path = elm`. A search path set in `METER_DB_URL`'s
    `options` is overridden by the service's own connection options.
- **ELM ingress.** Add a `pathType: Prefix` rule that sends `/flowvision/v1/elm` to the
  ELM Service. The longest matching prefix wins, so this rule takes precedence over BFM's
  `/flowvision/v1`. Until the ELM rule exists, BFM's rule also catches ELM paths.
  ELM serves its full path itself, so the rule needs no rewrite.
  - ELM also serves the same API under `/meter-reader/v1`, its path before this layout.
    Add a rule for that prefix only if existing clients will reach ELM through this ingress.
  - A reading can take up to 100 s, and a request body up to 30 MB (a base64 photo). Raise
    the ingress limits to match. With ingress-nginx, the defaults (60 s, 1 MB) are too low:
    set `nginx.ingress.kubernetes.io/proxy-read-timeout: "120"`,
    `proxy-send-timeout: "120"` and `proxy-body-size: "30m"`.

## Deferred

These are agreed directions, left out of this change so BFM's behaviour stays unchanged.

1. **BFM health and client migration.**
   - Add `GET /health` only after BFM's routes stop blocking the event loop. They are
     `async def` but run the image download and inference synchronously, so a worker
     that is busy with a request cannot answer anything else, and an HTTP probe on it
     times out under load. First make the routes plain `def` (FastAPI then runs them in
     a thread pool) or move the blocking work off the loop.
   - Until then, configure the BFM probes like this:
     - **Startup probe:** `GET /`, with a failure threshold long enough to cover model
       loading. The pod gets no traffic before it is ready, so nothing blocks the probe.
     - **Liveness and readiness probes:** `tcpSocket` on port 8000. Gunicorn's worker
       timeout (`TIMEOUT`) already restarts hung workers.
     - Never use `GET /` for liveness or readiness. Busy workers fail it, so under load
       Kubernetes would restart pods or remove them from the Service.
   - Move clients to `/flowvision/v1/bfm`, then deprecate the bare `/flowvision/v1` path.
2. **Database schema per service.**
   - BFM stays in `public`, with the table `flowvision_extraction_data`. Moving it would
     need a data migration and code changes for no gain.
   - ELM gets an `elm` schema in the same database, owned by its own role. That role has
     no privileges on `public`, and BFM's role has none on `elm`.
3. **Alembic migrations, per service.**
   - BFM:
     1. Add a baseline revision that matches the current
        [DDL](../../services/bfm-ocr/flowvision_db_ddl.sql).
     2. Run `alembic stamp head` against existing databases. This records the baseline
        as applied without running it.
     3. From then on, change the schema only through revisions.
   - ELM uses Alembic from its first table. Its version table goes in the `elm` schema
     (`version_table_schema`), so it does not collide with BFM's `public.alembic_version`.
4. **Golden-image tests** for reading accuracy, run in CI, and tests for BFM.
5. **How services consume `libs/ocr-common`.** Decide when the first code moves there.
   The options:
   - build from the repo root with `-f services/<svc>/Dockerfile`. This needs a root
     `.dockerignore`, because the local legacy builder ignores per-Dockerfile ignore
     files;
   - publish versioned wheels to a private package index.
6. **The error contract for new services.** BFM returns HTTP 200 with `statusCode` in
   the body. The alternative is real HTTP status codes with an RFC 9457 problem body.
   Decide when the first ELM endpoint is designed, in its own ADR.
7. **Base-image hardening.**
   - Pin `python:3.12-slim` by digest. It currently resolves to Debian trixie.
   - Switch BFM to CPU-only torch wheels to shrink its image.

## Consequences

- Services can change, upgrade dependencies and be released independently. ELM does not
  inherit BFM's ML stack or image size.
- BFM must be run and built from `services/bfm-ocr`, because its config, model and log
  paths are relative to that directory.
- Each service carries its own Dockerfile and lock. The template keeps them consistent.
- The root GitBook docs still describe the old layout (for example `python src/run.py`
  from the root) and backends that no longer exist.
- `services/bfm-ocr/flowvision.service` was moved unchanged. It was already broken and is
  not used.
