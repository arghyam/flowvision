import logging
from http import HTTPStatus
import traceback
from datetime import datetime
import boto3
from botocore.config import Config as BotoConfig
from uuid import uuid4, UUID

from error.error import CustomHTTPException
from validation.validators import ImageValidator
from service.vision.openai_vision_service import OpenAIVisionService
from service.vision.qwen_vision_service import QwenVisionService
from models.models import Error, Status, ReadingExtractionRequest, ReadingExtractionResponse, ReadingExtractionResult
from conf.config import Config
from PIL import Image
import base64


import requests
from io import BytesIO


class ImageService:
    def __init__(self, config: Config):
        vision_model: str = config.find("vision_model")
        if ('gpt-4o'.lower() == vision_model.lower()):
            self.model = "GPT"
            self.vision_service = OpenAIVisionService()
        elif (vision_model.lower().__contains__('qwen')):
            self.vision_service = QwenVisionService()
            self.model = "QWEN"
        else:
            raise Exception("Configured model not available for service...")
        
        self.resizing_width = config.find("image_resizing.width")
        self.resizing_height = config.find("image_resizing.height")
        self.crop_left = config.find("image_crop.left")
        self.crop_top = config.find("image_crop.top")
        self.crop_right = config.find("image_crop.right")
        self.crop_bottom = config.find("image_crop.bottom")

    
    def resize_image(self, image, max_height=800, max_width=1000):
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
   
    def crop_image(self, image):
        width, height = image.size   # Get dimensions
        left = self.crop_left * width
        top = self.crop_top * height
        right = self.crop_right * width
        bottom = self.crop_bottom * height
        cropped_image = image.crop((left, top, right, bottom))
        return cropped_image

    def preprocess_image(self, imageURL):
        image = Image.open(BytesIO(requests.get(imageURL).content))
        resized_image = self.resize_image(image)
        cropped_image = self.crop_image(resized_image)
        image_buffer = BytesIO()
        cropped_image.save(image_buffer, format = "PNG")
        return image_buffer.getvalue()

    def extract_reading(self, request: ReadingExtractionRequest) -> ReadingExtractionResponse:
        status_code = HTTPStatus.OK.value
        response_code = HTTPStatus.OK.phrase

        try:
            cropped_image = self.preprocess_image(request.imageURL)
            meter_reading = self.vision_service.extract(image_bytes=cropped_image)

            if 'nometer' in meter_reading.lower():
                meter_reading_status = Status.NOMETER
            elif 'unclear' in meter_reading.lower():
                meter_reading_status = Status.UNCLEAR
            else:
                meter_reading_status = Status.SUCCESS

            result = ReadingExtractionResult(
                status=meter_reading_status,
                meterReading=meter_reading,
                meterBrand=None
            )

            return ReadingExtractionResponse(
                id=request.id if request.id else uuid4(),
                ts=datetime.now(),
                responseCode=response_code,
                statusCode=status_code,
                result=result
            )
        except CustomHTTPException as e:
            return self.handle_custom_http_exception(error=e, id=request.id or None)
        except Exception as e:
            return self.handle_other_exceptions(error=e, id=request.id or None)



    def handle_custom_http_exception(self, error: CustomHTTPException, id: UUID | None = None):
        status_code = error.status_code
        response_code = error.phrase
        error_code = error.error_code
        error_message = error.detail
        logging.error("\nError type: %s\nTrace: %s", error_message, traceback.format_exc())

        error = Error(errorCode=error_code, errorMsg=error_message)

        return ReadingExtractionResponse(
            id=id if id else uuid4(),
            ts=datetime.now(),
            responseCode=response_code,
            statusCode=status_code,
            error=error
        )

    def handle_other_exceptions(self, error: Exception, id: UUID | None = None):
        status_code = HTTPStatus.INTERNAL_SERVER_ERROR.value
        response_code = HTTPStatus.INTERNAL_SERVER_ERROR.phrase
        error_message = str(error)
        logging.error("\nError type: %s\nTrace: %s", type(error).__name__, traceback.format_exc())

        error = Error(errorCode=HTTPStatus.INTERNAL_SERVER_ERROR.value, errorMsg=error_message)

        return ReadingExtractionResponse(
            id=id if id else uuid4(),
            ts=datetime.now(),
            responseCode=response_code,
            statusCode=status_code,
            error=error
        )
