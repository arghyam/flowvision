import atexit
import logging
import threading
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from http import HTTPStatus
from io import BytesIO
from time import perf_counter
from typing import Dict, Optional
from uuid import uuid4, UUID

import cv2
import httpx
import numpy as np
from queue import SimpleQueue
from PIL import Image, UnidentifiedImageError

from error.error import CustomHTTPException
from models.models import (
    BaseResponse,
    Error,
    FeedbackRequest,
    FeedbackResponse,
    FeedbackResponseStatus,
    FeedbackStatus,
    ReadingExtractionRequest,
    ReadingExtractionResponse,
    ReadingExtractionResult,
    ReadingExtractionResultData,
    ResponseCode,
    Status,
)
from conf.config import Config
from service.api.metadata_service import MetadataStore
from service.vision.inference_utils import (
    classify_bfm_image,
    classify_color_image,
    direct_recognize_meter_reading,
    extract_digit_image,
    load_bfm_classification,
    load_color_classification_model,
    load_individual_numbers_model,
)


@dataclass
class DownloadedImage:
    raw_bytes: bytes
    pil_image: Image.Image
    _numpy_rgb: Optional[np.ndarray] = field(default=None, init=False, repr=False)
    _numpy_bgr: Optional[np.ndarray] = field(default=None, init=False, repr=False)

    def numpy_rgb(self) -> np.ndarray:
        if self._numpy_rgb is None:
            self._numpy_rgb = np.array(self.pil_image)
        return self._numpy_rgb

    def numpy_bgr(self) -> np.ndarray:
        if self._numpy_bgr is None:
            self._numpy_bgr = cv2.cvtColor(self.numpy_rgb(), cv2.COLOR_RGB2BGR)
        return self._numpy_bgr


