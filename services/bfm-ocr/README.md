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

Endpoints, served under both `/flowvision/v1/bfm` (use this for new clients) and the original
`/flowvision/v1` (existing clients):

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
| `FLOWVISION_COUNTER_ROW_ONLY` | `false` reads every digit in the photo again, left to right (default on) |
| `FLOWVISION_ORIENTATION` | `off` reads photos as taken, without turning them upright (default on) |

`src/.env` is loaded at startup. Database schema: [flowvision_db_ddl.sql](flowvision_db_ddl.sql).

## How a photo is read

1. A small orientation model turns a sideways or upside-down photo upright (`models.orientation`).
2. The quality check rates the upright photo. A bad photo returns `UNCLEAR`.
3. The digit model finds the digits, and only the meter's counter row is kept (`counter_row_only`).
4. A small drum colour model (`models.drum_colour`) tells whether the last drum is red, the decimal digit, and sets
   `lastDigitColor`. Only a row left with 7 drums is trimmed: a box after the red seventh drum that is off the drum
   pitch or weak (gear teeth, a dial mark) is dropped.

If the orientation model is under 90% sure, the photo is read as taken. If no counter row of 3 or more digits is
found, every digit is read left to right and the drum colour model checks the last digit. The API is the
same in every case. When the last digit is red (and the reading has at least two digits) the service writes the
decimal point into `meterReading` itself (`"012644.8"`), so clients use the reading as returned: see "Decimal point"
in [api-reference.md](../../api-reference.md).
The service does not start without the drum colour model, and a drum check that fails on a photo returns the error
response rather than a reading without its point.

## Tests

```bash
pip install pytest
python -m pytest tests -q
python scripts/try_photos.py /path/to/photos   # each photo with fixes 1-3 off and on
```

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
python training/orientation/train_orientation.py --photos DIR --out DIR   # orientation classifier
```

To deploy a fine-tuned model, point `models.bfm_classification` in `src/conf/config.yaml` at the new export.
