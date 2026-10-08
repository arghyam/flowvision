# ELM OCR

Extracts readings from ELM meter images. **Skeleton:** only `GET /health` is served, and
no reading provider is implemented yet.

**Run every command from this directory (`services/elm-ocr`).**

## Layout

| Path | Role |
| --- | --- |
| [src/elm_ocr/main.py](src/elm_ocr/main.py) | FastAPI app and `/health` |
| [src/elm_ocr/providers/base.py](src/elm_ocr/providers/base.py) | `ReadingProvider` contract: `ExtractionContext` in, `ProviderResult` out |

## Providers

The service downloads the image once and passes it to a provider as an
`ExtractionContext`. The provider returns a `ProviderResult`. Its `ReadingOutcome` maps
explicitly to the API status; the status is never inferred from the reading text. When a
provider itself fails, it raises `ProviderError`. An unreadable image is not a failure.

`extract` is `async` and must not block the event loop. Offload CPU-bound inference to
a thread or process pool, and use async clients for network calls.

## Local development

```bash
python3.12 -m venv venv && source venv/bin/activate
pip install -r requirements.lock
pip install --no-deps -e .
uvicorn elm_ocr.main:app --reload       # :8000
curl localhost:8000/health
```

## Docker

```bash
docker build -t elm-ocr services/elm-ocr      # from the repo root
docker run -p 8000:8000 elm-ocr
```

The container runs one uvicorn process as a non-root user (UID 10001) and logs to stdout.
To scale, add replicas rather than workers.

## Dependencies

- Declare direct dependencies in `pyproject.toml`.
- `requirements.lock` pins every package with hashes for Python 3.12, and the Docker image
  installs from it. Regenerate it after you change dependencies:

  ```bash
  uv pip compile pyproject.toml --universal --python-version 3.12 \
    --generate-hashes -o requirements.lock
  ```
