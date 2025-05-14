import logging
from http import HTTPStatus

import traceback
from datetime import datetime
from uuid import uuid4, UUID

from fastapi import BackgroundTasks
from error.error import CustomHTTPException
from service.vision.openai_vision_service import OpenAIVisionService
from service.vision.qwen_vision_service import QwenVisionService
from service.vision.inception_v3_service import InceptionV3VisionService
from models.models import Error, Status, ReadingExtractionRequest, ReadingExtractionResponse, ReadingExtractionResult, ReadingExtractionResultData, ResponseCode, FeedbackRequest, FeedbackResponseStatus, FeedbackResponse, FeedbackStatus, BaseResponse
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
    extract_digit_image
)


class ImageService:
    def __init__(self, config: Config):
        self.base_logger = logging.getLogger(config.find("logs.api_logger.name"))
        self.feedback_logger = logging.getLogger(config.find("logs.feedback_request_logger.name"))
        self.extraction_logger = logging.getLogger(config.find("logs.extraction_request_logger.name"))

        self.metadata_store = MetadataStore(config=config)

        vision_model: str = config.find("vision_model")
        print("Vision model: ", vision_model)
        if vision_model.lower() == 'inceptionv3':
            self.vision_service = InceptionV3VisionService(config=config)

        # vision_model: str = config.find("vision_model")
        # if ('gpt-4o'.lower() == vision_model.lower()):
        #     self.model = "GPT"
        #     self.vision_service = OpenAIVisionService(config=config)
        # elif (vision_model.lower().__contains__('qwen')):
        #     self.vision_service = QwenVisionService()
        #     self.model = "QWEN"
        # else:
        #     raise Exception("Configured model not available for service...")

        self.resizing_width = config.find("image_resizing.width")
        self.resizing_height = config.find("image_resizing.height")
        self.crop_left = config.find("image_crop.left")
        self.crop_top = config.find("image_crop.top")
        self.crop_right = config.find("image_crop.right")
        self.crop_bottom = config.find("image_crop.bottom")

    def resize_image(self, image: Image.Image, max_height=800, max_width=1000):
        """Resize the image only if it exceeds the specified dimensions."""
        original_width, original_height = image.size

        # Check if resizing is needed
        if original_width > max_width or original_height > max_height:
            # Calculate the new size maintaining the aspect ratio
            aspect_ratio = original_width / original_height
            if original_width > original_height:
                new_width = max_width
                new_height = int(max_width / aspect_ratio)
            else:
                new_height = max_height
                new_width = int(max_height * aspect_ratio)

            # Resize the image using LANCZOS for high-quality downscaling
            return image.resize((new_width, new_height), Image.LANCZOS)
        else:
            return image

    def crop_image(self, image: Image.Image):
        width, height = image.size   # Get dimensions
        left = self.crop_left * width
        top = self.crop_top * height
        right = self.crop_right * width
        bottom = self.crop_bottom * height
        cropped_image = image.crop((left, top, right, bottom))
        return cropped_image

    def preprocess_image(self, imageURL):
        image = Image.open(BytesIO(requests.get(imageURL).content))
        image = ImageOps.exif_transpose(image)
        resized_image = self.resize_image(image, max_height=self.resizing_height, max_width=self.resizing_width)
        cropped_image = self.crop_image(resized_image)
        image_buffer = BytesIO()
        cropped_image.save(image_buffer, format="PNG")
        # cropped_image.save("image_used.png")
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
            cropped_image = self.preprocess_image(request.imageURL)

            # Get quality status from BFM classification
            quality_result = classify_bfm_image(cropped_image)
            quality_status = quality_result['prediction'].lower()
            quality_confidence = quality_result['confidence']

            # Only proceed with meter reading if quality is good
            if quality_status == 'good':
                # Detect digits and get their bounding boxes
                meter_reading_result = self.vision_service.extract(image_bytes=cropped_image)
                # Expecting: meter_reading, sorted_boxes, sorted_classes
                if isinstance(meter_reading_result, tuple) and len(meter_reading_result) >= 3:
                    meter_reading, sorted_boxes, sorted_classes = meter_reading_result
                else:
                    meter_reading = meter_reading_result
                    sorted_boxes, sorted_classes = [], []

                # Convert tuple to string if necessary
                meter_reading_str = str(meter_reading[0]) if isinstance(meter_reading, tuple) else str(meter_reading)
            else:
                meter_reading_str = "Image quality too poor for recognition"
                sorted_boxes = []
                meter_reading_status = Status.UNCLEAR

            processing_time = (datetime.now() - start_time).total_seconds()

            # Default color result
            color_result = {"prediction": "unknown", "confidence": 0.0}

            # Only classify color if digits were detected
            if sorted_boxes and len(sorted_boxes) > 0:
                image_array = np.frombuffer(cropped_image, np.uint8)
                image = cv2.imdecode(image_array, cv2.IMREAD_COLOR)
                last_box = sorted_boxes[-1]
                last_digit_image = extract_digit_image(image, last_box)
                color_result = classify_color_image(last_digit_image)

            last_digit_color = color_result['prediction'].lower()
            color_confidence = color_result['confidence']

            if 'nometer' in meter_reading_str.lower():
                meter_reading_status = Status.NOMETER
            elif 'unclear' in meter_reading_str.lower() or quality_status == 'bad':
                meter_reading_status = Status.UNCLEAR
            else:
                meter_reading_status = Status.SUCCESS
             
            result = ReadingExtractionResult(
                status=meter_reading_status,
                correlationId=uuid4(),
                data=ReadingExtractionResultData(
                    meterReading=meter_reading_str,
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
