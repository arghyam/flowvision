from http import HTTPStatus

import filetype
from fastapi import UploadFile

from conf.config import Config
from error.error import CustomHTTPException, ErrorCode


class ImageValidator:
    """FlowVision's image validator (size, and the real file type from the bytes), with WebP added. The type an
    upload declares is not trusted: many HTTP clients send application/octet-stream for any file."""
    accepted_file_types = ['image/jpeg', 'image/jpg', 'image/png', 'image/webp', 'jpeg', 'jpg', 'png', 'webp']

    def __init__(self, config: Config):
        self.config = config
        self.file_size_limit = self.config.find("file_size_limit", default=20971520)

    async def read_upload(self, image: UploadFile) -> bytes:
        """Reads an uploaded file, refusing it as soon as it passes the size limit. The bytes are checked later."""
        if image.size is not None:
            self._validate_image_size(image_size=image.size)
        data = bytearray()
        while content := await image.read(1024 * 64):
            data += content
            self._validate_image_size(image_size=len(data))
        return bytes(data)

    def validate_bytes(self, image_bytes: bytes) -> bytes:
        """Checks raw image bytes (upload, base64 or downloaded)."""
        if len(image_bytes) == 0:
            raise CustomHTTPException(
                status_code=HTTPStatus.BAD_REQUEST.value,
                error_code=ErrorCode.INVALID_FILE_ERROR.value,
                detail="A valid jpg or png image must be provided."
            )
        self._validate_image_size(image_size=len(image_bytes))
        file_info = filetype.guess(image_bytes)
        if not file_info:
            raise CustomHTTPException(
                status_code=HTTPStatus.UNSUPPORTED_MEDIA_TYPE.value,
                error_code=ErrorCode.UNSUPPORTED_FILE_TYPE_ERROR.value,
                detail="Unable to determine file type.",
            )
        self._validate_image_type(content_type=file_info.extension.lower())
        return image_bytes

    def _validate_image_type(self, content_type: str):
        if content_type not in self.accepted_file_types:
            raise CustomHTTPException(
                status_code=HTTPStatus.UNSUPPORTED_MEDIA_TYPE.value,
                error_code=ErrorCode.UNSUPPORTED_FILE_TYPE_ERROR.value,
                detail="Unsupported file type. Expected jpg, png or webp image.",
            )

    def _validate_image_size(self, image_size: int):
        if image_size > self.file_size_limit:
            raise CustomHTTPException(
                status_code=HTTPStatus.REQUEST_ENTITY_TOO_LARGE.value,
                error_code=ErrorCode.FILE_SIZE_ERROR.value,
                detail=f"Image File is too large. File must be less than {self.file_size_limit} bytes.",
            )
