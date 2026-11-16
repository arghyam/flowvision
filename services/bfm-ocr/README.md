# BFM OCR (FlowVision)

Extracts numeric readings from bulk flow meter (BFM) images. API contract:
[flowvision_api_spec.yml](flowvision_api_spec.yml) and [api-reference.md](../../api-reference.md).
Folder layout and where the important files are: [architecture.md](architecture.md).

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

## Photos that are not meters

Only a photo that shows a meter counter can get a reading. Anything else, including adult or other
inappropriate photos, is refused as `UNCLEAR` or `NOMETER` and never read. The service keeps no photo,
only its address (`image_url`). It cannot stop a user sending such a photo: that belongs in the app,
which knows the sender and holds the photo. Such photos must never be used for training, review or
testing.

## Docker

```bash
docker build -t flowvision services/bfm-ocr      # from the repo root
docker run -p 8000:8000 flowvision
```

Workers are auto-sized from CPU/GPU count. Each worker loads every model, so set
`-e WORKERS=1` on low-memory hosts. `TIMEOUT`, `LOG_LEVEL` and the other gunicorn settings
can be overridden the same way (see the [Dockerfile](Dockerfile)).

### Before going live on AWS: checklist

The service has no login of its own and fetches whatever photo address it is sent, so its safety comes from where it
runs.

1. **Keep it private.** Only JalSoochak's backend should reach port 8000: a security group that allows that source
   and nothing else, not open to the internet. `/docs` and `/openapi.json` are open to anyone who can reach the port.
2. **Require IMDSv2** on the EC2 instance (`HttpTokens=required`; hop limit 1, or 2 if Docker must reach it). Then a
   photo address pointing at `169.254.169.254` cannot read the instance's details or keys.
3. **Limit outgoing traffic** if possible: the photo storage (S3) and the database are all the service needs.
4. **Copy `screen_check.onnx`** (351 MB, not in git) into `src/models/` before `docker build`. Without
   it the service starts and reads photos, but does not refuse photos of screens and paper; the startup log then says
   so.
5. **Database:** run [flowvision_db_ddl.sql](flowvision_db_ddl.sql) once and set `FLOWVISION_DB_*`. Without them the
   service still answers, but stores nothing and logs an error for each request.
6. **Memory:** each worker loads every model, about 0.75 GB and up to 1.2 GB on a large photo. Allow 1.5 GB per
   worker; on a 2–4 GB host set `WORKERS=1`.
7. **Load balancer:** health check path `/` (the service answers about 20 s after starting; give it a grace period of
   at least 60 s). The image's keep-alive is 65 s, longer than the ALB's default idle timeout of 60 s, as the ALB
   needs; if the ALB timeout is raised, set `KEEPALIVE` above it.
8. **Logs** are written to `logs/` inside the container (30 days kept) and are lost when the container is replaced:
   mount a volume there or ship them to CloudWatch. They contain the photo addresses.
9. **Build once and send one test photo** before going live: the image has not yet been built on Linux. It is large
   (several GB), because the package list installs the graphics-card build of PyTorch, which this CPU service does
   not use.

Already built in: the photo download gives up after 30 s and refuses photos over 10 MB or 60 megapixels; database
writes give up after a few seconds; `YOLO_OFFLINE=true` stops the orientation model's library from contacting the
internet.

## Configuration

| Variable | Purpose |
| --- | --- |
| `CONFIG_PATH` | Config file (default `src/conf/config.yaml`) |
| `FLOWVISION_DB_{HOST,PORT,NAME,USERNAME,PASSWORD}` | PostgreSQL connection |
| `FLOWVISION_QUALITY_THRESHOLD` | Overrides `quality_threshold` in config (`0`–`1`) |
| `FLOWVISION_SCREEN_CHECK` | `off` reads photos of a phone screen or paper instead of refusing them (default on; model `src/models/screen_check.onnx`, kept local) |
| `FLOWVISION_ORIENTATION` | `off` reads photos as taken, without turning them upright first (default on) |
| `FLOWVISION_COUNTER_ROW_ONLY` | `false` reads every digit in the photo instead of only the counter row (default on). Only for the old digit model (removed); the reader always reads the counter row only |
| `FLOWVISION_DIGIT_PADDING` | Overrides `digit_padding_color` in config (`black` or `white`) |

`src/.env` is loaded at startup. Database schema: [flowvision_db_ddl.sql](flowvision_db_ddl.sql).

## Tests

The automated tests are kept outside this repository.

## Dependencies

- `requirements.txt` lists the direct dependencies. Edit this file.
- `requirements.lock` pins every package with hashes for Python 3.12. The Docker image
  installs from it. Regenerate it after changing `requirements.txt`:

  ```bash
  uv pip compile requirements.txt --universal --python-version 3.12 \
    --generate-hashes -o requirements.lock
  ```

## Training

`training/` is only for retraining models; the service never reads it and it is left out of the
Docker image.

```bash
python training/quality/finetune_bfm.py      # photo quality (good/bad) check
python training/orientation/train_orientation.py --photos /path/to/upright --out /path/to/work
                                             # turns sideways photos upright
python training/color/finetune_color.py      # old last-digit colour model (used only when the YOLOX reader is off)
```

Put the images in `training/data/` before running the quality or colour script (git ignores any
photos placed there):

```
training/data/train/quality/good, bad    training/data/test/images        (quality)
training/data/train/color/red, black     training/data/test/color/red, black (colour)
```

To deploy a fine-tuned model, point `models.bfm_classification` or `models.orientation`
in `src/conf/config.yaml` at the new export (the orientation model as ONNX). The YOLOX reader's
models (the `.onnx` files in `src/models/`) are trained separately.
