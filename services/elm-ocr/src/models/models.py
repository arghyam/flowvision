"""API models. Same envelope and field names as FlowVision's BFM service (services/bfm-ocr/src/models/models.py),
extended for the meter reader."""
import math
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Dict, List, Optional
from uuid import UUID

from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict, Field, model_validator

# Request ids come from the calling system (its own ids are fine) or are generated as UUIDs. They name the stored
# photo, so only letters, digits, '.', '_' and '-' are allowed.
REQUEST_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
def _not_bool(value):
    if isinstance(value, bool):
        raise ValueError("a reading must be digits (a string or a number), not true/false")
    return value


# A reading sent as a JSON number keeps its form: 19000 stays "19000" (int first), 19712.7 stays "19712.7".
# true/false are refused (they would otherwise become 1/0).
ReadingValue = Annotated[int | float | str, BeforeValidator(_not_bool)]


def _whole_number(value):
    if isinstance(value, bool):
        raise ValueError("must be a whole number, not true/false")
    return value


def _plausible_time(value):
    """Times a meter photo can have: a far-off date is a client's placeholder (e.g. DateTime.MinValue), not a time."""
    if value is not None and not 2000 <= value.year <= 2100:
        raise ValueError("must be a date between 2000 and 2100")
    return value


PhotoTime = Annotated[Optional[datetime], AfterValidator(_plausible_time)]


MAX_NESTING = 20


def storable(value, where="request", depth=0):
    """Refuses what a database cannot hold: the NUL character in any text (keys included), NaN and Infinity, and
    details nested more than MAX_NESTING levels deep."""
    if depth > MAX_NESTING:
        raise ValueError(f"{where} is nested more than {MAX_NESTING} levels deep")
    if isinstance(value, str):
        if "\x00" in value:
            raise ValueError(f"{where} contains a NUL character")
        if any("\ud800" <= ch <= "\udfff" for ch in value):
            raise ValueError(f"{where} contains invalid text (an unpaired surrogate)")
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{where} contains NaN or Infinity")
    elif isinstance(value, dict):
        for k, v in value.items():
            storable(k, where, depth + 1)
            storable(v, f"{where}.{k}" if depth < 3 else where, depth + 1)
    elif isinstance(value, (list, tuple)):
        for v in value:
            storable(v, where, depth + 1)
    elif isinstance(value, BaseModel):
        storable(value.model_dump(), where, depth)
    return value


def _plain_id(value):
    """A request id sent as a JSON number keeps its digits; true/false, NaN and Infinity are not ids."""
    if isinstance(value, bool) or (isinstance(value, float) and not math.isfinite(value)):
        raise ValueError("id must be text (letters, digits, . _ -), not true/false, NaN or Infinity")
    return value


class Error(BaseModel):
    errorCode: str | int
    errorMsg: str
    retryable: Optional[bool] = Field(None, description="True: the same request (same id) can be sent again later")


class ResponseCode(StrEnum):
    OK = "OK"
    ERROR = "ERROR"


class FeedbackResponseStatus(StrEnum):
    SUBMITTED = "SUBMITTED"
    FAILED = "FAILED"


class Status(StrEnum):
    NOMETER = 'NOMETER'            # no meter display visible: ask for a new photo
    UNCLEAR = 'UNCLEAR'            # wrong photo (not the total screen, no unit or the wrong unit): ask for a new one
    SUCCESS = 'SUCCESS'            # reading passed every check
    NEEDS_REVIEW = 'NEEDS_REVIEW'  # hard to read (glare, unclear digit, low confidence) or failed a consistency check: a person checks it


class BaseResponse(BaseModel):
    id: str
    ts: datetime
    responseCode: ResponseCode
    statusCode: int
    error: Optional[Error] = None


class BaseRequest(BaseModel):
    id: Annotated[Optional[str], BeforeValidator(_plain_id)] = Field(None, pattern=REQUEST_ID_PATTERN,
                              description="The caller's id for this request (letters, digits, . _ -; up to 128). "
                                          "Sending the same id again returns the stored result instead of reading again.")
    ts: Optional[datetime] = None


class PreviousReading(BaseModel):
    value: ReadingValue
    unit: Optional[str] = None
    capturedAt: PhotoTime = None


class ReadingExtractionRequest(BaseRequest):
    # The calling system may send any other details it has (e.g. meterNumber, village, agentId): they are accepted
    # and stored with the reading, in the request JSON. Numbers sent for text fields (e.g. consumerId) are accepted.
    model_config = ConfigDict(extra="allow", coerce_numbers_to_str=True)

    imageURL: Optional[str] = Field(None, description="HTTP(S) URL of the photo on the calling system; the service "
                                    "downloads it (or send imageBase64)")
    imageBase64: Optional[str] = Field(None, description="The photo, base64-encoded")
    consumerId: Optional[str] = Field(None, max_length=128,
                                      description="Enables the consumer's meter settings and previous-reading check")
    capturedAt: PhotoTime = Field(None, description="When the photo was taken (ISO 8601, between 2000 and 2100); "
                                  "without a time zone it is taken as India time. Defaults to now")
    previousReading: Optional[PreviousReading] = Field(None, description="Overrides the stored previous reading")
    metadata: Optional[Dict[str, Any]] = None

    @model_validator(mode="after")
    def _storable(self):
        storable({k: v for k, v in self.__dict__.items() if k != "imageBase64"}, "request")
        storable(self.model_extra or {}, "request")
        return self


