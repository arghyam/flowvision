"""
Request handling for the meter reader, following FlowVision's ImageService: build the response envelope, log each
request/response, store every request with its outcome, and turn errors into ERROR responses.

One reading request, in order:
1. claim its id (the idempotency key): an id that already has a result gets that stored result back, with no second
   model call; one still being processed gets "in progress" (409, retry shortly);
2. wait for a free read slot (503 "busy" if none frees up quickly);
3. load the photo (base64 or download from the calling system);
4. claim the photo: the same photo of the same consumer is answered from its earlier result (or refused with 409
   while it is being read under another id), and a photo a person corrected is sent to review, not read again;
5. reserve the call's estimated cost against the daily budget (429 once it is used up), then read the photo within
   the request deadline and save it;
6. store the complete record, with the real cost, before the response is sent.
"""
import base64
import binascii
import hashlib
import json
import logging
import math
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit

from sqlalchemy.exc import DataError, DBAPIError
from http import HTTPStatus
from typing import NamedTuple
from uuid import uuid4

from conf.config import Config, ServiceSettings
from error.error import CustomHTTPException, ErrorCode
from models.models import (Action, BaseResponse, Error, FeedbackRequest, FeedbackResponse, FeedbackResponseStatus,
                           FeedbackStatus, MeterInfo, MeterProfile, MeterProfileRequest, MeterProfileResponse,
                           MeterTypeInfo, MeterTypesResponse, ReadingExtractionRequest, ReadingExtractionResponse,
                           ReadingExtractionResult, ReadingExtractionResultData, ReadSummary, Reason, ResponseCode,
                           Status)
from service.api.guards import DailyBudget, ReadSlots
from service.api.metadata_service import MetadataStore
from service.vision import ACCEPTED, ImageError, MeterReader, ModelError, previous_from
from service.vision.checks import ENERGY_UNITS, REASONS, normalize_unit, parse_reading
from service.vision.images import fetch_image_url
from validation.validators import ImageValidator

# FlowVision keeps meterReading present on every result; when there is no reading it carries a short explanation.
NO_METER_READING = "No meter display detected in the image"
UNCLEAR_READING = "Not the cumulative energy reading; please retake the photo"
REVIEW_READING = "Reading needs manual review"


def now():
    return datetime.now(timezone.utc)


class Reply(NamedTuple):
    response: BaseResponse
    headers: dict


class _Answered(Exception):
    """Carries a finished response out of the read path (e.g. a duplicate photo answered from an earlier result)."""

    def __init__(self, response):
        super().__init__()
        self.response = response


def redact_url(url: str | None) -> str | None:
    """The URL without user info, query string or fragment: signatures and tokens stay out of logs and the database."""
    if not url:
        return url
    try:
        parts = urlsplit(url)
        return urlunsplit((parts.scheme, parts.netloc.rpartition("@")[2], parts.path, "", ""))
    except ValueError:                        # not a valid link (refused later): still never log its query
        return re.sub(r"//[^/]*@", "//", url.split("?", 1)[0].split("#", 1)[0])


# Query parameters that change when a link is signed or issued again (AWS S3, Google Cloud Storage, Azure SAS,
# CloudFront, and common token parameters): ignored when deciding whether two links point at the same photo. Only
# these exact names: every other parameter may identify the photo (?photoId=123, ?key=photos/1.jpg) and counts.
_REISSUE_PARAMS = {"signature", "expires", "key-pair-id", "policy", "sig", "se", "st", "sp", "sv", "sr", "spr", "ss",
                   "srt", "skoid", "sktid", "skt", "ske", "sks", "skv", "googleaccessid", "token", "access_token",
                   "auth_token", "id_token", "jwt", "hmac", "expiry", "exp", "nonce"}


def _changes_on_reissue(name: str) -> bool:
    name = name.lower()
    return name in _REISSUE_PARAMS or name.startswith(("x-amz-", "x-goog-"))


