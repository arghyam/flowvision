from pydantic import BaseModel
from datetime import datetime
from typing import Optional
from fastapi import UploadFile


class DataField(BaseModel):
    data: str


class Error(BaseModel):
    errorCode: int
    errorMessage: str
    errorDetails: Optional[str] = None

class Request(BaseModel):
    id: str
    ts: datetime
    image: UploadFile
    metadata: Optional[dict] = None
    jal_mitra_id: Optional[str] = None


class Response(BaseModel):
    id: str
    ts: datetime
    responseCode: str
    statusCode: int
    data: DataField | Error
    params: Optional[str] = None
