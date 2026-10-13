"""
Builds the FastAPI app. routes.py calls create_app() for gunicorn/uvicorn; tests call it with a fake reader.

Every response uses FlowVision's envelope ({id, ts, responseCode, statusCode, result | error}), errors included:
authentication, validation, unknown paths and unexpected failures. The HTTP status equals `statusCode`, so load
balancers and alarms see failures; METER_ERRORS_AS_HTTP_200=true restores FlowVision's HTTP-200-for-everything.
"""
import asyncio
import dataclasses
import hmac
import json
import logging
import math
import re
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from uuid import uuid4

import anyio
from fastapi import APIRouter, Depends, FastAPI, File, Form, Header, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from starlette.exceptions import HTTPException as StarletteHTTPException

from conf.config import (Config, check_production, database_url, image_location, load_service_settings,
                         load_settings, service_path)
from conf.logging import CustomLoggers
from error.error import CustomHTTPException, ErrorCode
from models.models import (REQUEST_ID_PATTERN, BaseResponse, FeedbackRequest, FeedbackResponse, MeterProfileRequest,
                           MeterProfileResponse, MeterTypesResponse, PreviousReading, ReadingExtractionRequest,
                           ReadingExtractionResponse)
from service.api.database import DatabaseService
from service.api.image_store import make_image_store, media_type
from service.api.metadata_service import MetadataStore
from service.api.reading_service import ReadingService
from service.vision import MeterReader

_HTTP_CODES = {401: ErrorCode.UNAUTHORIZED_ERROR, 404: ErrorCode.NOT_FOUND_ERROR, 413: ErrorCode.FILE_SIZE_ERROR}
BODY_PART_TIMEOUT = 60.0               # seconds a request body may stall between two parts
_REQUEST_ID = re.compile(REQUEST_ID_PATTERN)


