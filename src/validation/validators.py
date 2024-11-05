from fastapi import HTTPException
from http import HTTPStatus
import filetype

from models.api_models import RequestForm


class ImageValidator:
    file_size_limit = 20971520  # 20MB so effectively no limit for now
    accepted_file_types = ['image/jpeg', 'image/jpg', 'image/png', 'jpeg', 'jpg', 'png']

    async def validate(self, request: RequestForm):
        image_content_type = request.data.content_type
        image_size = request.data.size
        image_bytes = bytes()

        self._validate_image_type(content_type=image_content_type)
        self._validate_image_size(image_size=image_size)

        while content := await request.data.read(1024):
            image_bytes = image_bytes + content
            self._validate_image_size(image_size=len(image_bytes))

        if len(image_bytes) == 0:
            raise HTTPException(
                status_code=HTTPStatus.BAD_REQUEST.value,
                detail={
                    "phrase": HTTPStatus.BAD_REQUEST.phrase,
                    "details": "A valid jpg or png image must be provided."
                }
            )

        file_info = filetype.guess(image_bytes)
        if not file_info:
            raise HTTPException(
                status_code=HTTPStatus.UNSUPPORTED_MEDIA_TYPE.value,
                detail={
                    "phrase": HTTPStatus.UNSUPPORTED_MEDIA_TYPE.phrase,
                    "details": "Unable to determine file type."
                },
            )

        detected_content_type = file_info.extension.lower()
        self._validate_image_type(content_type=detected_content_type)
        return image_bytes

    def _validate_image_type(self, content_type: str):
        if (content_type not in self.accepted_file_types):
            raise HTTPException(
                status_code=HTTPStatus.UNSUPPORTED_MEDIA_TYPE.value,
                detail={
                    "phrase": HTTPStatus.UNSUPPORTED_MEDIA_TYPE.phrase,
                    "details": "Unsupported file type. Expected jpg or png image."
                },
            )

    def _validate_image_size(self, image_size: int):
        if image_size > self.file_size_limit:
            raise HTTPException(
                status_code=HTTPStatus.REQUEST_ENTITY_TOO_LARGE.value,
                detail={
                    "phrase": HTTPStatus.REQUEST_ENTITY_TOO_LARGE.phrase,
                    "details": f"Image File is too large. File must be less than {self.file_size_limit} bytes."
                }
            )
