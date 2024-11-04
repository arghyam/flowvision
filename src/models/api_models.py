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


class RequestForm(BaseModel):
    id: str
    ts: datetime
    data: UploadFile
    params: Optional[str] = None
    model_config = {"extra": "forbid"}


class Response(BaseModel):
    id: str
    ts: datetime
    responseCode: str
    statusCode: int
    data: DataField | Error
    params: Optional[str] = None
