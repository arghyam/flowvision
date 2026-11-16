# API Reference

This document provides a comprehensive reference for the FlowVision REST API, including endpoint specifications, request/response formats and error handling patterns. The service has no authentication of its own: it must only be reachable from the client's backend (see the BFM README, "Before going live on AWS"). The API enables external clients to extract meter readings from images hosted at a URL using AI-powered vision services.

### API Overview <a href="#api-overview" id="api-overview"></a>

The FlowVision API is built using FastAPI and exposes a RESTful interface at the base path `/flowvision/v1/bfm`. The original base path `/flowvision/v1` serves the same endpoints for existing clients. The API follows a two-phase workflow: reading extraction from an image URL, and optional feedback submission linked to the extraction by its `correlationId`.

**Every answer comes with HTTP status 200, errors included.** Check `responseCode` (`OK` / `ERROR`) and then `result.status`. Only a request body that fails validation gets a real HTTP 422.

---

## Endpoints

Each POST endpoint is served under both base paths: `/flowvision/v1/bfm` for new clients and `/flowvision/v1` for existing clients. Requests and responses are identical on both.

| Method | Path                                                                     | Description                             |
| ------ | ------------------------------------------------------------------------ | --------------------------------------- |
| GET    | `/`                                                                      | Health check                            |
| POST   | `/flowvision/v1/bfm/extract-reading`<br>`/flowvision/v1/extract-reading` | Extract meter reading from an image URL |
| POST   | `/flowvision/v1/bfm/feedback`<br>`/flowvision/v1/feedback`               | Submit accuracy feedback for a reading  |

---

## GET `/`

Returns API health status.

**Response**
```json
{
  "message": "Hi, I am the meter reading assistant."
}
```

---

## POST `/flowvision/v1/bfm/extract-reading`

Also served at `/flowvision/v1/extract-reading` for existing clients.

Extracts a meter reading from an image hosted at a given URL.

### Request Body (`application/json`)

| Field      | Type                | Required | Description                                      |
| ---------- | ------------------- | -------- | ------------------------------------------------ |
| `id`       | `string (UUID)`     | No       | Client-generated request identifier              |
| `ts`       | `string (datetime)` | No       | Request timestamp in ISO 8601 format             |
| `imageURL` | `string`            | Yes      | URL of the meter image (e.g. S3 presigned URL). The download must finish within 30 s; photos over 10 MB or 60 megapixels are refused with an error |
| `metadata` | `object`            | No       | Arbitrary key-value pairs for additional context |

**Example**
```json
{
  "id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "ts": "2026-06-25T10:30:00Z",
  "imageURL": "https://s3.amazonaws.com/bucket/meter-image.jpg",
  "metadata": {
    "location": "zone-A",
    "deviceId": "meter-001"
  }
}
```

### Response Body (`application/json`)

**Success — HTTP 200**

| Field          | Type            | Description                                     |
| -------------- | --------------- | ----------------------------------------------- |
| `id`           | `string (UUID)` | Echoed request identifier                        |
| `ts`           | `string`        | Response timestamp                               |
| `responseCode` | `string`        | Always `"OK"` on success                         |
| `statusCode`   | `integer`       | HTTP status code (`200`)                         |
| `result`       | `object`        | Extraction result (see below)                    |

**`result` object**

