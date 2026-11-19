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
from PIL import Image, ImageOps, UnidentifiedImageError

import time
import requests
from io import BytesIO
import numpy as np
import cv2

from service.vision.yolox_reader import UnsureColour, colour_second_opinion
from service.vision.inference_utils import (
    load_bfm_classification,
    load_individual_numbers_model,
    load_color_classification_model,
    load_orientation_model,
    load_screen_check,
    check_screen,
    load_digit_reader,
    turn_upright,
    classify_bfm_image,
    direct_recognize_meter_reading,
    classify_color_image,
    extract_digit_image,
    RecognitionOutcome
)

# Seconds to wait for the photo: (connect, read). Without a limit a slow or dead
# link would hold a worker indefinitely.
DOWNLOAD_TIMEOUT = (10, 30)
# The read limit above restarts with every piece of data, so a server that sends a trickle could still hold the worker
# for minutes (and every request waiting behind it). The whole download must finish within this many seconds.
DOWNLOAD_DEADLINE = 30

# Placeholder values for `meterReading` when no numeric reading could be
# extracted. Kept as strings rather than None so the field is always present in
# the response body — see api-reference.md.
NO_METER_READING = "No digits detected in the image"
UNCLEAR_READING = "Image quality too poor for recognition"
SCREEN_OR_PAPER = "Photo of a screen or paper, not the meter"

# What the operator is asked to do when no reading is given (operatorMessage). Glare is the usual cause of an unclear
# last wheel, and the usual remedy is another photo from a slightly different angle, or the meter shaded by a hand.
RETAKE_NO_METER = "No meter counter was found. Please take a close photo of the meter's number window, straight on."
RETAKE_SCREEN = ("This looks like a photo of a phone or computer screen, or of a paper. Please take the photo of the "
                 "meter itself, at the meter.")
RETAKE_COLOUR = ("The last wheel could not be read: it may be hidden by glare. Please take another photo, tilting the "
                 "phone a little or shading the meter with your hand.")
RETAKE_LAST_WHEEL = ("The last number on the right could not be read clearly: it may be turning, blurred or hidden by "
                     "glare. Please take another photo, holding the phone still and straight over the number window, "
                     "tilting it a little or shading the meter with your hand to avoid glare.")
RETAKE_UNCLEAR = ("The numbers could not be read clearly. Please take another photo: hold the phone straight over the "
                  "number window, close enough to fill most of the photo, keep it still, and avoid glare by tilting the "
                  "phone a little or shading the meter with your hand.")


def retake_message(status, reason):
    """The operator message for a response with no reading; None for a reading."""
    if status == Status.SUCCESS:
        return None
    if reason and reason.startswith("photo of a "):
        return RETAKE_SCREEN
    if status == Status.NOMETER:
        return RETAKE_NO_METER
    if reason and ("colour" in reason or "may be red" in reason):
        return RETAKE_COLOUR
    if reason and reason.startswith("the last wheel"):
        return RETAKE_LAST_WHEEL
    return RETAKE_UNCLEAR


