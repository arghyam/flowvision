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

    # Codes shared with FlowVision
    INVALID_FILE_ERROR = "ERR_001", "Invalid File Error", "A valid jpg or png image must be provided."
    UNSUPPORTED_FILE_TYPE_ERROR = "ERR_002", "Unsupported File Type Error", "Unsupported file type. Expected jpg or png image."
    FILE_SIZE_ERROR = "ERROR_003", "File Size Error", "File is too large."
    LLM_ERROR = "ERROR_04", "LLM Error", "An Unexpected error occurred while calling LLM."
    # Meter reader
    IMAGE_DOWNLOAD_ERROR = "ERROR_05", "Image Download Error", "The image URL could not be downloaded."
    INVALID_REQUEST_ERROR = "ERROR_06", "Invalid Request Error", "The request is missing or has conflicting fields."
    UNKNOWN_METER_TYPE_ERROR = "ERROR_07", "Unknown Meter Type Error", "The meter type is not in the registry."
    NOT_FOUND_ERROR = "ERROR_08", "Not Found Error", "No record with this ID."
    UNAUTHORIZED_ERROR = "ERROR_09", "Unauthorized", "Missing or wrong X-API-Key header."
    BUSY_ERROR = "ERROR_10", "Service Busy", "Too many readings are in progress; retry shortly."
    BUDGET_EXCEEDED_ERROR = "ERROR_11", "Budget Exceeded", "The daily model budget has been reached."
    IN_PROGRESS_ERROR = "ERROR_12", "Request In Progress", "A request with this id is still being processed."
    STORAGE_ERROR = "ERROR_13", "Storage Error", "The database is unavailable."
    INTERNAL_ERROR = "ERROR_14", "Internal Error", "An unexpected error occurred."
    ID_CONFLICT_ERROR = "ERROR_15", "Id Conflict", "This id was already used for a different request."


class CustomHTTPException(Exception):
    def __init__(
        self,
        status_code: int,
        phrase: str | None = None,
        detail: str | None = None,
        error_code: int | str | None = None,
        headers=None,
        retryable: bool = False,
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
        self.retryable = retryable      # True: sending the same request again later can succeed

    def __str__(self) -> str:
        return f"{self.status_code}: {self.phrase}"

    def __repr__(self) -> str:
        class_name = self.__class__.__name__
        return f"{class_name}(status_code={self.status_code!r}, detail={self.phrase!r})"
