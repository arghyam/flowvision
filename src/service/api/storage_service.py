from error.error import CustomHTTPException
from validation.validators import ImageValidator
from models.models import ImageUploadRequest, ImageUploadResponse, ImageUploadResult
from conf.config import Config

import logging
from http import HTTPStatus
from datetime import datetime
import requests
import boto3
from botocore.config import Config as BotoConfig
from uuid import uuid4, UUID

import os


class StorageService:
  def __init__(self, config: Config):
    feedback_logger_name = config.find("logs.feedback_request_logger.name")
    self.logger = logging.getLogger(feedback_logger_name)
    self.config = config
    self.image_validator = ImageValidator(config)
    self.presigned_url_expiration = config.find("presigned_url_expiration")
    self.bucket_name = config.find("s3.bucket_name")
    self.s3_client = boto3.client("s3", aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"), aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"), config=BotoConfig(signature_version='s3v4')
    # endpoint_url=config.find("s3.endpoint_url"),
    )

  def _send_to_storage(self, url, image_bytes, content_type):
    try:
      http_response = requests.put(
        url=url,
        data=image_bytes,
        headers={'Content-Type': content_type}
      )
    except Exception as e:
      self.logger.error(f"Error uploading image to presigned URL: {str(e)}")
      raise CustomHTTPException(
        status_code=HTTPStatus.INTERNAL_SERVER_ERROR.value,
        detail=f"Error uploading image to presigned URL: {str(e)}"
      )

    if http_response.status_code != HTTPStatus.OK.value:
      self.logger.error(f"AWS PUT BUCKET RESPONSE: {http_response.__dict__}")
      raise CustomHTTPException(
        status_code=http_response.status_code,
        detail=f"Error uploading image to presigned URL: {http_response.reason}"
      )

  async def _get_from_storage(self, url):
    try:
      http_response = requests.get(url=url)
    except Exception as e:
      self.logger.error(f"Error retrieving image from presigned URL: {str(e)}")
      raise CustomHTTPException(
        status_code=HTTPStatus.INTERNAL_SERVER_ERROR.value,
        detail=f"Error retrieving image from presigned URL: {str(e)}"
      )
    if http_response.status_code != HTTPStatus.OK.value:
      self.logger.error(f"AWS GET BUCKET RESPONSE: {http_response.__dict__}")
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
      upload_url = self.generate_presigned_upload_url(object_key=file_name, content_type=content_type)
      self.logger.info(msg=f"PRESIGNED UPLOAD URL GENERATED: {upload_url}")
      download_url = self.generate_presigned_download_url(object_key=file_name)
      self.logger.info(msg=f"PRESIGNED DOWNLOAD URL GENERATED: {download_url}")

      self._send_to_storage(url=upload_url, image_bytes=image_bytes, content_type=content_type)
      await request.image.close()

      result = ImageUploadResult(imageURL=download_url)

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

  def generate_presigned_upload_url(self, object_key: str, content_type: str = None, expiration: int = None) -> str:
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
      self.logger.error(f"Error generating presigned upload URL: {str(e)}")
      raise CustomHTTPException(
          status_code=HTTPStatus.INTERNAL_SERVER_ERROR.value,
          detail=f"Failed to generate presigned upload URL: {str(e)}"
      )

  def generate_presigned_download_url(self, object_key: str, expiration: int = None) -> str:
    try:
      if expiration is None:
        expiration = self.presigned_url_expiration
      url = self.s3_client.generate_presigned_url(
        'get_object',
        Params={
            'Bucket': self.bucket_name,
            'Key': object_key,
        },
        ExpiresIn=expiration
      )
      return url
    except Exception as e:
      self.logger.error(f"Error generating presigned download URL: {str(e)}")
      raise CustomHTTPException(
        status_code=HTTPStatus.INTERNAL_SERVER_ERROR.value,
        detail=f"Failed to generate presigned download URL: {str(e)}"
      )