class ImageService:
    def __init__(self, config: Config):
        self.base_logger = logging.getLogger(config.find("logs.api_logger.name"))
        self.feedback_logger = logging.getLogger(config.find("logs.feedback_request_logger.name"))
        self.extraction_logger = logging.getLogger(config.find("logs.extraction_request_logger.name"))

        self.metadata_store = MetadataStore(config=config)

        self.max_black_redness = config.find("reader_max_black_redness", 6.0)
        self.read_poor_photos = config.find("reader_poor_photos", False)
        # Larger photos are refused before they are decoded (memory). Field photos are under 1 MB and 13 megapixels.
        self.max_photo_bytes = int(config.find("file_size_limit", 10485760))
        self.max_photo_pixels = int(config.find("max_photo_pixels", 60000000))

        self.base_logger.info("Loading fine-tuned InceptionV3 models...")
        # Load models from config
        self.bfm_classification_model = load_bfm_classification()
        self.digit_reader = load_digit_reader()
        # the old digit model is a large one: only load it when it does the reading
        self.individual_numbers_model = None if self.digit_reader else load_individual_numbers_model()
        # the old colour model (Sahaj main's fastai one) is only used when the reader has no colour model of its own
        self.color_classification_model = None if getattr(self.digit_reader, 'colour', None) else load_color_classification_model()
        self.orientation_model = load_orientation_model()
        self.screen_check = load_screen_check()


    def download_image(self, imageURL):
        # Phones often store the photo sideways with a rotation flag; apply it
        # so digits run left to right as the operator saw them
        started = time.monotonic()
        content = bytearray()
        with requests.get(imageURL, timeout=DOWNLOAD_TIMEOUT, stream=True) as reply:
            # a 404 page or similar is not a photo: fail here with the reason, not in the decoder
            reply.raise_for_status()
            while True:
                # read1 returns whatever has arrived, so the size and time limits are checked as the photo comes in
                chunk = reply.raw.read1(65536, decode_content=True)
                if not chunk:
                    break
                content.extend(chunk)
                if len(content) > self.max_photo_bytes:
                    raise ValueError(f"Photo is larger than {self.max_photo_bytes // 1048576} MB")
                if time.monotonic() - started > DOWNLOAD_DEADLINE:
                    raise TimeoutError(f"Photo took longer than {DOWNLOAD_DEADLINE} seconds to download")
        try:
            image = Image.open(BytesIO(bytes(content)))
        except UnidentifiedImageError:
            # e.g. an error page or an expired link: say so plainly, not "cannot identify image file <_io.BytesIO ...>"
            raise ValueError("The link did not return an image") from None
        if image.width * image.height > self.max_photo_pixels:
            raise ValueError(f"Photo is larger than {self.max_photo_pixels // 1000000} megapixels")
        image = ImageOps.exif_transpose(image)
        # A photo saved by an editing app can be CMYK (a print format) or similar, which cannot be stored as PNG
        if image.mode not in ("RGB", "L"):
            image = image.convert("RGB")
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

            # A photo of a phone / computer screen or of paper is not the meter: refused before anything is read
            # (switch: FLOWVISION_SCREEN_CHECK). Judged on the photo as received, as the check was trained.
            screen = check_screen(Image.open(BytesIO(original_image)), self.screen_check)
            refusal_reason = None
            if screen and screen['refused']:
                self.base_logger.info("Request %s refused: photo of a %s, not the meter (screen %.3f, paper %.3f)",
                                      request.id, screen['kind'], screen['screen'], screen['paper'])
                refusal_reason = f"photo of a {screen['kind']}"
                quality_status, quality_confidence = 'not checked', 0.0
                meter_reading_status = Status.NOMETER
                meter_reading_str = SCREEN_OR_PAPER
                sorted_boxes = []
                raw_rollover = []
            else:
                # A sideways or upside-down photo is turned upright first: the quality
                # check, the digit model and the colour check below all see it upright
                original_image, angle, angle_confidence = turn_upright(original_image, self.orientation_model)
                if angle:
                    self.extraction_logger.info("Request %s: photo turned upright by %s degrees (confidence %.2f)",
                                                request.id, angle, angle_confidence)

                # Get quality status from BFM classification
                quality_result = classify_bfm_image(original_image, model=self.bfm_classification_model)
                quality_status = quality_result['prediction'].lower()
                quality_confidence = quality_result['confidence']

                # Only proceed with meter reading if quality is good
                if quality_status == 'good' or self.read_poor_photos:
                    # Detect digits and get their bounding boxes
                    # Passed as a PIL image so it is converted to the BGR order the
                    # detector was trained on (a raw RGB array would be taken as BGR)
                    pil_image = Image.open(BytesIO(original_image)).convert("RGB")
                    recognition = direct_recognize_meter_reading(pil_image, self.individual_numbers_model, self.digit_reader)

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
                    elif recognition.outcome is RecognitionOutcome.REFUSED:
                        # Digits were found but the counter could not be read safely.
                        # A refusal is safer than a wrong number reaching the dashboard.
                        self.base_logger.info(
                            "Reading refused for request %s: %s", request.id, recognition.detail)
                        refusal_reason = recognition.detail
                        meter_reading_status = Status.UNCLEAR
                        meter_reading_str = UNCLEAR_READING
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
                try:
                    last_digit_image = extract_digit_image(image, last_box)
                    colour_check = getattr(self.digit_reader, 'colour', None)
                    if getattr(colour_check, 'pair', False):
                        before = extract_digit_image(image, sorted_boxes[-2]) if len(sorted_boxes) >= 2 else None
                        try:
                            color_result = colour_check(last_digit_image, before)
                        except UnsureColour as unsure:
                            # Unsure: read only if the one-wheel colour model is sure and the same way;
                            # otherwise refused below, keeping the default "unknown" colour result
                            second = colour_second_opinion(self.digit_reader, last_digit_image, unsure)
                            if second is None:
                                raise
                            color_result = second
                    elif colour_check is not None:
                        color_result = colour_check(last_digit_image)
                    else:
                        color_result = classify_color_image(last_digit_image, model=self.color_classification_model)
                    # A red wheel called black makes the stored value 10 times too high
                    redness = recognition.last_wheel_redness
                    if color_result["prediction"].lower() == "black" and redness is not None \
                            and redness >= self.max_black_redness:
                        raise ValueError(f"the last wheel may be red ({redness:.1f})")
                except ValueError as e:
                    # The last wheel's colour decides whether the reading is divided
                    # by 10 downstream, so a reading whose colour cannot be checked
                    # is refused rather than sent with an unknown colour
                    self.base_logger.info("Reading refused for request %s: %s", request.id, e)
                    refusal_reason = str(e)
                    meter_reading_status = Status.UNCLEAR
                    meter_reading_str = UNCLEAR_READING
                    raw_rollover = []

            last_digit_color = color_result['prediction'].lower()
            color_confidence = color_result['confidence']

            # A red last wheel is the digit after the decimal point, so the service writes the point itself, as the live
            # service (bfm-phase-1) does: "0097574" with a red last wheel is sent as "009757.4". Callers use the value as
            # returned. Leading zeros are kept; rolloverPositions count digits, not the point.
            if meter_reading_status == Status.SUCCESS and last_digit_color == 'red' and len(meter_reading_str) > 1:
                meter_reading_str = f"{meter_reading_str[:-1]}.{meter_reading_str[-1]}"

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
                    colorConfidence=color_confidence,
                    # Left out of a reading; with no reading it tells the operator how to take a better photo
                    operatorMessage=retake_message(meter_reading_status, refusal_reason)
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
