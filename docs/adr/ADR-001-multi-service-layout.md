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
- Restructuring must not change how BFM behaves: not its API, its base path, its database
  table or its responses.

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
   ownership under the legacy builder. The API, the base path `/flowvision/v1`, the
   database table and the behaviour are unchanged.
6. **The GitBook docs stay at the root** (`README.md`, `SUMMARY.md`, `.gitbook/` and the
   rest). They are stale and were not updated by this change.
7. **No `deploy/` folder.** Deployment config stays in the Kubernetes repository.
8. **No CI yet.** It would have no tests to run, deploys are manual, and the BFM build is
   too heavy to run on every push. CI will be added together with golden-image tests.

New services follow [templates/service/README.md](../../templates/service/README.md).

## Changes required outside this repository

These are tracked here because the Kubernetes repository and the build procedure live
elsewhere.

- **BFM image build.** Build BFM with `docker build services/bfm-ocr`, not from the repo
  root. Update any build script or runbook that points at the old root `Dockerfile`.
  The image's behaviour, port (8000) and environment variables are unchanged.
- **BFM ingress.** No change. `/flowvision/v1` keeps routing to BFM.
- **ELM deployment**, needed once ELM serves real endpoints:
  - Add a Deployment and a Service for the `elm-ocr` image, with container port 8000.
  - Point the liveness and readiness probes at `GET /health`. Probes reach the pod
    directly, so `/health` does not need an ingress rule.
  - Set `securityContext.runAsNonRoot: true`. The image runs as UID 10001.
  - Run one uvicorn process per pod and scale with replicas.
- **ELM ingress.** Add a `pathType: Prefix` rule that sends `/flowvision/v1/elm` to the
  ELM Service. The longest matching prefix wins, so this rule takes precedence over BFM's
  `/flowvision/v1`. Until the ELM rule exists, BFM's rule also catches ELM paths.
  ELM serves its full path itself, so the rule needs no rewrite.

## Deferred

These are agreed directions, left out of this change so BFM's behaviour stays unchanged.

1. **BFM paths and health.**
   - Serve BFM under both `/flowvision/v1` (existing clients) and `/flowvision/v1/bfm`
     (the per-meter prefix), and add `GET /health`.
   - Then add an ingress rule for `/flowvision/v1/bfm`, move clients to it, and
     deprecate the bare path.
   - Until `/health` exists, BFM probes can use `GET /`.
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
4. **CI, together with golden-image tests** for reading accuracy.
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
