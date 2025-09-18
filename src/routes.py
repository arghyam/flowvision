from fastapi import FastAPI
from starlette.concurrency import run_in_threadpool

from conf.logging import CustomLoggers
from service.api.image_service import ImageService
from conf.config import Config
from models.models import ReadingExtractionRequest, ReadingExtractionResponse, FeedbackRequest, FeedbackResponse

from dotenv import load_dotenv


load_dotenv()
app = FastAPI()
config = Config()
CustomLoggers(config=config)
flow_vision_service = ImageService(config=config)
basepath = "/flowvision/v1"


@app.get("/")
async def root():
    return {"message": "Hi, I am the meter reading assistant."}


@app.post(
    f"{basepath}/extract-reading",
    response_model=ReadingExtractionResponse,
    response_model_exclude_none=True,
)
async def extract_reading(request: ReadingExtractionRequest):
    return await run_in_threadpool(flow_vision_service.extract_reading, request)


@app.post(
    f"{basepath}/feedback",
    response_model=FeedbackResponse,
    response_model_exclude_none=True,
)
async def log_feedback(request: FeedbackRequest):
    return await run_in_threadpool(flow_vision_service.log_feedback, request)
