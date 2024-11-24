from pydantic import BaseModel
from datetime import datetime
from fastapi import UploadFile
from enum import StrEnum
from uuid import UUID


class Error(BaseModel):
    errorCode: str | int
    errorMsg: str

class Status(StrEnum):
    NOMETER = 'NOMETER'
    UNCLEAR = 'UNCLEAR'
    SUCCESS = 'SUCCESS'

class BaseResponse(BaseModel):
    id: UUID
    ts: datetime
    responseCode: str
    statusCode: int
    error: Error | None = None

class ReadingExtractionRequest(BaseModel):
  id: UUID | None = None
  ts: datetime
  imageURL: str
  metadata: dict | None = None


class ImageUploadRequest(BaseModel):
  id: UUID | None = None
  ts: datetime
  image: UploadFile
  metadata: dict | None = None

class ImageUploadResult(BaseModel):
  imageURL: str


class ReadingExtractionResult(BaseModel):
  status: Status
  meterReading: float | str | None = None
  meterBrand: str | None = None


class ImageUploadResponse(BaseResponse):
    result: ImageUploadResult | None = None


class ReadingExtractionResponse(BaseResponse):
    result: ReadingExtractionResult | None = None