class Reason(BaseModel):
    code: str
    message: str


class MeterInfo(BaseModel):
    typeId: Optional[str] = None
    make: Optional[str] = None
    modelCode: Optional[str] = None
    faceplateText: List[str] = []


class ReadSummary(BaseModel):
    status: Optional[str] = None
    display: Optional[str] = None
    unit: Optional[str] = None
    screenLabel: Optional[str] = None
    screenType: Optional[str] = None
    confidence: Optional[float] = None
    meterMake: Optional[str] = None
    meterModelCode: Optional[str] = None
    bestGuess: Optional[str] = None
    decimalPlacesGuess: Optional[int] = None
    check: Optional[str] = None
    reason: Optional[str] = None
    seconds: Optional[float] = None


class Action(StrEnum):
    ACCEPT = "ACCEPT"    # SUCCESS: the reading passed every check
    RETAKE = "RETAKE"    # UNCLEAR / NOMETER: no display, the wrong screen or the wrong unit; a new photo is needed
    REVIEW = "REVIEW"    # NEEDS_REVIEW: hard to read or failed a consistency check; a person confirms or corrects it


class ReadingExtractionResultData(BaseModel):
    meterReading: Optional[str] = Field(None, description="The reading as displayed, e.g. 0019712.7, whenever a display "
                                        "was seen ('?' = a digit the model could not guess); status and action say how "
                                        "far to trust it. A short explanation when no display was seen (NOMETER), or when the "
                                        "model could not read a single digit")
    action: Optional[Action] = Field(None, description="ACCEPT, RETAKE or REVIEW")
    unit: Optional[str] = None
    meterBrand: Optional[str] = None
    decimalSource: Optional[str] = Field(None, description="Where the decimal point comes from: consumer | registry "
                                         "(meter type) | visible_point | previous_reading | model_guess")
    uncertainDigits: List[int] = Field([], description="1-based positions of the digits in meterReading the model "
                                       "was unsure of (leading zeros counted, decimal point not)")
    reasons: List[Reason] = []
    retakeHint: Optional[str] = Field(None, description="Message to show the consumer when a new photo is needed")
    meter: Optional[MeterInfo] = None
    previousReading: Optional[Dict[str, Any]] = None
    reads: List[ReadSummary] = []
    notes: List[str] = []
    model: Optional[str] = None
    promptVersion: Optional[str] = None
    processingTime: float
    costEstimateUsd: Optional[float] = None


class ReadingExtractionResult(BaseModel):
    status: Status
    correlationId: UUID
    data: Optional[ReadingExtractionResultData] = None


class ReadingExtractionResponse(BaseResponse):
    result: Optional[ReadingExtractionResult] = None


class FeedbackRequestData(BaseModel):
    accurate: Optional[bool] = Field(None, description="true: the returned reading is the correct cumulative reading; "
                                     "false: it is wrong. May be left out when `actual` is sent")
    extracted: Optional[ReadingValue] = None
    actual: Optional[ReadingValue] = Field(None, description="The true reading; send it as a string to keep leading zeros")

    @model_validator(mode="after")
    def _something_to_record(self):
        if self.accurate is None and self.actual is None:
            raise ValueError("send accurate (true/false), actual (the true reading), or both")
        return self


class FeedbackRequest(BaseRequest):
    correlationId: UUID
    data: FeedbackRequestData


class FeedbackStatus(BaseModel):
    status: FeedbackResponseStatus


class FeedbackResponse(BaseResponse):
    result: Optional[FeedbackStatus] = None


class MeterProfileRequest(BaseModel):
    meterTypeId: Optional[str] = Field(None, description="One of GET /meter-types")
    decimals: Annotated[Optional[int], BeforeValidator(_whole_number)] = Field(
        None, ge=0, le=3, description="Digits after the decimal point; overrides the type")
    digits: Annotated[Optional[int], BeforeValidator(_whole_number)] = Field(
        None, ge=1, le=12, description="Total digits on the display, leading zeros included")
    billingUnit: Optional[str] = Field(None, description="kWh or kVAh; a clear reading in another unit is returned as "
                                                         "UNCLEAR (a hard-to-read one goes to review)")


class MeterProfile(BaseModel):
    consumerId: str
    meterTypeId: Optional[str] = None
    decimals: Optional[int] = None
    digits: Optional[int] = None
    billingUnit: Optional[str] = None
    updatedAt: Optional[datetime] = None


class MeterProfileResponse(BaseResponse):
    result: Optional[MeterProfile] = None


class MeterTypeInfo(BaseModel):
    id: str
    description: str
    decimals: int
    digits: Optional[int] = None
    patterns: List[str]
    evidence: str = ""


class MeterTypesResponse(BaseResponse):
    result: Optional[List[MeterTypeInfo]] = None
