# BFM OCR: how the folder is laid out

The BFM service reads the counter of a bulk flow meter from a photo and returns the reading. This page
shows where everything is. How to run it: [README.md](README.md). The API contract:
[flowvision_api_spec.yml](flowvision_api_spec.yml).

## Folder structure

```
services/bfm-ocr/
├── src/                          THE SERVICE (everything that runs)
│   ├── run.py                    starts the web server (uvicorn, port 8000)
│   ├── routes.py                 the API endpoints: /extract-reading, /feedback
│   ├── conf/
│   │   ├── config.yaml           ALL SETTINGS: model paths, reader rules, limits
│   │   ├── config.py             loads config.yaml
│   │   ├── logging.py            log files
│   │   └── queries.py            SQL used to store requests and feedback
│   ├── models/
│   │   ├── models.py             request / response formats (not ML models)
│   │   ├── *.onnx                THE READER'S MODELS (see "Models" below)
│   │   ├── bfm_fastai_v2         photo quality check
│   │   └── orientation_yolo11n_cls.onnx        turns sideways photos upright
│   ├── service/
│   │   ├── api/
│   │   │   ├── image_service.py  ONE PHOTO'S JOURNEY, step by step (start reading here)
│   │   │   ├── metadata_service.py   stores each request / response / feedback
│   │   │   └── database.py       PostgreSQL connection
│   │   └── vision/
│   │       ├── inference_utils.py    the reading rules (agreement, limits, half rule)
│   │       ├── yolox_reader.py   the reader: finds the counter, reads the wheels, colour
│   │       ├── counter.py        picks the counter row out of all digits found
│   │       ├── screen_check.py   refuses photos of a screen or of paper
│   │       ├── orientation.py    turns a sideways photo upright
│   │       └── types.py          shared data types
│   └── error/error.py            error responses
├── training/                     retraining the quality and orientation models (and the removed old colour model)
│   ├── data/                     empty image folders the scripts read (photos never go to git)
│   ├── quality/  orientation/  color/
├── scripts/try_photos.py         developer tool: read a folder of photos through the service
├── Dockerfile, .dockerignore     container build (training/ and scripts/ are left out)
├── requirements.txt / .lock      Python packages (the image installs from the .lock)
├── flowvision_api_spec.yml       API description (OpenAPI)
├── flowvision_db_ddl.sql         creates the PostgreSQL table flowvision_extraction_data
├── flowvision.service            systemd unit for running without Docker (see note below)
├── README.md                     how to run, configure and retrain
└── architecture.md               this page
```

Kept off GitHub (git-ignored): `logs/` (runtime logs), the local working folder with field data, and
`src/models/screen_check.onnx` (see below). The automated tests are kept outside this
repository.

## How a photo is read

Everything happens in `ImageService.extract_reading` ([image_service.py](src/service/api/image_service.py)).
Each step can stop and answer; the service refuses rather than guesses.

| # | Step | Code | Refused as |
|---|---|---|---|
| 1 | Download the photo from `imageURL` | `image_service.py` `download_image` | error |
| 2 | Is it a phone screen or paper? | `screen_check.py` | `NOMETER` |
| 3 | Turn it upright if it is sideways | `orientation.py`, `inference_utils.turn_upright` | |
| 4 | Photo quality (good / bad), reported in the answer | `inference_utils.classify_bfm_image` | |
| 5 | Find the counter, read every wheel | `yolox_reader.py` `Reader` | `NOMETER` if no digits |
| 6 | Reading rules: the counter must read the same on several versions of it, every digit sure enough, enough wheels | `inference_utils.recognize_with_yolox_reader`, `read_by_agreement`, `read_counter` | `UNCLEAR` |
| 7 | Colour of the last wheel (red = a decimal wheel), with a second opinion when unsure | `yolox_reader.py` `ColourCheck`, `colour_second_opinion` | `UNCLEAR` if unsure |
| 8 | Answer, and store the request in PostgreSQL in the background | `metadata_service.py`, `queries.py` | |

