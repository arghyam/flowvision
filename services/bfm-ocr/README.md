# BFM OCR (FlowVision)

Extracts numeric readings from bulk flow meter (BFM) images. API contract:
[flowvision_api_spec.yml](flowvision_api_spec.yml) and [api-reference.md](../../api-reference.md).

**Run every command from this directory (`services/bfm-ocr`).** Config, model and log paths
are relative to it.

## Local development

```bash
python3.12 -m venv venv && source venv/bin/activate
pip install -r requirements.lock
python src/run.py            # uvicorn on :8000
```

Endpoints (base path `/flowvision/v1`):

- `POST /extract-reading`
- `POST /feedback`

## Docker

```bash
docker build -t flowvision services/bfm-ocr      # from the repo root
docker run -p 8000:8000 flowvision
```

Workers are auto-sized from CPU/GPU count. Each worker loads every model, so set
`-e WORKERS=1` on low-memory hosts. `TIMEOUT`, `LOG_LEVEL` and the other gunicorn settings
can be overridden the same way (see the [Dockerfile](Dockerfile)).

## Configuration

| Variable | Purpose |
| --- | --- |
| `CONFIG_PATH` | Config file (default `src/conf/config.yaml`) |
| `FLOWVISION_DB_{HOST,PORT,NAME,USERNAME,PASSWORD}` | PostgreSQL connection |
| `FLOWVISION_QUALITY_THRESHOLD` | Overrides `quality_threshold` in config (`0`–`1`) |
| `FLOWVISION_DIGIT_PADDING` | Overrides `digit_padding_color` in config (`black` or `white`) |

`src/.env` is loaded at startup. Database schema: [flowvision_db_ddl.sql](flowvision_db_ddl.sql).

## Dependencies

- `requirements.txt` lists the direct dependencies. Edit this file.
- `requirements.lock` pins every package with hashes for Python 3.12. The Docker image
  installs from it. Regenerate it after changing `requirements.txt`:

  ```bash
  uv pip compile requirements.txt --universal --python-version 3.12 \
    --generate-hashes -o requirements.lock
  ```

## Training

```bash
python training/quality/finetune_bfm.py     # quality (good/bad) classifier
python training/color/finetune_color.py     # last-digit colour classifier
```

To deploy a fine-tuned model, point `models.bfm_classification` or
`models.color_classification` in `src/conf/config.yaml` at the new export.
