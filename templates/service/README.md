# Service conventions

Conventions for every service under `services/`. This is a checklist, not a template you
can copy as-is. The real template will be extracted from `services/elm-ocr` once that
service serves real traffic. Until then, start a new service by copying `services/elm-ocr`
and work through this list.

`services/bfm-ocr` predates these conventions and is exempt until it is migrated.

## Layout

```text
services/<meter>-ocr/
├── Dockerfile
├── .dockerignore          # allowlist
├── README.md
├── pyproject.toml         # direct dependencies
├── requirements.lock      # generated, hashed
├── src/<meter>_ocr/
│   └── main.py            # FastAPI app
└── tests/
```

- Name the directory in kebab-case (`elm-ocr`) and the package in snake_case (`elm_ocr`).
  Use the `src/` layout.
- A service is self-contained: every command runs from its directory, and the Docker
  build context is that directory.
- Services never import from each other. Shared code goes to `libs/ocr-common`, and only
  once two services need it.
- Add the service to the root `CODEOWNERS`.

## Dependencies

- Python 3.12.
- Declare direct dependencies in `pyproject.toml`. Compile `requirements.lock` with hashes
  using uv, which is a lock compiler only, not a workspace:

  ```bash
  uv pip compile pyproject.toml --universal --python-version 3.12 \
    --generate-hashes -o requirements.lock
  ```

- The image installs from the lock with `pip install --require-hashes`.

## Container

- Use a multi-stage build: create a venv from the lock in a builder stage, then copy
  `/opt/venv` into the runtime stage.
- Run as a non-root numeric UID (`USER 10001:10001`), so Kubernetes can enforce
  `runAsNonRoot`.
- Keep code root-owned and write nothing to disk. If the service must write somewhere,
  create that directory with `install -d -o <uid>` before `USER`. The local Docker uses
  the legacy builder, which creates `WORKDIR` as root even when it comes after `USER`.
- Use an exec-form `CMD` with one uvicorn process per container. Scale with replicas.
- Log to stdout only.

## HTTP API

- Serve the API under the base path `/flowvision/v1/<meter>` (e.g. `/flowvision/v1/elm`).
  The ingress routes requests by this prefix.
- Serve `GET /health` at the root, outside the base path, for Kubernetes probes.
- Name JSON fields in camelCase. Every extraction response carries a `correlationId`,
  and every request log line includes it.
- Map the response status explicitly from the model or provider outcome. Never infer it
  from the reading text.
- **Open: the error contract.** BFM returns HTTP 200 with `statusCode` in the body. New
  services have not yet chosen between that envelope and real HTTP status codes with an
  RFC 9457 problem body. Decide when the first ELM endpoint is designed, and record the
  decision in an ADR.

## Concurrency

- An `async def` handler must never block. Offload CPU-bound inference
  (`anyio.to_thread.run_sync` or a process pool) and use async clients for I/O. BFM's
  routes are `async def` handlers that call blocking code; don't copy that pattern.
- Load models once at startup, in the FastAPI lifespan, not per request.

## Configuration and data

- Configure the service through environment variables, using a prefix unique to the
  service (e.g. `ELM_`). Never commit secrets.
- Each service owns its own PostgreSQL schema and role, and manages migrations with
  Alembic. Never read or write another service's tables. ADR-001 records how this rolls
  out.

## Tests

- Use pytest, with tests under `tests/`. Golden-image tests for reading accuracy are
  planned for when CI is introduced.
