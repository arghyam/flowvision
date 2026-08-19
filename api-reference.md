# API Reference

This document provides a comprehensive reference for the FlowVision REST API, including endpoint specifications, request/response formats, authentication, and error handling patterns. The API enables external clients to upload images and extract meter readings using AI-powered vision services.

### API Overview <a href="#api-overview" id="api-overview"></a>

The FlowVision API is built using FastAPI and exposes a RESTful interface at the base path `/flowvision/v1`. The API follows a three-phase workflow: image upload, reading extraction, and optional feedback submission.

#### API Architecture <a href="#api-architecture" id="api-architecture"></a>

<figure><img src=".gitbook/assets/Screenshot 2025-07-28 at 2.40.30 PM.png" alt=""><figcaption></figcaption></figure>


#### Request/Response Flow <a href="#requestresponse-flow" id="requestresponse-flow"></a>

<figure><img src=".gitbook/assets/Screenshot 2025-07-28 at 2.40.59 PM.png" alt=""><figcaption></figcaption></figure>

---

## Endpoints

| Method | Path                             | Description                               |
| ------ | -------------------------------- | ----------------------------------------- |
| GET    | `/`                              | Health check                              |
| POST   | `/flowvision/v1/uploadImage`     | Upload image to S3 and get presigned URL  |
| POST   | `/flowvision/v1/extract-reading` | Extract meter reading from an image URL   |
| POST   | `/flowvision/v1/feedback`        | Submit accuracy feedback for a reading    |

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

## POST `/flowvision/v1/uploadImage`

Uploads an image to S3 and returns a presigned URL for use in the extraction endpoint.

**Request Format:** `multipart/form-data` with `ImageUploadRequest` model

**Response:** Returns S3 presigned URL with 60-second expiry for subsequent extraction requests.

---

## POST `/flowvision/v1/extract-reading`

Extracts a meter reading from an image hosted at a given URL.

### Request Body (`application/json`)

| Field      | Type                | Required | Description                                      |
| ---------- | ------------------- | -------- | ------------------------------------------------ |
| `id`       | `string (UUID)`     | No       | Client-generated request identifier              |
| `ts`       | `string (datetime)` | No       | Request timestamp in ISO 8601 format             |
| `imageURL` | `string`            | Yes      | URL of the meter image (e.g. S3 presigned URL)   |
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
| `meterReading`                              | `string`          | Raw digit string extracted from the meter (e.g. `"96733"`), when `status` is `SUCCESS`. No decimal adjustment is applied — the downstream system interprets fractional digits based on `lastDigitColor`. For non-`SUCCESS` statuses it carries a placeholder instead of a reading — see the note below. |
| `hasRollover`                               | `boolean`         | `true` if one or more digit wheels were detected mid-rotation (between two digit positions).                                                    |
| `rolloverPositions`                         | `array` \| absent | Present only when `hasRollover` is `true`. Each entry describes one rollover position. Omitted from response when there is no rollover.         |
| `rolloverPositions[].position`              | `integer`         | 1-indexed position of the rollover digit in the reading, counted left to right.                                                                 |
| `rolloverPositions[].selectedDigit.value`   | `integer`         | The digit value chosen by the model (highest-confidence detection at this position).                                                            |
| `rolloverPositions[].selectedDigit.confidence` | `float`        | Model confidence for the selected digit (`0.0` – `1.0`).                                                                                       |
| `rolloverPositions[].alternateDigit.value`  | `integer`         | The next-best digit candidate at this position.                                                                                                 |
| `rolloverPositions[].alternateDigit.confidence` | `float`       | Model confidence for the alternate digit (`0.0` – `1.0`).                                                                                      |
| `processingTime`                            | `float`           | Total server-side processing time in seconds.                                                                                                   |
| `qualityStatus`                             | `string`          | Image quality classification: `"good"` or `"bad"`.                                                                                             |
| `qualityConfidence`                         | `float`           | Model confidence for the quality classification (`0.0` – `1.0`).                                                                               |
| `lastDigitColor`                            | `string`          | Detected color of the last digit wheel: `"red"`, `"black"`, or `"unknown"` (when no digits were detected).                                     |
| `colorConfidence`                           | `float`           | Model confidence for the color classification (`0.0` – `1.0`).                                                                                 |

> `data` is always present in the response regardless of `status`. `meterReading` only holds a real reading when `status` is `SUCCESS`; otherwise it carries a fixed placeholder string, never a number:
>
> | `status`  | `meterReading` value                        |
> | --------- | ------------------------------------------- |
> | `SUCCESS` | the extracted digit string, e.g. `"96733"`  |
> | `NOMETER` | `"No digits detected in the image"`          |
> | `UNCLEAR` | `"Image quality too poor for recognition"`  |
>
> Clients must branch on `status` and must not attempt to parse `meterReading` unless `status` is `SUCCESS`.

**Example — successful reading with rollover**
```json
{
  "id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "ts": "2026-06-25T10:30:01Z",
  "responseCode": "OK",
  "statusCode": 200,
  "result": {
    "status": "SUCCESS",
    "correlationId": "f9e8d7c6-b5a4-3210-fedc-ba9876543210",
    "data": {
      "meterReading": "96733",
      "hasRollover": true,
      "rolloverPositions": [
        {
          "position": 3,
          "selectedDigit": { "value": 7, "confidence": 0.94 },
          "alternateDigit": { "value": 6, "confidence": 0.91 }
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
      "colorConfidence": 0.0
    }
  }
}
```

**Error — HTTP 500**

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
| `errorMsg`  | `string`  | Exception message from the server                  |

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

## POST `/flowvision/v1/feedback`

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
| `status` | `string (enum)` | `"SUBMITTED"` if feedback was stored; `"FAILED"` if it could not be |

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

**Error — HTTP 500**

Same envelope as the extract-reading error response above.

---

## Status Values

### Extraction Status (`result.status` in extract-reading)

| Value     | Description                                         |
| --------- | --------------------------------------------------- |
| `SUCCESS` | Meter reading extracted successfully                |
| `NOMETER` | Image passed the quality check but no digits were detected in it — e.g. the photo is not of a meter |
| `UNCLEAR` | Image quality too poor to extract a reading         |

### Feedback Status (`result.status` in feedback)

| Value       | Description                  |
| ----------- | ---------------------------- |
| `SUBMITTED` | Feedback accepted and stored |
| `FAILED`    | Feedback could not be stored |

### Response Code (`responseCode`)

| Value   | Meaning                    |
| ------- | -------------------------- |
| `OK`    | Request processed normally |
| `ERROR` | An error occurred          |

---

### Background Task Processing <a href="#background-task-processing" id="background-task-processing"></a>

The API uses FastAPI's `BackgroundTasks` for asynchronous operations:

* **Request Logging:** All extraction requests are logged to PostgreSQL via `MetadataStore`
* **Response Logging:** Extraction results and processing metadata are persisted
* **Feedback Storage:** User feedback is stored with correlation to original requests

This pattern ensures API responses return quickly while maintaining comprehensive audit trails.


### Service Dependencies <a href="#service-dependencies" id="service-dependencies"></a>

The FastAPI application initializes core services at startup:

| Service          | Purpose                                        | Configuration                              |
| ---------------- | ---------------------------------------------- | ------------------------------------------ |
| `ImageService`   | Orchestrates meter reading extraction workflow | Uses `Config` for vision service selection |
| `StorageService` | Manages S3 image uploads and presigned URLs    | Configured via AWS credentials             |
| `Config`         | Centralized configuration management           | Loads from `config.yaml` and environment   |
