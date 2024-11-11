import http
from enum import StrEnum, _simple_enum


@_simple_enum(StrEnum)
class ErrorCode:
    def __new__(self, value, phrase, description=''):
        obj = str.__new__(self, value)
        obj._value_ = value

        obj.phrase = phrase
        obj.description = description
        return obj

    INVALID_FILE_ERROR = "ERR_001", "Invalid File Error", "A valid jpg or png image must be provided."
    UNSUPPORTED_FILE_TYPE_ERROR = "ERR_002", "Unsupported File Type Error", "Unsupported file type. Expected jpg or png image."
    FILE_SIZE_ERROR = "ERROR_003", "File Size Error", "File is too large."
    LLM_ERROR = "ERROR_04", "LLM Error", "An Unexpected error occurred while calling LLM."


class CustomHTTPException(Exception):
    def __init__(
        self,
        status_code: int,
        phrase: str | None = None,
        detail: str | None = None,
        error_code: int | None = None,
        headers=None
    ) -> None:

        if phrase is None:
            phrase = http.HTTPStatus(status_code).phrase
        if error_code is None:
            error_code = status_code
        self.status_code = status_code
        self.phrase = phrase
        self.detail = detail
        self.error_code = error_code
        self.headers = headers

    def __str__(self) -> str:
        return f"{self.status_code}: {self.phrase}"

    def __repr__(self) -> str:
        class_name = self.__class__.__name__
        return f"{class_name}(status_code={self.status_code!r}, detail={self.phrase!r})"