def _link_identity(url: str | None) -> str | None:
    """The link as far as it identifies a photo: scheme, host, the decoded path, and the query parameters that do not
    change when the link is signed or issued again."""
    if not url:
        return url
    try:
        parts = urlsplit(url.strip())
        host = parts.netloc.rpartition("@")[2].lower()
    except ValueError:
        return url.strip()
    query = urlencode(sorted((k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                             if not _changes_on_reissue(k)))
    path = quote(unquote(parts.path), safe="/:@!$&'()*+,;=")      # "a b.jpg" and "a%20b.jpg" are one photo
    return urlunsplit((parts.scheme.lower(), host, path, query, ""))


def photo_sha1(request: ReadingExtractionRequest, image_bytes: bytes | None) -> str | None:
    """SHA-1 of the photo's bytes when the request carries them (upload or base64), else None (a link)."""
    if image_bytes is not None:
        return hashlib.sha1(image_bytes).hexdigest()
    if request.imageBase64 is not None:
        try:
            return hashlib.sha1(base64.b64decode(request.imageBase64.split(",", 1)[-1])).hexdigest()
        except (binascii.Error, ValueError):
            return None
    return None


def fingerprint(request: ReadingExtractionRequest, image_bytes: bytes | None, sha1: str | None = None) -> str:
    """Identifies what a request asks for (consumer + photo), so a reused id for a different request is caught."""
    sha1 = sha1 or photo_sha1(request, image_bytes)
    if sha1 is not None:                      # the photo itself, so upload and base64 of one photo match
        photo = "bytes:" + sha1
    elif request.imageBase64 is not None:
        photo = "b64:" + hashlib.sha1(request.imageBase64.encode()).hexdigest()
    else:
        photo = "url:" + (_link_identity(request.imageURL) or "")
    return hashlib.sha1(f"{request.consumerId or ''}|{photo}".encode()).hexdigest()


def legacy_fingerprints(request: ReadingExtractionRequest, image_bytes: bytes | None) -> set:
    """The fingerprint the service wrote before October 2026, so an id answered by that version still replays after
    an upgrade: base64 by its text, and links without a query string. A link with a query is left out: that version
    ignored the query, so its fingerprint cannot tell ?photoId=1 from ?photoId=2."""
    if image_bytes is not None:
        photo = "bytes:" + hashlib.sha1(image_bytes).hexdigest()
    elif request.imageBase64:
        photo = "b64:" + hashlib.sha1(request.imageBase64.split(",", 1)[-1].encode()).hexdigest()
    else:
        try:
            parts = urlsplit(request.imageURL or "")
        except ValueError:
            return set()
        if parts.query:
            return set()
        photo = "url:" + urlunsplit((parts.scheme, parts.netloc.rpartition("@")[2], parts.path, "", ""))
    return {hashlib.sha1(f"{request.consumerId or ''}|{photo}".encode()).hexdigest()}


def _storable_copy(value, depth=0):
    """A copy a database can hold: NUL and unpaired surrogates in text replaced, NaN and Infinity as null, and
    anything nested more than 20 levels deep replaced by a short note."""
    if depth > 20:
        return "(nested too deeply: not stored)"
    if isinstance(value, str):
        return value.replace("\x00", "\ufffd").encode("utf-8", "replace").decode("utf-8")
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {_storable_copy(str(k), depth + 1): _storable_copy(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_storable_copy(v, depth + 1) for v in value]
    return value


class ReadingService:
    def __init__(self, config: Config, reader: MeterReader, metadata_store: MetadataStore, image_store=None,
                 settings: ServiceSettings | None = None):
        self.config = config
        self.reader = reader
        self.metadata_store = metadata_store
        self.image_store = image_store          # None: photos are not kept
        self.settings = settings or ServiceSettings()
        self.validator = ImageValidator(config)
        self.base_logger = logging.getLogger(config.find("logs.api_logger.name"))
        self.feedback_logger = logging.getLogger(config.find("logs.feedback_request_logger.name"))
        self.extraction_logger = logging.getLogger(config.find("logs.extraction_request_logger.name"))
        self.budget = DailyBudget(self.settings.daily_budget_usd, metadata_store.reserve)
        self.slots = ReadSlots(self.settings.max_concurrent_reads, self.settings.busy_wait)
        # reserved against the daily budget before each paid read, per model call: more than any call measured so
        # far costs (max $0.043 in 384 GPT-6 Sol calls; Claude calls so far under $0.03), so readings in flight cannot
        # carry spend past the cap
        self.reserve_usd = round(self.settings.reserve_per_call_usd * reader.settings.reads, 5)

    # ---------- reading ----------
    def _local(self, dt):
        """A time sent without a time zone is taken as local (India) time, not UTC."""
        return dt.replace(tzinfo=self.settings.naive_time_zone) if dt is not None and dt.tzinfo is None else dt

    def load_image(self, request: ReadingExtractionRequest, deadline: float) -> bytes:
        if (request.imageURL is None) == (request.imageBase64 is None):
            raise CustomHTTPException(status_code=HTTPStatus.BAD_REQUEST.value,
                                      error_code=ErrorCode.INVALID_REQUEST_ERROR.value,
                                      detail="Send exactly one of imageURL or imageBase64.")
        if request.imageBase64 is not None:
            try:
                data = base64.b64decode(request.imageBase64.split(",", 1)[-1], validate=True)
            except (binascii.Error, ValueError):
                raise CustomHTTPException(status_code=HTTPStatus.BAD_REQUEST.value,
                                          error_code=ErrorCode.INVALID_FILE_ERROR.value,
                                          detail="imageBase64 is not valid base64.")
        else:
            s = self.settings
            try:
                data = fetch_image_url(request.imageURL, self.reader.settings.max_image_bytes, timeout=s.download_timeout,
                                       allowed_hosts=s.allowed_hosts, allow_private=s.allow_private,
                                       headers=s.download_headers, deadline=deadline)
            except ImageError as e:
                raise CustomHTTPException(
                    status_code=(HTTPStatus.BAD_GATEWAY if e.retryable else HTTPStatus.BAD_REQUEST).value,
                    error_code=ErrorCode.IMAGE_DOWNLOAD_ERROR.value, detail=str(e), retryable=e.retryable)
        return self.validator.validate_bytes(data)

    def extract_reading(self, request: ReadingExtractionRequest, image_bytes: bytes | None = None,
                        arrived: float | None = None) -> Reply:
        """arrived: time.monotonic() when the request arrived (the deadline counts from then, queueing included)."""
        received = now()
        deadline = (arrived or time.monotonic()) + self.settings.request_deadline
        stale_after = self.settings.request_deadline + 60
        request.id = request.id or str(uuid4())
        request.capturedAt = self._local(request.capturedAt)
        if request.previousReading is not None:
            request.previousReading.capturedAt = self._local(request.previousReading.capturedAt)
        request_json = request.model_dump(mode="json", exclude={"imageBase64"})
        if request.imageURL:
            request_json["imageURL"] = redact_url(request.imageURL)   # no signatures or tokens in logs and database
        self.extraction_logger.info(json.dumps(request_json, ensure_ascii=False, default=str))
        sha1 = photo_sha1(request, image_bytes)
        # Every request is recorded, failures included: the details sent, the photo, the full request and response.
        record = {"request_id": request.id, "consumer_id": request.consumerId, "image_url": request_json.get("imageURL"),
                  "metadata": request.metadata, "request_timestamp": received,
                  "captured_at": request.capturedAt or received, "request_json": request_json,
                  "request_fingerprint": fingerprint(request, image_bytes, sha1)}
        same_request = {record["request_fingerprint"], *legacy_fingerprints(request, image_bytes)}

        try:
            state, row = self.metadata_store.claim(record, stale_after=stale_after)
        except (OverflowError, ValueError, DataError) as e:      # the details cannot be stored: never retry them
            self.base_logger.warning("request %s: details cannot be stored: %s", request.id, e)
            return self._reply(self.error(request.id, HTTPStatus.BAD_REQUEST, ErrorCode.INVALID_REQUEST_ERROR,
                                          "The request's details cannot be stored (a date out of range, or a "
                                          "character or number a database cannot hold)."), {})
        except Exception:  # noqa: BLE001 - no database: refuse before any paid work
            self.base_logger.exception("request %s: database unavailable, request refused", request.id)
            return self._reply(self.error(request.id, HTTPStatus.SERVICE_UNAVAILABLE, ErrorCode.STORAGE_ERROR,
                                          "The database is unavailable; retry shortly.", retryable=True),
                               {"Retry-After": "10"})
        if state == "conflict" or (state in ("replay", "in_progress")
                                   and not self._same_request(row, request, same_request, sha1)):
            return self._reply(self.error(request.id, HTTPStatus.CONFLICT, ErrorCode.ID_CONFLICT_ERROR,
                                          "This id was already used for a different request (another consumer or "
                                          "photo); send each new photo with a new id."), {})
        if state == "replay" and row.get("response_json"):
            self.base_logger.info("request %s: already answered, returning the stored result", request.id)
            return Reply(ReadingExtractionResponse.model_validate(row["response_json"]), {"Idempotent-Replay": "true"})
        if state == "in_progress":
            return self._reply(self.error(request.id, HTTPStatus.CONFLICT, ErrorCode.IN_PROGRESS_ERROR,
                                          "A request with this id is still being processed; retry shortly.",
                                          retryable=True), {"Retry-After": "10"})

        # a retried id keeps what its earlier attempts cost (an abandoned attempt: what it reserved)
        earlier_cost = float(row.get("cost_usd") or 0.0) if state == "retry" and row else 0.0
        reserved = 0.0
        headers = {}
        try:
            prev = request.previousReading
            try:
                previous = previous_from(prev.value, prev.unit, prev.capturedAt) if prev else None
            except ValueError:
                raise CustomHTTPException(status_code=HTTPStatus.BAD_REQUEST.value,
                                          error_code=ErrorCode.INVALID_REQUEST_ERROR.value,
                                          detail="previousReading.value must be digits with an optional decimal point, "
                                                 "e.g. \"0019712.7\".")
            if not self.slots.acquire():
                headers["Retry-After"] = "10"
                raise CustomHTTPException(status_code=HTTPStatus.SERVICE_UNAVAILABLE.value,
                                          error_code=ErrorCode.BUSY_ERROR.value, retryable=True,
                                          detail="Too many readings are in progress; retry shortly.")
            try:
                data = (self.validator.validate_bytes(image_bytes) if image_bytes is not None
                        else self.load_image(request, deadline))
                record["image_sha1"] = hashlib.sha1(data).hexdigest()
                hours = self.settings.duplicate_photo_hours
                kind, earlier = self.metadata_store.claim_photo(
                    request.id, received, request.consumerId, record["image_sha1"],
                    received - timedelta(hours=hours) if hours else None, stale_after)
                if kind in ("duplicate", "corrected"):
                    record["image_path"] = self.save_image(request.id, data, received)
                    response = self._reuse(earlier, request.id, record, corrected=kind == "corrected")
                    headers = {"Duplicate-Of": earlier["request_id"]}
                    raise _Answered(response)
                if kind == "in_flight":
                    headers["Retry-After"] = "10"
                    raise CustomHTTPException(
                        status_code=HTTPStatus.CONFLICT.value, error_code=ErrorCode.IN_PROGRESS_ERROR.value,
                        retryable=True, detail=f"The same photo of this consumer is being read under request "
                                               f"{earlier['request_id']}; retry shortly with the same id: its result "
                                               f"is then returned without a second reading.")
                allowed, used_up = self.budget.reserve(request.id, received, self.reserve_usd)
                if not allowed and not used_up:          # only the reservations of readings in flight are in the way
                    headers["Retry-After"] = "60"
                    raise CustomHTTPException(status_code=HTTPStatus.TOO_MANY_REQUESTS.value,
                                              error_code=ErrorCode.BUDGET_EXCEEDED_ERROR.value, retryable=True,
                                              detail="The daily model budget is nearly used up by readings in "
                                                     "progress; retry in a minute.")
                if not allowed:
                    self.base_logger.critical("daily model budget of $%.2f reached: readings refused until %s",
                                              self.settings.daily_budget_usd, self.budget.resets_at().isoformat())
                    raise CustomHTTPException(status_code=HTTPStatus.TOO_MANY_REQUESTS.value,
                                              error_code=ErrorCode.BUDGET_EXCEEDED_ERROR.value,
                                              detail=f"The daily model budget is used up; readings resume at "
                                                     f"{self.budget.resets_at().isoformat()}.")
                reserved = self.reserve_usd
                try:
                    result = self.reader.read(data, consumer_id=request.consumerId, captured_at=record["captured_at"],
                                              previous=previous, deadline=deadline)
                finally:           # the photo is kept whatever happened, after the paid call (not on its clock)
                    record["image_path"] = self.save_image(request.id, data, received)
            finally:
                self.slots.release()
            status = self.api_status(result)
            correlation_id = uuid4()
            response = ReadingExtractionResponse(
                id=request.id, ts=now(), responseCode=ResponseCode.OK, statusCode=HTTPStatus.OK.value,
                result=ReadingExtractionResult(status=status, correlationId=correlation_id,
                                               data=self.result_data(result, status)))
            record.update({
                "correlation_id": str(correlation_id), "meter_reading_status": status.value,
                "meter_reading": None if status == Status.NOMETER else result.reading, "unit": result.unit,
                "meter_type": result.meter.get("typeId"), "reasons": result.reason_codes,
                "result": response.result.data.model_dump(mode="json"), "model": result.model,
                "processing_time": result.processing_seconds, "cost_usd": result.cost_usd})
        except _Answered as a:
            response = a.response
        except CustomHTTPException as e:
            response = self.error(request.id, e.status_code, e.error_code, e.detail or e.phrase, retryable=e.retryable)
        except ImageError as e:          # the photo could not be prepared: no model call was made
            response = self.error(request.id, HTTPStatus.BAD_REQUEST, ErrorCode.INVALID_FILE_ERROR, str(e),
                                  retryable=e.retryable)
        except ModelError as e:
            log = self.base_logger.critical if e.alert else self.base_logger.error
            log("request %s: model call failed: %s", request.id, e)
            record["cost_usd"] = e.cost_usd              # a failed call can still be billed: counted all the same
            response = self.error(request.id, HTTPStatus.BAD_GATEWAY, ErrorCode.LLM_ERROR,
                                  f"The model call failed: {e}", retryable=e.retryable)
        except Exception as e:  # noqa: BLE001
            spent = getattr(e, "cost_usd", None)
            # the model call may have been made: what it cost, else what was reserved for it
            record["cost_usd"] = float(spent) if spent is not None else reserved
            if isinstance(e, DBAPIError):
                self.base_logger.error("request %s: database error", request.id, exc_info=True)
                response = self.error(request.id, HTTPStatus.SERVICE_UNAVAILABLE, ErrorCode.STORAGE_ERROR,
                                      "The database is unavailable; retry shortly.", retryable=True)
                headers["Retry-After"] = "10"
            else:
                self.base_logger.exception("request %s: unexpected error", request.id)
                response = self.error(request.id, HTTPStatus.INTERNAL_SERVER_ERROR, ErrorCode.INTERNAL_ERROR,
                                      f"Internal error; quote request id {request.id}.", retryable=True)
        if response.responseCode == ResponseCode.ERROR:
            record["meter_reading_status"] = "ERROR"
        if earlier_cost:                  # a retried id keeps what its failed attempts cost
            record["cost_usd"] = round(earlier_cost + float(record.get("cost_usd") or 0.0), 5)
        record.update(response_timestamp=response.ts, response_json=response.model_dump(mode="json", exclude_none=True))
        self._store(record)
        self.extraction_logger.info(response.model_dump_json())
        return self._reply(response, headers)

    @staticmethod
    def _same_request(row, request, fingerprints, sha1) -> bool:
        """Whether a stored request with this id asked for the same thing (consumer and photo) as this one."""
        stored = row.get("request_fingerprint")
        if stored is None or stored in fingerprints:
            return True
        if row.get("consumer_id") != request.consumerId:
            return False
        return sha1 is not None and row.get("image_sha1") == sha1      # same photo, sent as a link before

    def _reuse(self, earlier, request_id, record, corrected=False) -> ReadingExtractionResponse:
        """Answers with an earlier request's result for the same photo: no model call, no cost. corrected: a person
        marked that reading wrong or corrected it, so this answer goes to review (CORRECTED_BEFORE)."""
        self.base_logger.info("request %s: same photo as request %s, returning its result (no model call)%s",
                              request_id, earlier["request_id"], " for review: it was corrected" if corrected else "")
        response = ReadingExtractionResponse.model_validate(earlier["response_json"])
        response.id, response.ts = request_id, now()
        data = response.result.data if response.result else None
        note = f"same photo as request {earlier['request_id']}: its result is reused"
        if corrected and response.result:
            fix = earlier.get("actual_reading")
            note = (f"same photo as request {earlier['request_id']}: a person "
                    + (f"corrected its reading to {fix}" if fix else "marked its reading wrong") + "; not read again")
            response.result.status = Status.NEEDS_REVIEW
            response.result.correlationId = uuid4()          # a new item to review, with its own feedback
            if data:
                data.action, data.retakeHint = Action.REVIEW, None
                data.reasons = [Reason(code="CORRECTED_BEFORE", message=REASONS["CORRECTED_BEFORE"][1])]
        if data:
            data.notes = [*data.notes, note]
            data.costEstimateUsd = 0.0
        record.update({k: earlier[k] for k in ("correlation_id", "meter_reading_status", "meter_reading", "unit",
                                               "meter_type", "reasons", "model")})
        if corrected and response.result:
            record.update(correlation_id=str(response.result.correlationId),
                          meter_reading_status=Status.NEEDS_REVIEW.value, reasons=["CORRECTED_BEFORE"])
        record.update(result=data.model_dump(mode="json") if data else None,
                      processing_time=0.0, cost_usd=0.0, duplicate_of=earlier["request_id"])
        return response

    def record_rejected(self, request_id: str, body: dict | None, response: BaseResponse):
        """Stores a request that was refused before it could be processed (invalid fields, too large), unless that id
        already has a record. Values a database cannot hold (NUL, NaN, broken text) are stored replaced. Never raises:
        storing must not turn a 400 into a 500."""
        body = {k: v for k, v in (body or {}).items() if k != "imageBase64"} if isinstance(body, dict) else {}
        if body.get("imageURL"):
            body["imageURL"] = redact_url(str(body["imageURL"]))
        consumer = body.get("consumerId")
        if isinstance(consumer, int) and not isinstance(consumer, bool):
            consumer = str(consumer)
        # only a valid consumer id is kept: an invalid one must not lock this id to it (ERROR_15 on the fixed resend)
        valid = isinstance(consumer, str) and 0 < len(consumer) <= 128 and _storable_copy(consumer) == consumer
        consumer = consumer if valid else None
        received = now()
        try:
            body = _storable_copy(body)
            self.metadata_store.store_rejected({
                "request_id": request_id, "consumer_id": consumer,
                "image_url": body.get("imageURL"), "request_timestamp": received, "captured_at": received,
                "meter_reading_status": "ERROR", "request_json": body,
                "response_json": response.model_dump(mode="json", exclude_none=True), "response_timestamp": response.ts})
        except Exception:  # noqa: BLE001
            self.base_logger.warning("request %s: refused request could not be stored", request_id, exc_info=True)

    def _store(self, record, attempts=5):
        """Writes the request's record before the response is sent, retrying a briefly unavailable database (about
        7 s in all). If it still fails, the reading is returned all the same: its row keeps the cost reserved before
        the call, so the daily budget still counts it."""
        for attempt in range(attempts):
            try:
                if not self.metadata_store.store_extraction(record):
                    self.base_logger.warning("request %s: taken over by a newer request with the same id; "
                                             "this result was not stored", record["request_id"])
                return
            except Exception:  # noqa: BLE001
                if attempt == attempts - 1:      # the reading is still returned; the operator must see this
                    self.base_logger.critical("request %s: result could not be stored", record["request_id"],
                                              exc_info=True)
                    return
                time.sleep(0.5 * 2 ** attempt)

    @staticmethod
    def _reply(response, headers):
        return Reply(response, headers)

    def save_image(self, request_id, image_bytes: bytes, when: datetime) -> str | None:
        """Keeps the photo; a failure is logged and never fails the reading."""
        if self.image_store is None:
            return None
        try:
            return self.image_store.save(request_id, image_bytes, when)
        except Exception:  # noqa: BLE001
            # CRITICAL: the reading is returned, but the photo kept for checks and research is lost (alarmed)
            self.base_logger.critical("request %s: photo could not be saved", request_id, exc_info=True)
            return None

    def load_photo(self, name: str) -> bytes | None:
        return self.image_store.load(name) if (self.image_store and name) else None

    @staticmethod
    def api_status(result) -> Status:
        if result.status == ACCEPTED:
            return Status.SUCCESS
        if result.status == "NEEDS_REVIEW":
            return Status.NEEDS_REVIEW
        return Status.NOMETER if "NO_DISPLAY" in result.reason_codes else Status.UNCLEAR

    @staticmethod
    def result_data(result, status: Status) -> ReadingExtractionResultData:
        # The reading is always returned when a display was seen; status/action say how far to trust it.
        # With no display (NOMETER) meterReading carries a short explanation instead (as in FlowVision).
        explanation = {Status.NOMETER: NO_METER_READING, Status.UNCLEAR: UNCLEAR_READING,
                       Status.NEEDS_REVIEW: REVIEW_READING}.get(status)
        reading = None if status == Status.NOMETER else result.reading
        brand = " ".join(x for x in (result.meter.get("make"), result.meter.get("modelCode")) if x) or None
        action = {Status.SUCCESS: Action.ACCEPT, Status.NEEDS_REVIEW: Action.REVIEW}.get(status, Action.RETAKE)
        return ReadingExtractionResultData(
            meterReading=reading or explanation, action=action, unit=result.unit, meterBrand=brand,
            decimalSource=result.decimal_source if reading else None,
            uncertainDigits=result.uncertain_digits if reading else [],
            reasons=[Reason(**r) for r in result.reasons], retakeHint=result.retake_hint,
            meter=MeterInfo(**result.meter), previousReading=result.previous,
            reads=[ReadSummary(**r) for r in result.reads], notes=result.notes, model=result.model,
            promptVersion=result.prompt_version, processingTime=result.processing_seconds,
            costEstimateUsd=result.cost_usd)

    # ---------- feedback ----------
    def log_feedback(self, request: FeedbackRequest) -> Reply:
        request.id = request.id or str(uuid4())
        request.ts = now()
        self.feedback_logger.info(request.model_dump_json())
        if request.data.actual is not None:
            try:
                parse_reading(request.data.actual)
            except ValueError:
                return self._reply(self.error(request.id, HTTPStatus.BAD_REQUEST, ErrorCode.INVALID_REQUEST_ERROR,
                                              "data.actual must be digits with an optional decimal point, "
                                              "e.g. \"0019712.7\"."), {})
        try:
            updated = self.metadata_store.store_feedback(str(request.correlationId), request.data.accurate,
                                                         request.data.actual, request.ts)
        except Exception:  # noqa: BLE001
            self.base_logger.exception("feedback %s: could not be stored", request.id)
            return self._reply(self.error(request.id, HTTPStatus.SERVICE_UNAVAILABLE, ErrorCode.STORAGE_ERROR,
                                          "The database is unavailable; retry shortly.", retryable=True),
                               {"Retry-After": "10"})
        if not updated:
            return self._reply(self.error(request.id, HTTPStatus.NOT_FOUND, ErrorCode.NOT_FOUND_ERROR,
                                          "No reading with this correlationId."), {})
        response = FeedbackResponse(id=request.id, ts=now(), responseCode=ResponseCode.OK,
                                    statusCode=HTTPStatus.OK.value,
                                    result=FeedbackStatus(status=FeedbackResponseStatus.SUBMITTED))
        self.feedback_logger.info(response.model_dump_json())
        return self._reply(response, {})

    # ---------- meter settings ----------
    def get_profile(self, consumer_id: str) -> BaseResponse:
        if "\x00" in consumer_id:
            return self.error(str(uuid4()), HTTPStatus.BAD_REQUEST, ErrorCode.INVALID_REQUEST_ERROR,
                              "The consumer id contains a NUL character.", log=False)
        p = self.metadata_store.get_profile(consumer_id)
        if not p:
            return self.error(str(uuid4()), HTTPStatus.NOT_FOUND, ErrorCode.NOT_FOUND_ERROR,
                              "No meter settings stored for this consumer.", log=False)
        return self._profile_response(p)

    def set_profile(self, consumer_id: str, req: MeterProfileRequest) -> BaseResponse:
        if "\x00" in consumer_id or "\x00" in (req.meterTypeId or "") or "\x00" in (req.billingUnit or ""):
            return self.error(str(uuid4()), HTTPStatus.BAD_REQUEST, ErrorCode.INVALID_REQUEST_ERROR,
                              "The meter settings contain a NUL character.", log=False)
        if req.meterTypeId and not self.reader.registry.get(req.meterTypeId):
            return self.error(str(uuid4()), HTTPStatus.BAD_REQUEST, ErrorCode.UNKNOWN_METER_TYPE_ERROR,
                              f"Unknown meter type {req.meterTypeId!r}; see GET /meter-types.", log=False)
        unit = normalize_unit(req.billingUnit)
        if unit is not None and unit not in ENERGY_UNITS:
            return self.error(str(uuid4()), HTTPStatus.BAD_REQUEST, ErrorCode.INVALID_REQUEST_ERROR,
                              f"billingUnit must be one of {', '.join(ENERGY_UNITS)}.", log=False)
        p = self.metadata_store.set_profile(consumer_id, req.meterTypeId, req.decimals, req.digits, unit)
        self.base_logger.info("meter settings for %s set to %s", consumer_id, req.model_dump_json())
        return self._profile_response(p)

    def _profile_response(self, p):
        return MeterProfileResponse(id=str(uuid4()), ts=now(), responseCode=ResponseCode.OK,
                                    statusCode=HTTPStatus.OK.value,
                                    result=MeterProfile(consumerId=p.consumer_id, meterTypeId=p.meter_type_id,
                                                        decimals=p.decimals, digits=p.digits,
                                                        billingUnit=p.billing_unit,
                                                        updatedAt=p.updated_at.replace(tzinfo=timezone.utc)
                                                        if p.updated_at and p.updated_at.tzinfo is None
                                                        else p.updated_at))

    def meter_types(self):
        return MeterTypesResponse(id=str(uuid4()), ts=now(), responseCode=ResponseCode.OK,
                                  statusCode=HTTPStatus.OK.value,
                                  result=[MeterTypeInfo(**t.to_dict()) for t in self.reader.registry.all()])

    # ---------- errors (FlowVision's envelope) ----------
    def error(self, id, status, code, message, retryable=False, log=True) -> BaseResponse:
        status = int(status)
        response = BaseResponse(id=str(id), ts=now(), responseCode=ResponseCode.ERROR, statusCode=status,
                                error=Error(errorCode=str(code), errorMsg=message, retryable=retryable))
        if log:
            self.base_logger.warning("request %s: %s %s %s", id, status, code, message)
        return response
