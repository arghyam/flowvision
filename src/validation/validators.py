from error.error import CustomHTTPException
from http import HTTPStatus
import filetype

from conf.config import Config
from models.models import ImageUploadRequest


class ImageValidator:
    accepted_file_types = ['image/jpeg', 'image/jpg', 'image/png', 'jpeg', 'jpg', 'png']

    def __init__(self, config: Config):
        self.config = config
        self.file_size_limit = self.config.find("file_size_limit", default=20971520)

    async def validate(self, request: ImageUploadRequest):
        image_content_type = request.image.content_type
        image_size = request.image.size
        image_name = request.image.filename
        image_bytes = bytes()

        self._validate_image_type(content_type=image_content_type)
        self._validate_image_size(image_size=image_size)

        while content := await request.image.read(1024):
            image_bytes = image_bytes + content
            self._validate_image_size(image_size=len(image_bytes))

        if len(image_bytes) == 0:
            raise CustomHTTPException(
                status_code=HTTPStatus.BAD_REQUEST.value,
                detail="A valid jpg or png image must be provided."
            )

        file_info = filetype.guess(image_bytes)
        if not file_info:
            raise CustomHTTPException(
                status_code=HTTPStatus.UNSUPPORTED_MEDIA_TYPE.value,
                detail="Unable to determine file type.",
            )

        detected_content_type = file_info.extension.lower()
        self._validate_image_type(content_type=detected_content_type)

        return image_bytes, image_content_type, image_name

    def _validate_image_type(self, content_type: str):
        if (content_type not in self.accepted_file_types):
            raise CustomHTTPException(
                status_code=HTTPStatus.UNSUPPORTED_MEDIA_TYPE.value,
                detail="Unsupported file type. Expected jpg or png image.",
            )

    def _validate_image_size(self, image_size: int):
        if image_size > self.file_size_limit:
            raise CustomHTTPException(
                status_code=HTTPStatus.REQUEST_ENTITY_TOO_LARGE.value,
                detail=f"Image File is too large. File must be less than {self.file_size_limit} bytes.",
            )