class ImageService:
    def __init__(self, config: Config):
        self.config = config
        self.base_logger = logging.getLogger(config.find("logs.api_logger.name"))
        self.feedback_logger = logging.getLogger(config.find("logs.feedback_request_logger.name"))
        self.extraction_logger = logging.getLogger(config.find("logs.extraction_request_logger.name"))

        self.metadata_store = MetadataStore(config=config)

        download_config = config.find("http.download", {}) or {}
        limits = httpx.Limits(
            max_connections=download_config.get("max_connections", 50),
            max_keepalive_connections=download_config.get("max_keepalive_connections", 20),
        )
        timeout = httpx.Timeout(
            connect=download_config.get("connect_timeout_seconds", 5),
            read=download_config.get("read_timeout_seconds", 10),
            write=download_config.get("read_timeout_seconds", 10),
            pool=download_config.get("read_timeout_seconds", 10),
        )
        self._http_client = httpx.Client(timeout=timeout, limits=limits, follow_redirects=True)
        atexit.register(self._http_client.close)

        self.base_logger.info("Loading inference models...")
        self.bfm_classification_model = load_bfm_classification()
        self.color_classification_model = load_color_classification_model()
        inference_workers = max(1, int(config.find("models.inference_workers", 1) or 1))
        self._inference_semaphore = threading.BoundedSemaphore(inference_workers)
        self._individual_model_pool: "SimpleQueue" = SimpleQueue()
        for _ in range(inference_workers):
            self._individual_model_pool.put(load_individual_numbers_model())
        self.base_logger.info("Initialized %s meter-reading worker(s)", inference_workers)

    def download_image(self, image_url: str) -> DownloadedImage:
        try:
            response = self._http_client.get(image_url)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise CustomHTTPException(
                status_code=HTTPStatus.BAD_REQUEST.value,
                detail=f"Failed to download image: {exc.response.status_code}",
            ) from exc
        except httpx.HTTPError as exc:
            raise CustomHTTPException(
                status_code=HTTPStatus.BAD_REQUEST.value,
                detail="Failed to download image",
            ) from exc

        try:
            pil_image = Image.open(BytesIO(response.content))
            pil_image.load()
        except (UnidentifiedImageError, OSError) as exc:
            raise CustomHTTPException(
                status_code=HTTPStatus.BAD_REQUEST.value,
                detail="Downloaded content is not a valid image",
            ) from exc

        return DownloadedImage(raw_bytes=response.content, pil_image=pil_image)

    def _acquire_inference_model(self):
        self._inference_semaphore.acquire()
        return self._individual_model_pool.get()

    def _release_inference_model(self, model) -> None:
        self._individual_model_pool.put(model)
        self._inference_semaphore.release()

    def extract_reading(self, request: ReadingExtractionRequest) -> ReadingExtractionResponse:
        status_code = HTTPStatus.OK.value
        response_code = ResponseCode.OK
        request.id = request.id if request.id else uuid4()
        request.ts = request.ts if request.ts else datetime.now()

        stage_timings: Dict[str, float] = {}
        overall_timer = perf_counter()

        self.metadata_store.enqueue_request(request)

        try:
            self.extraction_logger.info(str(request.model_dump_json()))

            stage_timer = perf_counter()
            downloaded_image = self.download_image(request.imageURL)
            stage_timings["downloadImage"] = perf_counter() - stage_timer

            stage_timer = perf_counter()
            quality_result = classify_bfm_image(
                downloaded_image.pil_image, model=self.bfm_classification_model
            )
            stage_timings["qualityClassification"] = perf_counter() - stage_timer
            quality_status = quality_result["prediction"].lower()
            quality_confidence = quality_result["confidence"]

            meter_reading_str = "Image quality too poor for recognition"
            sorted_boxes = []
            sorted_classes = []

            if quality_status == "good":
                stage_timer = perf_counter()
                model = self._acquire_inference_model()
                try:
                    meter_reading_result = direct_recognize_meter_reading(
                        downloaded_image.numpy_bgr(), model
                    )
                finally:
                    self._release_inference_model(model)
                stage_timings["meterReading"] = perf_counter() - stage_timer

                if isinstance(meter_reading_result, tuple) and len(meter_reading_result) >= 3:
                    meter_reading, sorted_boxes, sorted_classes = meter_reading_result
                else:
                    meter_reading = meter_reading_result
                    sorted_boxes, sorted_classes = [], []

                meter_reading_str = (
                    str(meter_reading[0]) if isinstance(meter_reading, tuple) else str(meter_reading)
                )
            else:
                stage_timings["meterReading"] = 0.0

            color_result = {"prediction": "unknown", "confidence": 0.0}
            if sorted_boxes:
                stage_timer = perf_counter()
                image = downloaded_image.numpy_bgr()
                last_box = sorted_boxes[-1]
                last_digit_image = extract_digit_image(image, last_box)
                color_result = classify_color_image(
                    last_digit_image, model=self.color_classification_model
                )
                stage_timings["colorClassification"] = perf_counter() - stage_timer
            else:
                stage_timings["colorClassification"] = 0.0

            last_digit_color = color_result["prediction"].lower()
            color_confidence = color_result["confidence"]

            if "nometer" in meter_reading_str.lower():
                meter_reading_status = Status.NOMETER
            elif "unclear" in meter_reading_str.lower() or quality_status == "bad":
                meter_reading_status = Status.UNCLEAR
            else:
                meter_reading_status = Status.SUCCESS

            processing_time = perf_counter() - overall_timer
            stage_timings["totalProcessing"] = processing_time

            result = ReadingExtractionResult(
                status=meter_reading_status,
                correlationId=uuid4(),
                data=ReadingExtractionResultData(
                    meterReading=meter_reading_str,
                    processingTime=processing_time,
                    qualityStatus=quality_status,
                    qualityConfidence=quality_confidence,
                    lastDigitColor=last_digit_color,
                    colorConfidence=color_confidence,
                    stageTimings={k: round(v, 6) for k, v in stage_timings.items()},
                ),
            )
            response = ReadingExtractionResponse(
                id=request.id,
                ts=datetime.now(),
                responseCode=response_code,
                statusCode=status_code,
                result=result,
            )
            self.metadata_store.enqueue_response(response)
        except CustomHTTPException as error:
            response = self.handle_custom_http_exception(error=error, id=request.id)
        except Exception as error:
            response = self.handle_other_exceptions(error=error, id=request.id)

        self.extraction_logger.info(str(response.model_dump_json()))
        if (
            isinstance(response, ReadingExtractionResponse)
            and response.result
            and response.result.data
        ):
            self.base_logger.debug("Stage timings: %s", response.result.data.stageTimings)
        return response

    def log_feedback(self, request: FeedbackRequest) -> FeedbackResponse:
        status_code = HTTPStatus.OK.value
        response_code = ResponseCode.OK
        request.id = request.id if request.id else uuid4()
        request.ts = request.ts if request.ts else datetime.now()

        self.metadata_store.enqueue_feedback(request)

        try:
            self.feedback_logger.info(str(request.model_dump_json()))
            response = FeedbackResponse(
                id=request.id,
                ts=datetime.now(),
                responseCode=response_code,
                statusCode=status_code,
                result=FeedbackStatus(status=FeedbackResponseStatus.SUBMITTED),
            )
        except Exception as error:
            response = self.handle_other_exceptions(error=error, id=request.id)

        self.feedback_logger.info(str(response.model_dump_json()))
        return response

    def handle_custom_http_exception(self, error: CustomHTTPException, id: UUID):
        status_code = error.status_code
        response_code = ResponseCode.ERROR
        error_code = error.error_code
        error_message = error.detail
        self.base_logger.error(
            "\nError type: %s\nRequest id: %s\nTrace: %s",
            error_message,
            id,
            traceback.format_exc(),
        )

        error_model = Error(errorCode=error_code, errorMsg=error_message)

        response = BaseResponse(
            id=id,
            ts=datetime.now(),
            responseCode=response_code,
            statusCode=status_code,
            error=error_model,
        )
        self.base_logger.error(str(response.model_dump_json()))
        return response

    def handle_other_exceptions(self, error: Exception, id: UUID):
        status_code = HTTPStatus.INTERNAL_SERVER_ERROR.value
        response_code = ResponseCode.ERROR
        error_message = str(error)
        self.base_logger.error(
            "\nError type: %s\nRequest id: %s\nTrace: %s",
            type(error).__name__,
            id,
            traceback.format_exc(),
        )

        error_model = Error(errorCode=HTTPStatus.INTERNAL_SERVER_ERROR.value, errorMsg=error_message)

        response = BaseResponse(
            id=id,
            ts=datetime.now(),
            responseCode=response_code,
            statusCode=status_code,
            error=error_model,
        )
        self.base_logger.error(str(response.model_dump_json()))
        return response