| Field           | Type            | Description                                                 |
| --------------- | --------------- | ----------------------------------------------------------- |
| `status`        | `string (enum)` | Outcome of extraction — see [Status Values](#status-values) |
| `correlationId` | `string (UUID)` | Unique ID linking this result to a feedback submission      |
| `data`          | `object`        | Reading details; always present regardless of `status`      |

**`result.data` object**

| Field                                       | Type              | Description                                                                                                                                     |
| ------------------------------------------- | ----------------- | ----------------------------------------------------------------------------------------------------------------------------------------------- |
| `meterReading`                              | `string`          | The reading, when `status` is `SUCCESS`. Always a string; leading zeros are kept. When the last digit wheel is red (`lastDigitColor` `"red"`), the service writes the decimal point before it (e.g. `"024506.9"` = 24506.9); otherwise it is digits only (e.g. `"0268045"`). Use it as returned: do not divide by 10 or place a point yourself. A wheel caught between two digits gives the digit that shows more: the next digit once half of it shows, otherwise the current one (9 to 0 the same way, with the carry). For non-`SUCCESS` statuses it carries a placeholder instead of a reading — see the note below. |
| `hasRollover`                               | `boolean`         | `true` when the service found two digits on one wheel (turning) and chose one. The current digit model reads one digit per wheel, so this is normally `false`. |
| `rolloverPositions`                         | `array` \| absent | Present only when `hasRollover` is `true`. Each entry describes one rollover position. Omitted from response when there is no rollover.         |
| `rolloverPositions[].position`              | `integer`         | 1-indexed position of the rollover digit in the reading, counted left to right. Digits only: the decimal point is not counted.                                                                 |
| `rolloverPositions[].selectedDigit.value`   | `integer`         | The digit used in `meterReading` at this position.                                                                                              |
| `rolloverPositions[].selectedDigit.confidence` | `float`        | Model confidence for the selected digit (`0.0` – `1.0`).                                                                                       |
| `rolloverPositions[].alternateDigit.value`  | `integer`         | The other digit seen on the turning wheel.                                                                                                      |
| `rolloverPositions[].alternateDigit.confidence` | `float`       | Model confidence for the alternate digit (`0.0` – `1.0`).                                                                                      |
| `processingTime`                            | `float`           | Server-side time in seconds to download and read the photo (the last wheel's colour check is not included).                                    |
| `qualityStatus`                             | `string`          | Image quality classification: `"good"` or `"bad"`; `"not checked"` for a photo of a screen or paper, refused before this check. A `bad` photo can still be read when every reading check passes. |
| `qualityConfidence`                         | `float`           | Model confidence for the quality classification (`0.0` – `1.0`).                                                                               |
| `lastDigitColor`                            | `string`          | Detected color of the last digit wheel: `"red"` (a decimal wheel), `"black"`, or `"unknown"` (no digits read, or the colour was too unsure, which refuses the reading). |
| `colorConfidence`                           | `float`           | Model confidence for the color classification (`0.0` – `1.0`).                                                                                 |
| `operatorMessage` | `string` | Only when `status` is not `SUCCESS`: what the operator should do, e.g. take another photo avoiding glare. Left out with a reading. |

> `data` is always present in the response regardless of `status`. `meterReading` only holds a real reading when `status` is `SUCCESS`; otherwise it carries a fixed placeholder string, never a number:
>
> | `status`  | `meterReading` value                        |
> | --------- | ------------------------------------------- |
> | `SUCCESS` | the extracted digit string, e.g. `"96733"`  |
> | `NOMETER` | `"No digits detected in the image"`          |
> | `NOMETER` | `"Photo of a screen or paper, not the meter"` |
> | `UNCLEAR` | `"Image quality too poor for recognition"`  |
>
> Clients must branch on `status` and must not attempt to parse `meterReading` unless `status` is `SUCCESS`.

**Example — successful reading** (red last wheel: the service writes the point, the reading is 24506.9)
```json
{
  "id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "ts": "2026-06-25T10:30:01.123456",
  "responseCode": "OK",
  "statusCode": 200,
  "result": {
    "status": "SUCCESS",
    "correlationId": "f9e8d7c6-b5a4-3210-fedc-ba9876543210",
    "data": {
      "meterReading": "024506.9",
      "hasRollover": false,
      "processingTime": 1.23,
      "qualityStatus": "good",
      "qualityConfidence": 0.95,
      "lastDigitColor": "red",
      "colorConfidence": 0.98
    }
  }
}
```

**Example — reading with a turning wheel** (rare: the current digit model reads one digit per wheel). The last wheel
shows 9 coming in and 8 going out; 9 shows more, so the reading uses 9.
```json
{
  "id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "ts": "2026-06-25T10:30:01.123456",
  "responseCode": "OK",
  "statusCode": 200,
  "result": {
    "status": "SUCCESS",
    "correlationId": "f9e8d7c6-b5a4-3210-fedc-ba9876543210",
    "data": {
      "meterReading": "024506.9",
      "hasRollover": true,
      "rolloverPositions": [
        {
          "position": 7,
          "selectedDigit": { "value": 9, "confidence": 0.92 },
          "alternateDigit": { "value": 8, "confidence": 0.81 }
        }
      ],
      "processingTime": 1.23,
      "qualityStatus": "good",
      "qualityConfidence": 0.95,
      "lastDigitColor": "red",
      "colorConfidence": 0.98
    }
  }
}
```

**Example — reading refused** (the operator is asked for a new photo)
```json
{
  "id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "ts": "2026-06-25T10:30:01.123456",
  "responseCode": "OK",
  "statusCode": 200,
  "result": {
    "status": "UNCLEAR",
    "correlationId": "f9e8d7c6-b5a4-3210-fedc-ba9876543210",
    "data": {
      "meterReading": "Image quality too poor for recognition",
      "hasRollover": false,
      "processingTime": 1.05,
      "qualityStatus": "bad",
      "qualityConfidence": 0.88,
      "lastDigitColor": "unknown",
      "colorConfidence": 0.0,
      "operatorMessage": "..."
    }
  }
}
```

**Example — no meter detected**
```json
{
  "id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "ts": "2026-06-25T10:30:01Z",
  "responseCode": "OK",
  "statusCode": 200,
  "result": {
    "status": "NOMETER",
    "correlationId": "f9e8d7c6-b5a4-3210-fedc-ba9876543210",
    "data": {
      "meterReading": "No digits detected in the image",
      "hasRollover": false,
      "processingTime": 0.85,
      "qualityStatus": "good",
      "qualityConfidence": 0.91,
      "lastDigitColor": "unknown",
      "colorConfidence": 0.0,
      "operatorMessage": "No meter counter was found. Please take a close photo of the meter's number window, straight on."
    }
  }
}
```

**Error** (still HTTP 200; `statusCode` in the body is `500`). For example the photo address gives 404, the
download takes longer than 30 s, or the photo is over 10 MB / 60 megapixels or is not an image.

| Field          | Type      | Description       |
| -------------- | --------- | ----------------- |
| `id`           | `string`  | Echoed request ID |
| `ts`           | `string`  | Response timestamp |
| `responseCode` | `string`  | Always `"ERROR"`  |
| `statusCode`   | `integer` | `500`             |
| `error`        | `object`  | Error details     |

**`error` object**

| Field       | Type      | Description                                        |
| ----------- | --------- | -------------------------------------------------- |
| `errorCode` | `integer` | `500` — the only value produced by current routes  |
| `errorMsg`  | `string`  | What went wrong, e.g. `"Photo is larger than 10 MB"` |

```json
{
  "id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "ts": "2026-06-25T10:30:01Z",
  "responseCode": "ERROR",
  "statusCode": 500,
  "error": {
    "errorCode": 500,
    "errorMsg": "..."
  }
}
```

**Validation Error — HTTP 422**

Returned by FastAPI when the request body fails schema validation.

---

## POST `/flowvision/v1/bfm/feedback`

Also served at `/flowvision/v1/feedback` for existing clients.

Submits human accuracy feedback for a previously extracted reading, linked via `correlationId`.

### Request Body (`application/json`)

| Field           | Type                | Required | Description                                              |
| --------------- | ------------------- | -------- | -------------------------------------------------------- |
| `id`            | `string (UUID)`     | No       | Client-generated request identifier                      |
| `ts`            | `string (datetime)` | No       | Request timestamp in ISO 8601 format                     |
| `correlationId` | `string (UUID)`     | Yes      | `correlationId` from the `extract-reading` response      |
| `data`          | `object`            | Yes      | Feedback payload (see below)                             |

**`data` object**

| Field       | Type      | Required | Description                                        |
| ----------- | --------- | -------- | -------------------------------------------------- |
| `accurate`  | `boolean` | Yes      | Whether the extracted reading was correct          |
| `extracted` | `float`   | No       | The value that was extracted by the model          |
| `actual`    | `float`   | No       | The true meter reading as verified by the operator |

**Example**
```json
{
  "id": "b2c3d4e5-f6a7-8901-bcde-f12345678901",
  "ts": "2026-06-25T10:35:00Z",
  "correlationId": "f9e8d7c6-b5a4-3210-fedc-ba9876543210",
  "data": {
    "accurate": false,
    "extracted": 24506.9,
    "actual": 24510.0
  }
}
```

### Response Body (`application/json`)

**Success — HTTP 200**

| Field          | Type      | Description                |
| -------------- | --------- | -------------------------- |
| `id`           | `string`  | Echoed request ID          |
| `ts`           | `string`  | Response timestamp         |
| `responseCode` | `string`  | `"OK"`                     |
| `statusCode`   | `integer` | `200`                      |
| `result`       | `object`  | Feedback submission status |

**`result` object**

| Field    | Type            | Description                                                          |
| -------- | --------------- | -------------------------------------------------------------------- |
| `status` | `string (enum)` | `"SUBMITTED"` when the feedback was accepted (it is stored in the background afterwards, so a database problem is only logged); `"FAILED"` if it could not be accepted |

```json
{
  "id": "b2c3d4e5-f6a7-8901-bcde-f12345678901",
  "ts": "2026-06-25T10:35:01Z",
  "responseCode": "OK",
  "statusCode": 200,
  "result": {
    "status": "SUBMITTED"
  }
}
```

**Error**

Same envelope as the extract-reading error response above (still HTTP 200).

---

## Status Values

### Extraction Status (`result.status` in extract-reading)

| Value     | Description                                         |
| --------- | --------------------------------------------------- |
| `SUCCESS` | Meter reading extracted successfully                |
| `NOMETER` | No meter counter was found in the photo, or the photo is of a phone/computer screen or of paper |
| `UNCLEAR` | The counter could not be read safely: no counter row, a digit missing from the row, too few digits, a digit too uncertain, the counter reading differently on its versions, or the last wheel's colour too unsure. The service refuses rather than guesses. |

### Feedback Status (`result.status` in feedback)

| Value       | Description                  |
| ----------- | ---------------------------- |
| `SUBMITTED` | Feedback accepted; it is stored in the background afterwards |
| `FAILED`    | Feedback could not be stored |

### Response Code (`responseCode`)

| Value   | Meaning                    |
| ------- | -------------------------- |
| `OK`    | Request processed normally |
| `ERROR` | An error occurred          |

---

### Background Task Processing <a href="#background-task-processing" id="background-task-processing"></a>

The API uses FastAPI's `BackgroundTasks` for asynchronous operations:

* **Request Logging:** All extraction requests are logged to PostgreSQL via `MetadataStore` (when `FLOWVISION_DB_*` is set; table from `services/bfm-ocr/flowvision_db_ddl.sql`)
* **Response Logging:** Extraction results and processing metadata are persisted
* **Feedback Storage:** User feedback is stored with correlation to original requests

This pattern ensures API responses return quickly while maintaining comprehensive audit trails.


### Service Dependencies <a href="#service-dependencies" id="service-dependencies"></a>

The FastAPI application initializes core services at startup:

| Service          | Purpose                                        | Configuration                              |
| ---------------- | ---------------------------------------------- | ------------------------------------------ |
| `ImageService`   | Orchestrates meter reading extraction workflow | Uses `Config` for vision service selection |
| `Config`         | Centralized configuration management           | Loads from `config.yaml` and environment   |
