# Meter reader API: integration guide for the JalSoochak central system

This service reads the cumulative energy reading (kWh or kVAh) from a photo of an electricity meter. The central system sends one request per photo and gets the reading back in the response, usually within 20–30 seconds.

Base URL: `https://<flowvision-host>/flowvision/v1/elm`. The service's operators give you the host and an API key. The same API is also served, unchanged, under the earlier base path `/meter-reader/v1`, for clients that already use it; new integrations use `/flowvision/v1/elm`.

## Contents

1. [Authentication](#1-authentication)
2. [Read a meter photo](#2-read-a-meter-photo)
3. [The response](#3-the-response)
4. [Timeouts and retries](#4-timeouts-and-retries)
5. [Errors](#5-errors)
6. [Feedback: confirm or correct a reading](#6-feedback-confirm-or-correct-a-reading)
7. [Meter settings per consumer (optional)](#7-meter-settings-per-consumer-optional)
8. [Looking up a stored reading or photo](#8-looking-up-a-stored-reading-or-photo)
9. [Health endpoints](#9-health-endpoints)
10. [Checklist before going live](#10-checklist-before-going-live)

## 1. Authentication

Send the API key in the `X-API-Key` header on every request. When the key is missing or wrong, the response is HTTP 401 with error code `ERROR_09`.

During a key rotation the service accepts both the old and the new key, so you can switch without downtime.

## 2. Read a meter photo

`POST /flowvision/v1/elm/extract-reading` with a JSON body:

```json
{
  "id": "JS-2026-09-30-000123",
  "imageURL": "https://images.jalsoochak.example.org/photos/000123.jpg",
  "consumerId": "PSPCL-000123",
  "capturedAt": "2026-09-30T10:15:00+05:30",
  "village": "Akalgarh",
  "agentId": "A7"
}
```

Every field is either **required** (the request is refused without it) or **optional** (the request works without it). For the optional fields, the third column says what you lose by leaving them out; `id`, `consumerId` and `capturedAt` should be sent on every request.

| Field | Required? | If you leave it out | Meaning |
|---|---|---|---|
| `imageURL` | **Yes**: exactly one of `imageURL` and `imageBase64` | Sending neither, or both, is refused with 400 `ERROR_06`. | Where the service downloads the photo from (your image endpoint). It must be reachable from the service. If the endpoint needs credentials, the operators configure them on the service side; they are sent only over HTTPS, or over plain HTTP to a private address when the operators allow private image hosts. A link with a user name or password in it (`https://user:pw@host/...`) is refused. Signed URLs work; the service stores and logs the URL without its query string and fragment. |
| `imageBase64` | **Yes**: exactly one of `imageURL` and `imageBase64` | As above. | The photo itself, base64-encoded (JPEG, PNG or WebP, at most 20 MB). Use this when the service cannot reach your image endpoint. |
| `id` | No, but always send it | The service creates an id and returns it. A retry after a timeout then cannot be matched to the first attempt, so the photo may be read, and paid for, twice. | **Your** id for this request: 1–128 characters from letters, digits, `.`, `_` and `-`, starting with a letter or digit. It is also the retry key: see [section 4](#4-timeouts-and-retries). Use a new id for every new photo. |
| `consumerId` | No, but always send it | The consumer's meter settings and the previous-reading check are skipped, so more readings go to review instead of being accepted. | Your consumer or meter account id (a string or a number, up to 128 characters). The service uses it for the meter's settings and to compare against that consumer's previous reading. |
| `capturedAt` | No, but always send it | The time the request arrived is used, which is wrong when a photo is uploaded later; the previous-reading check then uses the wrong date. | When the photo was taken, as an ISO 8601 date-time between the years 2000 and 2100. A time without a zone (`2026-09-30T10:15:00`) is taken as India time. Send the capture time, not the upload or message time. |
| `previousReading` | No | The service uses the previous reading it has stored for this consumer. | `{"value": "0019650.2", "unit": "kWh", "capturedAt": "..."}` (`value` is required inside it). It overrides the stored previous reading. |
| `metadata` | No | Nothing extra is stored. | Any JSON object; it is stored with the reading. Text with the NUL character, and NaN or Infinity numbers, are refused here and in any other field. |
| anything else | No | Nothing extra is stored. | Any other fields (`village`, `agentId`, `meterNumber`...) are accepted and stored with the reading. |

The same request can be sent as a multipart form to `POST /flowvision/v1/elm/extract-reading/upload`: the photo in the file field `image` (required), plus the optional text fields `id`, `consumerId`, `capturedAt`, `previousValue`, `previousUnit`, `previousCapturedAt` and `metadata` (a JSON object as text). Other form fields are ignored.

The service returns the same `id` you sent. The exceptions are errors refused before the request is read: 401 `ERROR_09`, 413 `ERROR_003` for a body over 30 MB, 503 `ERROR_10` "Too many large photos", and invalid JSON or an invalid `id`. These carry a new id and are not stored.

Two things to watch:
- **Field names are case-sensitive.** A misspelled `consumerID` is stored as an extra field, and the consumer checks are skipped.
- **Photos without a meter** return a normal response with status `NOMETER`, not an error.

## 3. The response

```json
{
  "id": "JS-2026-09-30-000123",
  "ts": "2026-09-30T04:45:21.512Z",
  "responseCode": "OK",
  "statusCode": 200,
  "result": {
    "status": "SUCCESS",
    "correlationId": "5f0c2a8e-1b7d-4c1e-9d3a-2f7e0b6a9c41",
    "data": {
      "meterReading": "0019712.7",
      "action": "ACCEPT",
      "unit": "kWh",
      "meterBrand": "HPL PPSM 01",
      "decimalSource": "registry",
      "uncertainDigits": [],
      "reasons": [],
      "meter": {"typeId": "hpl_ppsm01", "make": "HPL", "modelCode": "PPSM 01"},
      "processingTime": 19.4,
      "costEstimateUsd": 0.023
    }
  }
}
```

**Decide what to do with `result.data.action`** (or `result.status`):

| `status` | `action` | What it means | What to do |
|---|---|---|---|
| `SUCCESS` | `ACCEPT` | The reading passed every check. | Use `meterReading`. |
| `UNCLEAR` | `RETAKE` | Wrong photo: not the total screen (e.g. a tariff T1/T2 or bill screen), no unit visible, or the wrong unit. | Ask for a new photo and show `retakeHint` to the person taking it. `meterReading` holds the model's best reading, for information only; when the model saw no digits at all it holds a short sentence instead. |
| `NOMETER` | `RETAKE` | No meter display is visible. | Ask for a new photo. `meterReading` holds a short explanation, not a number. |
| `NEEDS_REVIEW` | `REVIEW` | The photo was hard to read (glare, an unclear or possibly missing digit, low confidence), or the reading failed a consistency check: wrong digit count, uncertain decimal point, lower than the previous reading, or an implausible jump. | A person checks `meterReading` against the photo, starting with the digits in `uncertainDigits` (if it is empty, check every digit), then confirms or corrects it through `/feedback`. A `?` in `meterReading` is a digit the model could not guess, and when the model could not read a single digit `meterReading` holds a short sentence: in both cases send the correct reading as `actual`. |

### Every possible outcome

Every reading response has one of the four statuses below. Except for `SUCCESS`, `reasons` names why. Usually it has one entry; when it has more, the first is the main one. `reasons[].message` and `retakeHint` are fixed texts, listed here exactly as they are returned. A `RETAKE` carries the `retakeHint` of its first reason; a `REVIEW` or `ACCEPT` has no `retakeHint`.

| `status` / `action` | `reasons[].code` | `reasons[].message` | `retakeHint` |
|---|---|---|---|
| `SUCCESS` / `ACCEPT` | none | none | none |
| `NOMETER` / `RETAKE` | `NO_DISPLAY` | No meter display is visible, or it is blank. | Take the photo straight at the meter's display, with the display switched on. |
| `UNCLEAR` / `RETAKE` | `WRONG_SCREEN` | The display is showing another screen, not the cumulative energy reading. | Wait until the display shows the total kWh (or kVAh) reading, then take the photo. |
| `UNCLEAR` / `RETAKE` | `UNIT_NOT_VISIBLE` | No kWh or kVAh label was seen, so it is not certain which energy total is shown (these meters show both). | Take the photo so the whole display is visible, including the kWh or kVAh label next to the reading. |
| `UNCLEAR` / `RETAKE` | `UNIT_MISMATCH` | The display shows a different unit from the one billed for this meter. | Wait until the display shows the billed unit (kWh or kVAh), then take the photo. |
| `NEEDS_REVIEW` / `REVIEW` | `UNREADABLE` | The reading could not be read with certainty. | none |
| `NEEDS_REVIEW` / `REVIEW` | `DIGITS_UNCLEAR` | One or more digits are unclear, or a digit may be missing. | none |
| `NEEDS_REVIEW` / `REVIEW` | `LOW_CONFIDENCE` | The model was not confident enough in the reading. | none |
| `NEEDS_REVIEW` / `REVIEW` | `BAD_OUTPUT` | The model's answer was internally inconsistent. | none |
| `NEEDS_REVIEW` / `REVIEW` | `DIGIT_COUNT_MISMATCH` | The number of digits does not match this meter type; a digit may have been dropped. | none |
| `NEEDS_REVIEW` / `REVIEW` | `DECIMAL_UNCERTAIN` | Meter type unknown and no decimal point was seen, so the decimal position is uncertain. Set this consumer's meter type to accept readings automatically. | none |
| `NEEDS_REVIEW` / `REVIEW` | `NO_CROSS_CHECK` | The model was not confident enough to accept this reading on its own, and this meter has no known digit count or previous reading to cross-check it. Set the consumer's meter type, or confirm this reading through feedback. | none |
| `NEEDS_REVIEW` / `REVIEW` | `BELOW_PREVIOUS` | The reading is lower than the previous accepted reading. | none |
| `NEEDS_REVIEW` / `REVIEW` | `JUMP_TOO_LARGE` | The increase since the previous reading is implausibly large. | none |
| `NEEDS_REVIEW` / `REVIEW` | `READS_DISAGREE` | Independent reads of the same photo gave different results. | none |
| `NEEDS_REVIEW` / `REVIEW` | `HISTORY_UNAVAILABLE` | The consumer's previous readings could not be checked (the database did not answer), so the reading was not compared with them. | none |
| `NEEDS_REVIEW` / `REVIEW` | `CORRECTED_BEFORE` | A person marked the reading of this same photo wrong or corrected it, so it is not read or accepted again; the earlier model reading is shown. | none |

`meterReading` holds the reading whenever the model saw digits, whatever the status. When there is no number to show, it holds one of these fixed sentences instead:

| `status` | `meterReading` when there is no number |
|---|---|
| `NOMETER` | No meter display detected in the image (always, since there is no display) |
| `UNCLEAR` | Not the cumulative energy reading; please retake the photo |
| `NEEDS_REVIEW` | Reading needs manual review |

`READS_DISAGREE` appears only when the operators set the service to read each photo twice (off by default). `notes`, when present, holds extra free-text details for people (for example why a reading was accepted without a cross-check); do not branch on it. Errors (`responseCode: "ERROR"`) are a separate kind of response: see [section 5](#5-errors).

**Bill only on `ACCEPT`,** or on a reading a person has confirmed. The reading is returned for every photo with a display so that people can see it, but only `ACCEPT` has passed every check.

Other fields:
- `meterReading` is the reading exactly as displayed, with leading zeros and the decimal point (`"0019712.7"`). It is a **string**: parse it as a decimal, never as a float, if you need the number. A `?` stands for a digit the model could not even guess.
- `unit` is `kWh` or `kVAh` on an `ACCEPT`. On other results it is whatever unit the display showed (e.g. `kW` on a demand screen), or it is left out. Meters show both totals, so check it matches the unit you bill in, or set the consumer's billing unit ([section 7](#7-meter-settings-per-consumer-optional)).
- `uncertainDigits` lists the 1-based positions of the digits the model was unsure of, counting leading zeros but not the decimal point. For example, `[6]` means "check the 6th digit".
- `decimalSource` says where the decimal point comes from: `consumer` or `registry` (the meter type), `visible_point`, `previous_reading`, or `model_guess`.
- `reasons` is a list of `{code, message}` explaining a non-ACCEPT result.
  - Photo problems (RETAKE): `NO_DISPLAY`, `WRONG_SCREEN`, `UNIT_NOT_VISIBLE`, `UNIT_MISMATCH`.
  - Review needed: `UNREADABLE`, `DIGITS_UNCLEAR` (a digit unclear or possibly missing), `LOW_CONFIDENCE` (the photo was hard to read, e.g. glare), `DIGIT_COUNT_MISMATCH`, `DECIMAL_UNCERTAIN`, `NO_CROSS_CHECK`, `BELOW_PREVIOUS`, `JUMP_TOO_LARGE`, `HISTORY_UNAVAILABLE` (the previous readings could not be checked), `READS_DISAGREE`, `BAD_OUTPUT`, `CORRECTED_BEFORE` (a person already marked the reading of this same photo wrong or corrected it, so it is not read or accepted again; see [section 4](#4-timeouts-and-retries)).
- Fields with no value (e.g. `retakeHint` on an ACCEPT) are left out of the response rather than sent as `null`. The one exception is `previousReading`, the previous reading the result was compared with: its `unit` or `capturedAt` can be `null`.
- `correlationId` identifies this reading for `/feedback`.

## 4. Timeouts and retries

- A reading normally takes 20–30 seconds and never more than about **100 seconds**. **Set your HTTP client's timeout to 120 seconds.**
- **Always send your own `id`, and use the same `id` when you retry.** The service treats the id as an idempotency key:
  - If that id already has a result, the service returns the **stored** result: the same reading and `correlationId`, no second model call, and the response header `Idempotent-Replay: true`.
  - If that id is still being processed, the response is HTTP 409 `ERROR_12` with `retryable: true`. Wait for the `Retry-After` header, then send it again.
  - If that id failed earlier (an error response), sending it again for the same consumer processes it again (with a corrected photo too).
- The same id with a **different** consumer or photo is refused (HTTP 409, `ERROR_15`, not retryable): an id identifies one photo of one consumer. Send each new photo with a new id. Only after a failed attempt may the same id be resent with a corrected photo, and only for the same consumer (or with a `consumerId` when the failed attempt had none).
- What counts as the same photo for a retried id: a link that was signed or issued again counts as the same link, and `a b.jpg` equals `a%20b.jpg`. Only these query parameters are ignored, because they change when a link is signed or issued again: any `X-Amz-*` or `X-Goog-*` parameter, and `Signature`, `Expires`, `Key-Pair-Id`, `Policy`, `sig`, `se`, `st`, `sp`, `sv`, `sr`, `spr`, `ss`, `srt`, `skoid`, `sktid`, `skt`, `ske`, `sks`, `skv`, `GoogleAccessId`, `token`, `access_token`, `auth_token`, `id_token`, `jwt`, `hmac`, `expiry`, `exp` and `nonce` (in any letter case). Every other parameter, such as `photoId`, `key` or `code`, tells photos apart. The same photo sent first as `imageURL` and then as `imageBase64` or an upload (`POST /flowvision/v1/elm/extract-reading/upload`, a multipart form with the photo in `image`) under the same id is also a retry. The other way round (base64 first, then a link) is refused with `ERROR_15`, so resend an id in the form you first used.
- The **same photo** (identical file) of the same consumer, sent again with a new id within 7 days of its original reading, gets that result back without a second reading (response header `Duplicate-Of: <earlier id>`, `costEstimateUsd` 0). A new photo of the same meter is always read. Also:
  - while that photo is still being read under another id, the new request gets HTTP 409 `ERROR_12` (retryable; the message names the other id). Retry with the same id after it finishes and you get the earlier result without a second reading, so a double submit is paid once;
  - if the consumer's meter settings ([section 7](#7-meter-settings-per-consumer-optional)) really changed after that reading, the photo is read again (a `PUT` with the same settings changes nothing);
  - if a person marked that reading wrong or corrected it through `/feedback` (`accurate: false`, or an `actual` that differs from the reading; an `actual` equal to the reading is a confirmation), the same photo is never read or accepted again: it comes back `NEEDS_REVIEW` with reason `CORRECTED_BEFORE`, the earlier model reading, a new `correlationId`, `Duplicate-Of` naming the corrected request, and `costEstimateUsd` 0.
- **Retry only errors with `"retryable": true`,** after the `Retry-After` header when one is given (otherwise wait 5–10 s), up to 3 times with growing waits. Errors with `retryable: false` will fail again: fix the request instead.
- A timeout on your side (no response at all) is safe to retry with the **same** `id`. So are responses that come from the load balancer rather than the service (HTTP 502, 503 or 504 without the JSON envelope, e.g. during a deployment): retry them like `retryable: true`.
- After a crash on the service side, the same id can answer 409 `ERROR_12` for up to about 3 minutes. Keep retrying with growing waits (e.g. 10 s, 30 s, 60 s, 120 s) before giving up. After those 3 minutes the photo is read again, which is a second paid model call. The same happens if the database fails for more than about 7 s just as a reading is stored: the reading is still returned to you, but a retry of that id after about 3 minutes reads it again. Both are rare, and the extra cost is one reading.
- A photo that comes back `RETAKE` is a new photo, so send it with a **new** `id`.

## 5. Errors

Errors use the same envelope, with `responseCode: "ERROR"` and an `error` object:

```json
{"id": "JS-2026-09-30-000123", "ts": "...", "responseCode": "ERROR", "statusCode": 502,
 "error": {"errorCode": "ERROR_04", "errorMsg": "The model call failed: OpenAI HTTP 503", "retryable": true}}
```

The HTTP status equals `statusCode`. (The operators can switch the service to FlowVision's style, where every response is HTTP 200 and only `statusCode` tells an error apart. Either way, check `responseCode`.)

| `errorCode` | HTTP | Retryable | Meaning |
|---|---|---|---|
| `ERROR_06` | 400 / 405 | no | Bad request: a missing or conflicting field, an invalid date or one outside 2000–2100, an invalid `id` (NaN or Infinity included), the NUL character in any text, NaN or Infinity, details nested more than 20 levels deep, or invalid JSON (400); or a wrong HTTP method (405). `errorMsg` says which field. A request refused this way keeps your `id` in the response and is stored, unless that id already has a result. An invalid `consumerId` is not stored as the id's consumer, so you can resend the id with a corrected one. |
| `ERR_001` | 400 | no | The image is empty, not valid base64, cannot be decoded, an unusual kind of JPEG (lossless or hierarchical; cameras and phones write baseline or progressive JPEGs), or is too large in pixels (PNG, WebP, or a progressive, multi-scan or CMYK JPEG over 16 megapixels; other JPEG over 250). |
| `ERR_002` | 415 | no | Not a JPEG, PNG or WebP image. |
| `ERROR_003` | 413 | no | The uploaded or base64 image is over 20 MB, or the request body is over 30 MB. A body of up to 60 MB is read and discarded first, so you get this error, not a dropped connection. Such a request is not stored, and the response carries a new id, not yours. |
| `ERROR_05` | 400 / 502 | 502: yes | The image URL could not be downloaded. 400 means your endpoint refused it (e.g. `HTTP 404`), the image there is over 20 MB, the host or address is not allowed, or the link contains a user name or password. 502 means your endpoint was down or too slow. |
| `ERROR_07` | 400 | no | `PUT /meters`: unknown `meterTypeId`. |
| `ERROR_15` | 409 | no | This `id` was already used for a different consumer or photo (after a failed attempt, another consumer). Right after a service upgrade, a retry of an id answered before it can also get this when its `imageURL` carried a token parameter (`token`, `access_token`, ...). Nothing is charged, and the stored result can be read with `GET /flowvision/v1/elm/readings/{id}`. |
| `ERROR_09` | 401 | no | Missing or wrong `X-API-Key`. |
| `ERROR_08` | 404 | no | Not found: no such reading (`/readings`, `/feedback`), no stored settings for this consumer (`GET /meters/{consumerId}`), or an unknown path. |
| `ERROR_12` | 409 | yes | A request with this `id` is still being processed, or the same photo of this consumer is being read under another id. |
| `ERROR_11` | 429 | only near the cap | The service's daily model budget is used up; readings resume the next day (India time), `retryable: false`. Tell the operators. Near the cap, when only other readings still in progress (each reserves $0.05) are in the way, the message says "retry in a minute", with `retryable: true` and `Retry-After: 60`. |
| `ERROR_04` | 502 | per response | The model (OpenAI, or Anthropic when the service runs Claude) failed or did not answer in time. Retry when `retryable` is true. |
| `ERROR_10` | 503 | yes | The service is busy: too many readings at once, or too many large photos being received at once ("Too many large photos are being processed"; refused before the body is read, so it is not stored and the response carries a new id, not yours). Wait for `Retry-After`. |
| `ERROR_13` | 503 | yes | The service's database is unavailable. |
| `ERROR_14` | 500 | yes | Unexpected error. Quote the `id` to the operators. |

## 6. Feedback: confirm or correct a reading

`POST /flowvision/v1/elm/feedback`:

```json
{"correlationId": "5f0c2a8e-1b7d-4c1e-9d3a-2f7e0b6a9c41", "data": {"accurate": false, "actual": "0019721.7"}}
```

Send `accurate`, `actual`, or both; at least one is required.
- `accurate: true` means the returned reading is the correct cumulative reading. On an `ACCEPT` or `REVIEW` result, it becomes the consumer's trusted previous reading, provided the reading is a full number: a reading with a `?`, or a sentence, cannot be confirmed, so send `actual` instead. `accurate: false` marks it wrong.
- `actual` is the correct reading. Send it **as a string** so the leading zeros are kept: digits, with a decimal point if the meter shows one. It becomes the consumer's trusted previous reading. This does not apply to a `CORRECTED_BEFORE` result, which repeats an earlier photo; send the correction with the original reading's `correlationId` instead (the request named in `Duplicate-Of`, see [section 8](#8-looking-up-a-stored-reading-or-photo)). `accurate` may be left out when `actual` is sent.
- A photo whose reading was marked wrong or corrected is not read again if it is sent again ([section 4](#4-timeouts-and-retries)).
- An unknown `correlationId` returns 404 `ERROR_08`.

Feedback matters: every later photo of that consumer is checked against the latest trusted reading.

## 7. Meter settings per consumer (optional)

`PUT /flowvision/v1/elm/meters/{consumerId}`:

```json
{"meterTypeId": "hpl_ppsm01", "billingUnit": "kWh"}
```

A `PUT` replaces all four settings: a field you leave out is cleared. All four are optional:
- `meterTypeId` is one of `GET /flowvision/v1/elm/meter-types`.
- `decimals` is the number of digits after the decimal point (a whole number, 0–3; not `true`/`false`).
- `digits` is the total number of digits on the display, including leading zeros (1–12).
- `billingUnit` is `kWh` or `kVAh`. With it set, a clear reading of the other unit comes back `UNCLEAR` (`UNIT_MISMATCH`). A hard-to-read photo still goes to review whatever its unit, so the reviewer should check `unit` too.

Setting these makes readings of that meter more reliable. `GET` on the same path returns the stored settings.

## 8. Looking up a stored reading or photo

- `GET /flowvision/v1/elm/readings/{id or correlationId}` returns the stored record in `result` (the usual envelope): the reading, status, your details, and the request and response.
- `GET /flowvision/v1/elm/readings/{id or correlationId}/image` returns the photo.

## 9. Health endpoints

These need no API key:
- `GET /health` returns 200 when the service is up.
- `GET /ready` returns 200 when its database is also reachable, and 503 otherwise.

## 10. Checklist before going live

- [ ] The API key is in a secret store on your side, not in code.
- [ ] Your HTTP client timeout is 120 s.
- [ ] Each request has your own unique `id`, and retries reuse it; retries happen only when `retryable` is true.
- [ ] `consumerId` and `capturedAt` are sent (the capture time, not the upload time).
- [ ] Billing uses only `ACCEPT` readings, or readings confirmed through `/feedback`.
- [ ] `RETAKE` shows `retakeHint` and asks for a new photo, sent with a new `id`.
- [ ] `REVIEW` results go to a person, who sends `/feedback`.
- [ ] The `unit` is checked, or `billingUnit` is set per consumer.
- [ ] Your image endpoint is reachable from the service. Give the operators its host name, and any credentials it needs.
