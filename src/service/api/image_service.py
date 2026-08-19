import logging
from http import HTTPStatus

import traceback
from datetime import datetime
from uuid import uuid4, UUID

from fastapi import BackgroundTasks
from error.error import CustomHTTPException
from models.models import Error, Status, ReadingExtractionRequest, ReadingExtractionResponse, ReadingExtractionResult, ReadingExtractionResultData, ResponseCode, FeedbackRequest, FeedbackResponseStatus, FeedbackResponse, FeedbackStatus, BaseResponse, RolloverDigit, RolloverPosition
from conf.config import Config
from service.api.metadata_service import MetadataStore
from PIL import Image, ImageOps

import requests
from io import BytesIO
import numpy as np
import cv2

from service.vision.inference_utils import (
    load_bfm_classification,
    load_individual_numbers_model,
    load_color_classification_model,
    classify_bfm_image,
    direct_recognize_meter_reading,
    classify_color_image,
    extract_digit_image,
    RecognitionOutcome
)

# Placeholder values for `meterReading` when no numeric reading could be
# extracted. Kept as strings rather than None so the field is always present in
# the response body — see api-reference.md.
NO_METER_READING = "No digits detected in the image"
UNCLEAR_READING = "Image quality too poor for recognition"


class ImageService:
    def __init__(self, config: Config):
        self.base_logger = logging.getLogger(config.find("logs.api_logger.name"))
        self.feedback_logger = logging.getLogger(config.find("logs.feedback_request_logger.name"))
        self.extraction_logger = logging.getLogger(config.find("logs.extraction_request_logger.name"))

        self.metadata_store = MetadataStore(config=config)

        self.base_logger.info("Loading fine-tuned InceptionV3 models...")
        # Load models from config
        self.bfm_classification_model = load_bfm_classification()
        self.individual_numbers_model = load_individual_numbers_model()
        self.color_classification_model = load_color_classification_model()


    def download_image(self, imageURL):
        image = Image.open(BytesIO(requests.get(imageURL).content))
        image_buffer = BytesIO()
        image.save(image_buffer, format="PNG")
        return image_buffer.getvalue()


    def extract_reading(self, request: ReadingExtractionRequest, background_tasks: BackgroundTasks) -> ReadingExtractionResponse:
        status_code = HTTPStatus.OK.value
        response_code = ResponseCode.OK
        request.id = request.id if request.id else uuid4()
        request.ts = request.ts if request.ts else datetime.now()
        background_tasks.add_task(self.metadata_store.store_request, request)

        try:
            start_time = datetime.now()
            self.extraction_logger.info(str(request.model_dump_json()))
            original_image = self.download_image(request.imageURL)

            # Get quality status from BFM classification
            quality_result = classify_bfm_image(original_image, model=self.bfm_classification_model)
            quality_status = quality_result['prediction'].lower()
            quality_confidence = quality_result['confidence']

            # Only proceed with meter reading if quality is good
            if quality_status == 'good':
                # Detect digits and get their bounding boxes
                pil_image = Image.open(BytesIO(original_image))
                recognition = direct_recognize_meter_reading(np.array(pil_image), self.individual_numbers_model)

                if recognition.outcome is RecognitionOutcome.DIGITS_FOUND:
                    meter_reading_status = Status.SUCCESS
                    meter_reading_str = recognition.reading
                    sorted_boxes = recognition.boxes
                    raw_rollover = recognition.rollover_positions
                elif recognition.outcome is RecognitionOutcome.NO_DIGITS:
                    # Readable image with no digits in it — e.g. a photo of
                    # something that is not a meter. A valid outcome, not an error.
                    self.base_logger.info(
                        "No digits detected for request %s: %s", request.id, recognition.detail)
                    meter_reading_status = Status.NOMETER
                    meter_reading_str = NO_METER_READING
                    sorted_boxes = []
                    raw_rollover = []
                else:
                    # INVALID_IMAGE — the bytes could not be decoded. Undecodable
                    # images already fail earlier in download_image() and surface
                    # as a 500, so keep this path consistent with that.
                    raise ValueError(recognition.detail or "Image could not be decoded")
            else:
                meter_reading_status = Status.UNCLEAR
                meter_reading_str = UNCLEAR_READING
                sorted_boxes = []
                raw_rollover = []

            processing_time = (datetime.now() - start_time).total_seconds()

            # Default color result
            color_result = {"prediction": "unknown", "confidence": 0.0}

            # Only classify color if digits were detected
            if sorted_boxes and len(sorted_boxes) > 0:
                image_array = np.frombuffer(original_image, np.uint8)
                image = cv2.imdecode(image_array, cv2.IMREAD_COLOR)
                last_box = sorted_boxes[-1]
                last_digit_image = extract_digit_image(image, last_box)
                color_result = classify_color_image(last_digit_image, model=self.color_classification_model)

            last_digit_color = color_result['prediction'].lower()
            color_confidence = color_result['confidence']

            rollover_objs = [
                RolloverPosition(
                    position=rp['position'],
                    selectedDigit=RolloverDigit(**rp['selectedDigit']),
                    alternateDigit=RolloverDigit(**rp['alternateDigit'])
                )
                for rp in raw_rollover
            ]

            result = ReadingExtractionResult(
                status=meter_reading_status,
                correlationId=uuid4(),
                data=ReadingExtractionResultData(
                    meterReading=meter_reading_str,
                    hasRollover=len(rollover_objs) > 0,
                    rolloverPositions=rollover_objs if rollover_objs else None,
                    processingTime=processing_time,
                    qualityStatus=quality_status,
                    qualityConfidence=quality_confidence,
                    lastDigitColor=last_digit_color,
                    colorConfidence=color_confidence
                )
            )
            response = ReadingExtractionResponse(
                id=request.id,
                ts=datetime.now(),
                responseCode=response_code,
                statusCode=status_code,
                result=result
            )
            background_tasks.add_task(self.metadata_store.store_response, response)
        except CustomHTTPException as e:
            response = self.handle_custom_http_exception(error=e, id=request.id)
        except Exception as e:
            response = self.handle_other_exceptions(error=e, id=request.id)
  
        self.extraction_logger.info(str(response.model_dump_json()))
        return response

    def log_feedback(self, request: FeedbackRequest, background_tasks: BackgroundTasks):
        status_code = HTTPStatus.OK.value
        response_code = ResponseCode.OK
        request.id = request.id if request.id else uuid4()
        request.ts = request.ts if request.ts else datetime.now()
        background_tasks.add_task(self.metadata_store.store_feedback, request)

        try:
            self.feedback_logger.info(str(request.model_dump_json()))
            response = FeedbackResponse(
                id=request.id,
                ts=datetime.now(),
                responseCode=response_code,
                statusCode=status_code,
                result=FeedbackStatus(status=FeedbackResponseStatus.SUBMITTED)
            )
        except Exception as e:
            response = self.handle_other_exceptions(error=e, id=request.id)
            
        self.feedback_logger.info(str(response.model_dump_json()))
        return response

    def handle_custom_http_exception(self, error: CustomHTTPException, id: UUID):
        status_code = error.status_code
        response_code = ResponseCode.ERROR
        error_code = error.error_code
        error_message = error.detail
        self.base_logger.error("\nError type: %s\nRequest id: %s\nTrace: %s", error_message, id, traceback.format_exc())

        error = Error(errorCode=error_code, errorMsg=error_message)

        response = BaseResponse(
            id=id,
            ts=datetime.now(),
            responseCode=response_code,
            statusCode=status_code,
            error=error
        )
        self.base_logger.error(str(response.model_dump_json()))
        return response

    def handle_other_exceptions(self, error: Exception, id: UUID):
        status_code = HTTPStatus.INTERNAL_SERVER_ERROR.value
        response_code = ResponseCode.ERROR
        error_message = str(error)
        self.base_logger.error("\nError type: %s\nRequest id: %s\nTrace: %s", type(error).__name__, id, traceback.format_exc())

        error = Error(errorCode=HTTPStatus.INTERNAL_SERVER_ERROR.value, errorMsg=error_message)

        response = BaseResponse(
            id=id,
            ts=datetime.now(),
            responseCode=response_code,
            statusCode=status_code,
            error=error
        )
        self.base_logger.error(str(response.model_dump_json()))
        return response
