from fastapi import FastAPI, Form
from typing_extensions import Annotated

from service.api.image_service import ImageService
from service.api.storage_service import StorageService
from conf.config import Config
from models.models import ImageUploadRequest, ReadingExtractionRequest

from dotenv import load_dotenv

load_dotenv()
app = FastAPI()
config = Config()
flow_vision_service = ImageService(config=config)
storage_service = StorageService(config=config)
basepath = "/flowvision/v1"


@app.get("/")
async def root():
    return {"message": "Hi, I am the meter reading assistant."}


@app.post(f"{basepath}/uploadImage")
async def upload_image(request: Annotated[ImageUploadRequest, Form()]):
    return await storage_service.upload_image(request)


@app.post(f"{basepath}/extractReading")
async def extract_reading(request: ReadingExtractionRequest):
    return flow_vision_service.extract_reading(request)
