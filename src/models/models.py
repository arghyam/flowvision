from pydantic import BaseModel
from datetime import datetime
from fastapi import UploadFile
from enum import StrEnum
from uuid import UUID
from typing import Optional, Dict, Any


class Error(BaseModel):
  errorCode: str | int
  errorMsg: str


class ResponseCode(StrEnum):
  OK = "OK"
  ERROR = "ERROR"


class FeedbackResponseStatus(StrEnum):
  SUBMITTED = "SUBMITTED"
  FAILED = "FAILED"


class Status(StrEnum):
  NOMETER = 'NOMETER'
  UNCLEAR = 'UNCLEAR'
  SUCCESS = 'SUCCESS'


class BaseResponse(BaseModel):
  id: UUID
  ts: datetime
  responseCode: ResponseCode
  statusCode: int
  error: Optional[Error] = None


class BaseRequest(BaseModel):
  id: Optional[UUID] = None
  ts: Optional[datetime] = None


class ReadingExtractionRequest(BaseRequest):
  imageURL: str
  metadata: Optional[Dict[str, Any]] = None


class ImageUploadRequest(BaseRequest):
  image: UploadFile
  metadata: dict | None = None


class ImageUploadResult(BaseModel):
  imageURL: str


class ReadingExtractionResultData(BaseModel):
  meterReading: Optional[float | str] = None
  meterBrand: Optional[str] = None


class ReadingExtractionResult(BaseModel):
  status: Status
  correlationId: UUID
  data: Optional[ReadingExtractionResultData] = None


class ImageUploadResponse(BaseResponse):
  result: ImageUploadResult | None = None


class ReadingExtractionResponse(BaseResponse):
  result: Optional[ReadingExtractionResult] = None


class FeedbackRequestData(BaseModel):
  accurate: bool
  actualReading: Optional[float] = None


class FeedbackRequest(BaseRequest):
  correlationId: UUID
  data: FeedbackRequestData


class FeedbackResponse(BaseResponse):
  status: FeedbackResponseStatus
