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
| `FLOWVISION_DIGIT_PADDING` | Overrides `digit_padding_color` in config (`black` or `white`) |
| `FLOWVISION_COUNTER_ROW_ONLY` | `false` reads every digit in the photo again, left to right (default on) |
| `FLOWVISION_ORIENTATION` | `off` reads photos as taken, without turning them upright (default on) |
| `FLOWVISION_DECIMAL_DRUM` | `off` goes back to the fastai colour classifier for the last digit (default on) |
| `FLOWVISION_DECIMAL_IN_READING` | `true` writes the decimal point into `meterReading` and adds `decimalInReading: true` to the response; clients must then not place it again (default off). The service refuses to start if it could not write the point: see below |

`src/.env` is loaded at startup. Database schema: [flowvision_db_ddl.sql](flowvision_db_ddl.sql).

## How a photo is read

1. A small orientation model turns a sideways or upside-down photo upright (`models.orientation`).
2. The quality check rates the upright photo. A bad photo returns `UNCLEAR`.
3. The digit model finds the digits, and only the meter's counter row is kept (`counter_row_only`).
4. A small drum colour model (`models.drum_colour`) tells whether the last drum is red, the decimal digit, and sets
   `lastDigitColor`. Only a row left with 7 drums is trimmed: a box after the red seventh drum that is off the drum
   pitch or weak (gear teeth, a dial mark) is dropped.

If the orientation model is under 90% sure, the photo is read as taken. If no counter row of 3 or more digits is
found, every digit is read left to right and the fastai classifier checks the last digit's colour. The API is the
same in every case. By default `meterReading` is the raw digits and `lastDigitColor` says whether the last digit is the
decimal. With `FLOWVISION_DECIMAL_IN_READING=true` the service writes the point itself when the drum colour model finds a
red last drum, and every response carries `decimalInReading: true`: see "Decimal point" in
[api-reference.md](../../api-reference.md).

With that setting on, the service does not start unless it can write the point. `FLOWVISION_DECIMAL_DRUM=off`,
`FLOWVISION_COUNTER_ROW_ONLY=false`, an empty `models.drum_colour` or a drum colour model that fails to load stops startup
with an error naming the cause.
Each worker runs this check when it starts, and one worker failing stops the service. To go back, set
`FLOWVISION_DECIMAL_IN_READING=false` first, or together with those switches. Clients must read `decimalInReading` before
the setting is turned on: one that does not places a second point.

## Tests

```bash
pip install pytest
python -m pytest tests -q
python scripts/try_photos.py /path/to/photos   # each photo with the fixes off and on
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
python training/color/finetune_color.py     # last-digit colour classifier
python training/orientation/train_orientation.py --photos DIR --out DIR   # orientation classifier
```

To deploy a fine-tuned model, point `models.bfm_classification` or
`models.color_classification` in `src/conf/config.yaml` at the new export.
