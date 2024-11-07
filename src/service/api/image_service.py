import logging
from http import HTTPStatus
import traceback
from datetime import datetime
import boto3
from botocore.config import Config as BotoConfig
from fastapi import HTTPException

from validation.validators import ImageValidator
from service.vision.openai_vision_service import OpenAIVisionService
from models.models import Request, Response, Error, DataField
from conf.config import Config


class ImageService:
    def __init__(self, config: Config):
        self.llm_service = OpenAIVisionService()
        self.image_validator = ImageValidator()
        self.s3_client = boto3.client('s3', config=BotoConfig(signature_version='s3v4'))
        self.bucket_name = config.s3_bucket_name

    def construct_response(self, id: str, response_code: str, status_code: int, data: DataField | Error, params=None):
        return Response(
            id=id,
            ts=datetime.now().isoformat(),
            responseCode=response_code,
            statusCode=status_code,
            data=data,
            params=params
        )

    def _send_to_storage(self, image_bytes):
        pass

    async def read_meter(self, request: Request) -> Response:
        status_code = HTTPStatus.OK.value
        response_code = HTTPStatus.OK.phrase

        try:
            image_bytes = await self.image_validator.validate(request)
            self._send_to_storage(image_bytes=image_bytes)
            answer = self.llm_service.read(image_bytes)
            await request.image.close()

            data = DataField(data=answer)

            return self.construct_response(
                id=request.id,
                response_code=response_code,
                status_code=status_code,
                data=data,
                params=None
            )

        except HTTPException as e:
            status_code = e.status_code
            response_code = e.detail['phrase']
            error_message = e.detail['phrase']
            error_details = e.detail['details']
            logging.error("\nError type: %s\nTrace: %s", error_message, traceback.format_exc())

            data = Error(errorCode=status_code, errorMessage=error_message, errorDetails=error_details)

            return self.construct_response(
                id=request.id,
                response_code=response_code,
                status_code=status_code,
                data=data,
                params=None
            )

        except Exception as e:
            status_code = HTTPStatus.INTERNAL_SERVER_ERROR.value
            response_code = HTTPStatus.INTERNAL_SERVER_ERROR.phrase
            error_message = type(e).__name__
            error_details = str(e)
            logging.error("\nError type: %s\nTrace: %s", error_message, traceback.format_exc())

            data = Error(errorCode=HTTPStatus.INTERNAL_SERVER_ERROR.value, errorMessage=error_message, errorDetails=error_details)

            return self.construct_response(
                id=request.id,
                response_code=response_code,
                status_code=status_code,
                data=data,
                params=None
            )

        finally:
            if request.image:
                await request.image.close()

    def generate_presigned_url(self, object_key: str, content_type: str = None, expiration: int = 3600) -> str:
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
        allowed_types = {
            'jpeg': 'image/jpeg',
            'jpg': 'image/jpeg',
            'png': 'image/png'
        }
        
        # Extract file extension from object_key if content_type not provided
        if not content_type:
            ext = object_key.split('.')[-1].lower()
            content_type = ext if ext in allowed_types else 'jpeg'
        
        if content_type.lower() not in allowed_types:
            raise HTTPException(
                status_code=HTTPStatus.BAD_REQUEST.value,
                detail={
                    'phrase': HTTPStatus.BAD_REQUEST.phrase,
                    'details': f"Invalid content type. Allowed types are: {', '.join(allowed_types.keys())}"
                }
            )

        try:
            url = self.s3_client.generate_presigned_url(
                'put_object',
                Params={
                    'Bucket': self.bucket_name,
                    'Key': object_key,
                    'ContentType': allowed_types[content_type.lower()]
                },
                ExpiresIn=expiration
            )
            return url
        except Exception as e:
            logging.error(f"Error generating presigned URL: {str(e)}")
            raise HTTPException(
                status_code=HTTPStatus.INTERNAL_SERVER_ERROR.value,
                detail={
                    'phrase': HTTPStatus.INTERNAL_SERVER_ERROR.phrase,
                    'details': f"Failed to generate presigned URL: {str(e)}"
                }
            )
