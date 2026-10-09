import logging
from fastapi import APIRouter, FastAPI, Form, BackgroundTasks
from typing_extensions import Annotated

from conf.logging import CustomLoggers
from service.api.image_service import ImageService
from conf.config import Config
from models.models import ImageUploadRequest, ReadingExtractionRequest, ReadingExtractionResponse, FeedbackRequest, FeedbackResponse

from dotenv import load_dotenv

load_dotenv()
app = FastAPI()
router = APIRouter()
config = Config()
CustomLoggers(config=config)
flow_vision_service = ImageService(config=config)
basepath = "/flowvision/v1"

@app.get("/")
async def root():
    return {"message": "Hi, I am the meter reading assistant."}


@router.post("/extract-reading", response_model=ReadingExtractionResponse, response_model_exclude_none=True)
async def extract_reading(request: ReadingExtractionRequest, background_tasks: BackgroundTasks):
    response = flow_vision_service.extract_reading(request, background_tasks)
    return response


@router.post("/feedback", response_model=FeedbackResponse, response_model_exclude_none=True)
async def log_feedback(request: FeedbackRequest, background_tasks: BackgroundTasks):
    response = flow_vision_service.log_feedback(request, background_tasks)
    return response


# Original base path for existing clients, plus the per-meter path (ADR-001)
app.include_router(router, prefix=basepath)
app.include_router(router, prefix=f"{basepath}/bfm")