def caller_id(value) -> str | None:
    """The caller's request id, if it is a valid one (it is echoed back and names the stored record). A number is
    taken as its digits, as the request model does."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        value = str(value)
    return value if isinstance(value, str) and _REQUEST_ID.fullmatch(value) else None


def _form_body(body) -> dict | None:
    """The text fields of a JSON object or a multipart form (files left out)."""
    if isinstance(body, dict):
        return body
    if hasattr(body, "multi_items"):
        return {k: v for k, v in body.multi_items() if isinstance(v, str)}
    return None


def _utc(value):
    """Stored times are UTC; SQLite returns them without a zone, so mark them before they are sent."""
    if isinstance(value, datetime) and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _closing(send):
    """`send` that marks a response "Connection: close" (the server then closes the connection after it)."""
    async def closing_send(message):
        if message["type"] == "http.response.start":
            headers = [h for h in message.get("headers", []) if h[0].lower() != b"connection"]
            message = {**message, "headers": headers + [(b"connection", b"close")]}
        await send(message)
    return closing_send


class RequestGate:
    """Runs before a request body is read: refuses a missing or wrong API key (401) on the API paths, and bodies over
    `limit` bytes (413): at once when Content-Length says so; for a streamed body, the body is cut off at the limit and
    the app's answer is replaced by the 413. It also bounds the photo bodies held in this worker at once
    (`inflight_limit` bytes, counted by Content-Length, a body without one as `limit`): one more is refused with 503
    "busy, retry" before it is read, so a burst of large photos cannot run the worker out of memory."""

    def __init__(self, app, limit: int, reply, deny, protected_prefix: tuple[str, ...], key_ok, inflight_limit: int = 0,
                 busy=None, reading_paths=(), stalled=None):
        self.app, self.limit, self.reply, self.deny, self.stalled = app, limit, reply, deny, stalled
        self.protected_prefix, self.key_ok = protected_prefix, key_ok
        self.inflight_limit, self.busy, self.reading_paths = inflight_limit, busy, tuple(reading_paths)
        self.inflight = 0                          # bytes of reading bodies admitted in this worker (one event loop)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers") or [])
        if scope.get("path", "").startswith(self.protected_prefix) and \
                not self.key_ok(headers.get(b"x-api-key", b"").decode("latin-1")):
            return await self.deny(scope, receive, send)
        length = headers.get(b"content-length")
        # a client waiting for "100 Continue" has not sent its body: refuse it at once rather than invite it
        waits = headers.get(b"expect", b"").lower() == b"100-continue"
        # a refusal that leaves the body unsent closes the connection after it (an admitted request keeps it)
        refuse_send = _closing(send) if waits else send
        if length is not None and length.isdigit() and int(length) > self.limit:
            if not waits:
                await self._discard_body(receive, 2 * self.limit)
            return await self.reply(scope, receive, refuse_send)
        size = 0
        if self.inflight_limit and scope.get("method") == "POST" and scope.get("path") in self.reading_paths:
            size = int(length) if length is not None and length.isdigit() else self.limit
            if self.inflight and self.inflight + size > self.inflight_limit:
                if not waits:
                    await self._discard_body(receive, self.limit)
                return await self.busy(scope, receive, refuse_send)
            self.inflight += size
        try:
            await self._guarded(scope, receive, send)
        finally:
            self.inflight -= size

    @staticmethod
    async def _discard_body(receive, at_most):
        """Reads and drops the request body (up to `at_most` bytes, each part within 10 s), so the refusal that
        follows reaches the client: closing with an unread body makes the client see a reset instead."""
        seen = 0
        try:
            while seen <= at_most:
                message = await asyncio.wait_for(receive(), timeout=10)
                if message["type"] != "http.request":
                    return
                seen += len(message.get("body", b""))
                if not message.get("more_body"):
                    return
        except asyncio.TimeoutError:
            return

    async def _guarded(self, scope, receive, send):
        seen, exceeded, stalled, replied = 0, False, False, False

        async def limited_receive():
            nonlocal seen, exceeded, stalled
            if exceeded or stalled:
                return {"type": "http.request", "body": b"", "more_body": False}
            try:                       # a body that stops arriving frees its place in the body limit after 60 s
                message = await asyncio.wait_for(receive(), timeout=BODY_PART_TIMEOUT)
            except asyncio.TimeoutError:
                stalled = True         # the app's answer is replaced by a retryable 503 ("send it again")
                return {"type": "http.request", "body": b"", "more_body": False}
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > self.limit:
                    exceeded = True
                    return {"type": "http.request", "body": b"", "more_body": False}
            return message

        async def guarded_send(message):
            nonlocal replied
            if not (exceeded or stalled):
                return await send(message)
            if not replied:
                replied = True
                await (self.stalled if stalled and self.stalled else self.reply)(scope, receive, send)

        await self.app(scope, limited_receive, guarded_send)


def create_app(config: Config | None = None, reader: MeterReader | None = None, db_url: str | None = None,
               service_api_key: str | None = None) -> FastAPI:
    config = config or Config()
    CustomLoggers(config=config)
    log = logging.getLogger(config.find("logs.api_logger.name"))
    settings = load_service_settings(config)
    if service_api_key is not None:                         # tests: an explicit key ("" = no authentication)
        settings = dataclasses.replace(settings, api_keys=(service_api_key,) if service_api_key else ())
    url = db_url or database_url(config)
    photos = image_location(config)
    if settings.environment == "production":
        problems = check_production(settings, url, photos)
        if problems:
            raise RuntimeError("METER_ENV=production, but: " + "; ".join(problems))
    elif not settings.api_keys:
        log.warning("METER_SERVICE_API_KEY is not set: every endpoint is open (development only)")

    store = MetadataStore(config, DatabaseService(url))
    if reader is None:
        reader = MeterReader(load_settings(config), store=store)
    else:
        reader.store = store
    image_store = make_image_store(photos)
    service = ReadingService(config, reader, store, image_store, settings)
    log.info("Starting (%s): database %s | photos %s | logs %s | deadline %.0f s | %d reads at once per worker%s",
             settings.environment, make_url(url).render_as_string(hide_password=True),
             image_store.describe() if image_store else "not kept", service_path("logs"),
             settings.request_deadline, settings.max_concurrent_reads,
             f" | daily budget ${settings.daily_budget_usd:.2f}" if settings.daily_budget_usd else "")
    # every API route is served under each base path (a config file that names a single `basepath` still works)
    basepaths = [p.rstrip("/") for p in config.find("basepaths") or [config.find("basepath", "/flowvision/v1/elm")]]
    reading_paths = {f"{p}/extract-reading{suffix}" for p in basepaths for suffix in ("", "/upload")}

    def envelope(response: BaseResponse, headers: dict | None = None) -> JSONResponse:
        status = 200 if settings.errors_as_http_200 else response.statusCode
        return JSONResponse(response.model_dump(mode="json", exclude_none=True), status_code=status, headers=headers)

    def error_reply(status: int, code, message: str, retryable=False, headers=None, id=None) -> JSONResponse:
        return envelope(service.error(id or str(uuid4()), status, code, message, retryable=retryable, log=False), headers)

    def key_ok(key: str | None) -> bool:
        return not settings.api_keys or any(hmac.compare_digest((key or "").encode(), k.encode())
                                            for k in settings.api_keys)

    async def require_key(x_api_key: str | None = Header(None)):      # also enforced by RequestGate, before the body
        if not key_ok(x_api_key):
            raise CustomHTTPException(status_code=401, error_code=ErrorCode.UNAUTHORIZED_ERROR.value,
                                      detail="Missing or wrong X-API-Key header.")

    # Readings are admitted in the event loop, before they take a worker thread: up to twice the read slots
    # (the rest of them wait for a slot, up to busy_wait); beyond that the answer is an immediate 503.
    admission = {"in": 0, "limit": settings.max_concurrent_reads * 2}

    @asynccontextmanager
    async def lifespan(_app):
        limiter = anyio.to_thread.current_default_thread_limiter()
        limiter.total_tokens = max(limiter.total_tokens, admission["limit"] + 16)   # threads for all admitted work
        yield

    async def admitted(fn, *args):
        if admission["in"] >= admission["limit"]:
            request = args[0]
            response = service.error(request.id or str(uuid4()), 503, ErrorCode.BUSY_ERROR,
                                     "Too many readings are in progress; retry shortly.", retryable=True)
            if request.id:                         # recorded like every refused request (a small write, no read)
                body = request.model_dump(mode="json", exclude={"imageBase64"})
                await run_in_threadpool(service.record_rejected, request.id, body, response)
            return envelope(response, {"Retry-After": "10"})
        admission["in"] += 1
        try:
            return envelope(*(await run_in_threadpool(fn, *args)))
        finally:
            admission["in"] -= 1

    docs = settings.docs_enabled
    app = FastAPI(title="Electricity Meter Reader", version="1.0", lifespan=lifespan,
                  description="Reads the cumulative kWh/kVAh reading from a meter photo with GPT-6 Sol and checks. "
                              "Send the X-API-Key header on every call.",
                  docs_url="/docs" if docs else None, redoc_url="/redoc" if docs else None,
                  openapi_url="/openapi.json" if docs else None)
    app.state.service = service
    app.state.admission = admission
    router = APIRouter(dependencies=[Depends(require_key)])

    async def too_large(scope, receive, send):
        await error_reply(413, ErrorCode.FILE_SIZE_ERROR,
                          f"Request body is larger than {settings.max_body_bytes} bytes.")(scope, receive, send)

    async def unauthorized(scope, receive, send):
        await error_reply(401, ErrorCode.UNAUTHORIZED_ERROR, "Missing or wrong X-API-Key header.")(scope, receive, send)

    async def busy_bodies(scope, receive, send):
        log.warning("%s: refused before reading the body: the photo bodies in this worker would pass %d bytes",
                    scope.get("path"), settings.max_inflight_body_bytes)
        await error_reply(503, ErrorCode.BUSY_ERROR, "Too many large photos are being processed; retry shortly.",
                          retryable=True, headers={"Retry-After": "10"})(scope, receive, send)

    async def stalled_body(scope, receive, send):
        log.warning("%s: the request body stopped arriving for %.0f s", scope.get("path"), BODY_PART_TIMEOUT)
        await error_reply(503, ErrorCode.BUSY_ERROR, "The request body stopped arriving; send the request again.",
                          retryable=True, headers={"Retry-After": "10"})(scope, receive, send)

    app.add_middleware(RequestGate, limit=settings.max_body_bytes, reply=too_large, deny=unauthorized,
                       protected_prefix=tuple(p + "/" for p in basepaths), key_ok=key_ok, stalled=stalled_body,
                       inflight_limit=settings.max_inflight_body_bytes, busy=busy_bodies, reading_paths=reading_paths)

    @app.exception_handler(CustomHTTPException)
    async def custom_error(request, exc: CustomHTTPException):
        return error_reply(exc.status_code, exc.error_code, exc.detail or exc.phrase, exc.retryable)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc: RequestValidationError):
        detail = "; ".join(f"{'.'.join(str(p) for p in err['loc'] if p != 'body')}: {err['msg']}"
                           for err in exc.errors()) or "Invalid request."
        log.warning("%s %s: invalid request: %s", request.method, request.url.path, detail)
        body = _form_body(exc.body)
        request_id = caller_id(body.get("id") if body else None)
        response = service.error(request_id or str(uuid4()), 400, ErrorCode.INVALID_REQUEST_ERROR, detail, log=False)
        if request_id and request.url.path in reading_paths:
            await run_in_threadpool(service.record_rejected, request_id, body, response)
        return envelope(response)

    @app.exception_handler(DBAPIError)
    async def database_error(request, exc: DBAPIError):
        log.error("%s %s: database error", request.method, request.url.path, exc_info=exc)
        return error_reply(503, ErrorCode.STORAGE_ERROR, "The database is unavailable; retry shortly.", retryable=True,
                           headers={"Retry-After": "10"})

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request, exc: StarletteHTTPException):
        code = _HTTP_CODES.get(exc.status_code, ErrorCode.INVALID_REQUEST_ERROR)
        return error_reply(exc.status_code, code, str(exc.detail))

    @app.exception_handler(Exception)
    async def unexpected_error(request, exc: Exception):
        # the type and message only: a chained validation error would carry the whole request (the photo) into the log
        log.error("%s %s: unexpected error: %s: %s", request.method, request.url.path, type(exc).__name__,
                  str(exc)[:300])
        return error_reply(500, ErrorCode.INTERNAL_ERROR, "Internal error.", retryable=True)

    # ---- health (no authentication, no thread pool: they answer even when every read slot is busy) ----
    @app.get("/")
    async def root():
        return {"message": "Hi, I am the electricity meter reading assistant.", "model": reader.settings.model}

    @app.get("/health")
    async def health():
        """Liveness: the process answers. Use this for the load balancer and container health checks."""
        return {"status": "ok"}

    ready_limiter = anyio.CapacityLimiter(2)

    @app.get("/ready")
    async def ready():
        """Readiness: the database answers. 503 when it does not."""
        try:
            with anyio.fail_after(5):             # the thread is abandoned (not waited for) after 5 s
                await anyio.to_thread.run_sync(store.ping, limiter=ready_limiter, abandon_on_cancel=True)
        except Exception:  # noqa: BLE001
            log.warning("readiness check: database unavailable", exc_info=True)
            return JSONResponse({"status": "unavailable", "database": "unreachable"}, status_code=503)
        return {"status": "ok", "database": "ok", "readsInProgress": service.slots.busy,
                "readSlots": service.slots.size}

    # ---- readings ----
    @router.post("/extract-reading", response_model=ReadingExtractionResponse, response_model_exclude_none=True)
    async def extract_reading(request: ReadingExtractionRequest):
        """Read one meter photo: send `imageURL` (downloaded by the service) or `imageBase64`, plus the details."""
        return await admitted(service.extract_reading, request, None, time.monotonic())

    @router.post("/extract-reading/upload", response_model=ReadingExtractionResponse, response_model_exclude_none=True)
    async def extract_reading_upload(image: UploadFile = File(...), id: str | None = Form(None),
                                     consumerId: str | None = Form(None), capturedAt: str | None = Form(None),
                                     previousValue: str | None = Form(None), previousUnit: str | None = Form(None),
                                     previousCapturedAt: str | None = Form(None), metadata: str | None = Form(None)):
        """The same as extract-reading, with the photo as a multipart file (for manual testing and small clients)."""
        arrived = time.monotonic()

        def form_fields():
            return {k: v for k, v in (("id", id), ("consumerId", consumerId), ("capturedAt", capturedAt),
                                      ("previousValue", previousValue), ("previousUnit", previousUnit),
                                      ("previousCapturedAt", previousCapturedAt), ("metadata", metadata))
                    if v is not None}
        previous_names = {"value": "previousValue", "unit": "previousUnit", "capturedAt": "previousCapturedAt"}
        try:
            try:
                previous = PreviousReading(value=previousValue, unit=previousUnit,
                                           capturedAt=previousCapturedAt or None) if previousValue else None
            except ValidationError as e:              # named as the form fields were sent
                raise ValueError("; ".join(f"{previous_names.get(str(err['loc'][0]), err['loc'][0])}: {err['msg']}"
                                           for err in e.errors())) from e
            request = ReadingExtractionRequest(
                id=id or None, consumerId=consumerId, capturedAt=capturedAt or None,
                metadata=json.loads(metadata) if metadata else None, previousReading=previous)
        except (ValidationError, ValueError) as e:
            if isinstance(e, ValidationError):
                detail = "; ".join(f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in e.errors())
            else:
                detail = str(e) if e.__cause__ is not None else "metadata must be a JSON object"
            request_id = caller_id(id)
            response = service.error(request_id or str(uuid4()), 400, ErrorCode.INVALID_REQUEST_ERROR, detail, log=False)
            if request_id:
                await run_in_threadpool(service.record_rejected, request_id, form_fields(), response)
            return envelope(response)
        try:
            image_bytes = await service.validator.read_upload(image)
        except CustomHTTPException as e:
            response = service.error(request.id or str(uuid4()), e.status_code, e.error_code, e.detail, log=False)
            if request.id:
                await run_in_threadpool(service.record_rejected, request.id, form_fields(), response)
            return envelope(response)
        return await admitted(service.extract_reading, request, image_bytes, arrived)

    @router.get("/readings/{record_id}")
    def get_reading(record_id: str):
        """The stored record of one request (by request id or correlationId)."""
        row = service.metadata_store.get_record(record_id)
        if not row:
            return error_reply(404, ErrorCode.NOT_FOUND_ERROR, "No reading with this request or correlation ID.")
        return {"id": str(uuid4()), "ts": datetime.now(timezone.utc).isoformat(), "responseCode": "OK",
                "statusCode": 200,
                "result": {k: (_utc(v).isoformat() if hasattr(v, "isoformat") else v) for k, v in row.items()}}

    @router.get("/readings/{record_id}/image")
    def get_reading_image(record_id: str):
        """The photo of one request."""
        row = service.metadata_store.get_record(record_id)
        name = row.get("image_path") if row else None
        data = service.load_photo(name) if name else None
        if data is None:
            return error_reply(404, ErrorCode.NOT_FOUND_ERROR, "No saved photo for this request or correlation ID.")
        return Response(content=data, media_type=media_type(name))

    @router.post("/feedback", response_model=FeedbackResponse, response_model_exclude_none=True)
    def log_feedback(request: FeedbackRequest):
        """Confirm (`accurate: true`) or correct (`actual`) a returned reading, by its correlationId."""
        return envelope(*service.log_feedback(request))

    # ---- meter settings ----
    @router.get("/meters/{consumer_id:path}", response_model=MeterProfileResponse, response_model_exclude_none=True)
    def get_meter(consumer_id: str):
        return envelope(service.get_profile(consumer_id))

    @router.put("/meters/{consumer_id:path}", response_model=MeterProfileResponse, response_model_exclude_none=True)
    def put_meter(consumer_id: str, request: MeterProfileRequest):
        return envelope(service.set_profile(consumer_id, request))

    @router.get("/meter-types", response_model=MeterTypesResponse, response_model_exclude_none=True)
    def meter_types():
        return envelope(service.meter_types())

    for prefix in basepaths:
        app.include_router(router, prefix=prefix)
    return app