The answer: `meterReading` is the digits as read (leading zeros kept) and `lastDigitColor` is `red`,
`black` or `unknown`. **When the last wheel is red, the service writes the decimal point itself**
(`"024506.9"`), as the live service on `bfm-phase-1` does; callers use the value as returned.

Turning wheels follow the half rule: when the last wheel is between two digits, the incoming digit is
read once half of it shows (a tie goes to the incoming digit), 9 to 0 included. The digit model learned
this rule. `turning_wheel_lower: true` would take the lower digit instead (JalSoochak's rollover spec), but
only where the reader finds both digits on a wheel, which the current model does not do.

## Models

All models are in one folder, `src/models/`. The reader's models there (ONNX, run with onnxruntime):

| File | Job |
|---|---|
| `bfm_window.onnx` | finds the counter in the photo |
| `bfm_digits.onnx` | reads each wheel |
| `bfm_colour.onnx` | last wheel red or black (looks at the last two wheels) |
| `bfm_colour_single.onnx` | colour second opinion (last wheel alone) |
| `screen_check.onnx` | screen / paper check. **Not in git** (351 MB, over GitHub's 100 MB limit): copy it in by hand. Without it the service still starts, but does not refuse screens and paper. |

The reader is switched on by `yolox_reader: true` in `config.yaml` (or `FLOWVISION_YOLOX_READER`).
Switched off, the service falls back to the old digit and colour models in `src/models/`.

## Settings worth knowing

All in [src/conf/config.yaml](src/conf/config.yaml); each one is explained there. Most can be overridden
at deploy time by an environment variable (listed in the README).

| Setting | What it does |
|---|---|
| `yolox_reader` | use the reader above. Must stay on: Sahaj main's old digit and colour models were removed on 16 Nov (still on GitHub main) |
| `screen_check` | refuse photos of screens and paper |
| `orientation_min_confidence` | how sure the turn-upright model must be |
| `counter_row_only`, `counter_min_digits` | read only the counter row (the switch was for the old digit model only); refuse rows that are too short |
| `reader_min_digit_confidence` | refuse when any digit is less sure than this |
| `reader_versions_agree`, `reader_versions_may_differ` | the counter must read the same on its versions. On (as delivered), this replaces the counter check, whose model (`bfm_gate.onnx`) was removed on 16 Nov as unused; the counter check settings in `config.yaml` change nothing |
| `turning_wheel_allowed` | read turning wheels (off: refuse them); only while `reader_versions_agree` is off |
| `turning_wheel_lower` | off: the half rule (the digit that shows more); on: the lower digit |
| `reader_rule_e` | off: a counter whose versions split between two numbers is refused |
| `reader_colour_unsure`, `reader_red_limit` | when the last wheel's colour counts as unsure / red |

## Where to look when you want to…

| …change | Look in |
|---|---|
| a limit or switch a rule on/off | `src/conf/config.yaml` |
| the order of the steps, or the answer's fields | `src/service/api/image_service.py`, `src/models/models.py` |
| how digits become a reading | `src/service/vision/inference_utils.py` |
| how the counter and colour are found | `src/service/vision/yolox_reader.py`, `counter.py` |
| a model | its file in `src/models/` (paths in `config.yaml`) |
| the API endpoints | `src/routes.py`, `flowvision_api_spec.yml` |
| what is stored in the database | `src/conf/queries.py`, `flowvision_db_ddl.sql` |

## Note on `flowvision.service`

For running on a server without Docker (not used with Docker). Paths updated 14 Nov 2026: it starts `src/run.py`
in `services/bfm-ocr/`, assuming the repository is at `/home/ubuntu/code/arghyam/flowvision` and the packages are
in `/home/ubuntu/code/arghyam/flowvision_venv`; change both if the server differs. Settings such as the database
details go in `/home/ubuntu/code/arghyam/flowvision.env`, outside the repository.
