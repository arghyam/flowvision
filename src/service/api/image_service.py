import logging
from http import HTTPStatus
import traceback
from datetime import datetime
import boto3
import requests
from botocore.config import Config as BotoConfig
from uuid import uuid4, UUID

from error.error import CustomHTTPException
from validation.validators import ImageValidator
from service.vision.openai_vision_service import OpenAIVisionService
from models.models import Error, ImageUploadRequest, ImageUploadResponse, ImageUploadResult
from models.models import Status, ReadingExtractionRequest, ReadingExtractionResponse, ReadingExtractionResult
from conf.config import Config


class ImageService:
    def __init__(self, config: Config):
        self.llm_service = OpenAIVisionService()
        self.image_validator = ImageValidator(config)
        self.presigned_url_expiration = config.find("presigned_url_expiration")
        self.bucket_name = config.find("s3.bucket_name")
        self.s3_client = boto3.client(
            "s3",
            endpoint_url=config.find("s3.endpoint_url"),
            config=BotoConfig(signature_version='s3v4')
        )

    def _send_to_storage(self, url, image_bytes):
        try:
            http_response = requests.put(url=url, data=image_bytes)
        except Exception as e:
            logging.error(f"Error uploading image to presigned URL: {str(e)}")
            raise CustomHTTPException(
                status_code=HTTPStatus.INTERNAL_SERVER_ERROR.value,
                detail=f"Error uploading image to presigned URL: {str(e)}"
            )

        if http_response.status_code != HTTPStatus.OK.value:
            raise CustomHTTPException(
                status_code=http_response.status_code,
                detail=f"Error uploading image to presigned URL: {http_response.content}"
            )

    async def _get_from_storage(self, url):
        try:
            http_response = requests.get(url=url)
        except Exception as e:
            logging.error(f"Error retrieving image from presigned URL: {str(e)}")
            raise CustomHTTPException(
                status_code=HTTPStatus.INTERNAL_SERVER_ERROR.value,
                detail=f"Error retrieving image from presigned URL: {str(e)}"
            )
        if http_response.status_code != HTTPStatus.OK.value:
            raise CustomHTTPException(
                status_code=http_response.status_code,
                detail=f"Error retrieving image from presigned URL: {http_response.reason}"
            )
        return http_response.content

    async def upload_image(self, request: ImageUploadRequest) -> ImageUploadResponse:
        status_code = HTTPStatus.OK.value
        response_code = HTTPStatus.OK.phrase

        try:
            image_bytes, content_type, file_name = await self.image_validator.validate(request)
            url = self.generate_presigned_url(object_key=file_name, content_type=content_type)
            self._send_to_storage(url=url, image_bytes=image_bytes)

            await request.image.close()

            result = ImageUploadResult(imageURL=url)

            return ImageUploadResponse(
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

        finally:
            if request.image:
                await request.image.close()

    async def extract_reading(self, request: ReadingExtractionRequest) -> ReadingExtractionResponse:
        status_code = HTTPStatus.OK.value
        response_code = HTTPStatus.OK.phrase

        try:
            image_bytes = await self._get_from_storage(request.imageURL)
            meter_reading = self.llm_service.read(image_bytes=image_bytes)

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

    def generate_presigned_url(self, object_key: str, content_type: str = None, expiration: int = None) -> str:
        """
        Generate a presigned URL for uploading an object to S3.

        Args:
            object_key (str): The key (path) where the object will be stored in S3
            content_type (str): The content type of the image (jpeg, jpg, or png)
            expiration (int): URL expiration time in seconds (default: 1 hour)

        Returns:
            str: Presigned URL for uploading

        Raises:
            HTTPException: If content type is invalid or URL generation fails
        """

        try:
            if expiration is None:
                expiration = self.presigned_url_expiration
            url = self.s3_client.generate_presigned_url(
                'put_object',
                Params={
                    'Bucket': self.bucket_name,
                    'Key': object_key,
                    'ContentType': content_type
                },
                ExpiresIn=expiration
            )
            return url
        except Exception as e:
            logging.error(f"Error generating presigned URL: {str(e)}")
            raise CustomHTTPException(
                status_code=HTTPStatus.INTERNAL_SERVER_ERROR.value,
                detail=f"Failed to generate presigned URL: {str(e)}"
            )

    def handle_custom_http_exception(self, error: CustomHTTPException, id: UUID | None = None):
        status_code = error.status_code
        response_code = error.phrase
        error_message = error.detail
        logging.error("\nError type: %s\nTrace: %s", error_message, traceback.format_exc())

        error = Error(errorCode=status_code, errorMsg=error_message)

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
